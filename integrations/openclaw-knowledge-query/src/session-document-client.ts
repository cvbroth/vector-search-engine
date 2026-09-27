/** Fixed Unix-socket protocol. The model cannot choose identities or file paths. */

import { createHash } from "node:crypto";
import http from "node:http";
import Value from "typebox/value";
import { Type } from "typebox";
import type { OpenedAttachment } from "./attachment-registry.js";

const SOCKET_PATH = "/run/knowledge-session-doc/query.sock";
const RESPONSE_LIMIT = 128 * 1024;
const QUERY_TIMEOUT_MS = 60_000;
const INDEX_TIMEOUT_MS = 120_000;

const documentSchema = Type.Object({
  attachment_id: Type.String({ pattern: "^[0-9a-f]{32}$" }),
  filename: Type.String(),
  status: Type.Union([Type.Literal("INDEXING"), Type.Literal("READY"), Type.Literal("FAILED"), Type.Literal("NO_SEARCHABLE_TEXT")]),
}, { additionalProperties: false });
const listSchema = Type.Object({ documents: Type.Array(documentSchema, { maxItems: 4 }) },
  { additionalProperties: false });
const evidenceSchema = Type.Object({
  rank: Type.Integer({ minimum: 1 }),
  page: Type.Union([Type.Integer({ minimum: 1 }), Type.Null()]),
  chunk_index: Type.Integer({ minimum: 0 }),
  text: Type.String(),
  fused_score: Type.Number(),
  semantic_score: Type.Union([Type.Number(), Type.Null()]),
  lexical_match: Type.Boolean(),
}, { additionalProperties: false });
const querySchema = Type.Object({
  status: Type.Union([
    Type.Literal("INDEXING"), Type.Literal("READY"), Type.Literal("FAILED"), Type.Literal("NOT_FOUND"),
    Type.Literal("NO_SEARCHABLE_TEXT"),
  ]),
  attachment_id: Type.Optional(Type.String({ pattern: "^[0-9a-f]{32}$" })),
  filename: Type.Optional(Type.String()),
  evidence: Type.Array(evidenceSchema, { maxItems: 10 }),
}, { additionalProperties: false });
const indexSchema = Type.Object({
  status: Type.Union([Type.Literal("INDEXING"), Type.Literal("READY"), Type.Literal("FAILED"), Type.Literal("NO_SEARCHABLE_TEXT")]),
  attachment_id: Type.String({ pattern: "^[0-9a-f]{32}$" }),
}, { additionalProperties: false });

export type ListedDocument = { attachment_id: string; filename: string; status: "INDEXING" | "READY" | "FAILED" | "NO_SEARCHABLE_TEXT" };
export type DocumentQuery = {
  status: "INDEXING" | "READY" | "FAILED" | "NOT_FOUND" | "NO_SEARCHABLE_TEXT";
  attachment_id?: string;
  filename?: string;
  evidence: Array<{
    rank: number; page: number | null; chunk_index: number; text: string;
    fused_score: number; semantic_score: number | null; lexical_match: boolean;
  }>;
};

export function hashSessionKey(sessionKey: string): string {
  return createHash("sha256").update(sessionKey, "utf8").digest("hex");
}

function exchange(
  route: string, body: Buffer, contentType: string, timeoutMs: number,
  headers: Record<string, string> = {}, signal?: AbortSignal,
  opened?: OpenedAttachment,
): Promise<unknown> {
  if (signal?.aborted) return Promise.reject(new Error("session document request aborted"));
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error?: Error, value?: unknown) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      if (error) reject(error);
      else resolve(value);
    };
    const request = http.request({
      socketPath: SOCKET_PATH, path: route, method: "POST",
      headers: {
        "Content-Type": contentType, "Content-Length": opened ? Number(opened.attachment.size) : body.byteLength,
        Accept: "application/json", ...headers,
      },
    }, (response) => {
      const parts: Buffer[] = [];
      let total = 0;
      response.on("data", (part: Buffer) => {
        total += part.byteLength;
        if (total > RESPONSE_LIMIT) request.destroy(new Error("session document response too large"));
        else parts.push(part);
      });
      response.on("error", (error) => finish(error));
      response.on("aborted", () => finish(new Error("session document response aborted")));
      response.on("end", () => {
        if (response.statusCode !== 200) {
          finish(new Error(`session document service HTTP ${response.statusCode ?? "unknown"}`));
          return;
        }
        try { finish(undefined, JSON.parse(Buffer.concat(parts, total).toString("utf8"))); }
        catch { finish(new Error("invalid session document response")); }
      });
    });
    const onAbort = () => request.destroy(new Error("session document request aborted"));
    const timer = setTimeout(() => request.destroy(new Error("session document request timed out")), timeoutMs);
    signal?.addEventListener("abort", onAbort, { once: true });
    request.on("error", (error) => finish(error));
    if (opened) {
      const stream = opened.handle.createReadStream({ autoClose: false });
      stream.on("error", (error) => request.destroy(error));
      request.on("close", () => stream.destroy());
      stream.pipe(request);
    } else request.end(body);
  });
}

function jsonBody(payload: Record<string, unknown>): Buffer {
  return Buffer.from(JSON.stringify(payload), "utf8");
}

export async function listSessionDocuments(agentId: string, sessionDigest: string, signal?: AbortSignal): Promise<ListedDocument[]> {
  const value = await exchange("/v1/list", jsonBody({ agent_id: agentId, session_hash: sessionDigest }),
    "application/json", QUERY_TIMEOUT_MS, {}, signal);
  if (!Value.Check(listSchema, value)) throw new Error("invalid session document list response");
  return value.documents;
}

export async function querySessionDocument(
  agentId: string, sessionDigest: string, attachmentId: string,
  query: string, topK: number, signal?: AbortSignal,
): Promise<DocumentQuery> {
  const value = await exchange("/v1/query", jsonBody({
    agent_id: agentId, session_hash: sessionDigest, attachment_id: attachmentId,
    query, top_k: topK,
  }), "application/json", QUERY_TIMEOUT_MS, {}, signal);
  if (!Value.Check(querySchema, value) ||
      (value.status === "READY" && (value.attachment_id !== attachmentId ||
        typeof value.filename !== "string")) ||
      value.evidence.some((item, index) => item.rank !== index + 1) ||
      (value.status !== "READY" && value.evidence.length > 0)) {
    throw new Error("invalid session document query response");
  }
  return value;
}

export async function submitSessionDocument(
  agentId: string, sessionDigest: string, attachmentId: string,
  opened: OpenedAttachment, signal?: AbortSignal,
): Promise<"INDEXING" | "READY" | "FAILED" | "NO_SEARCHABLE_TEXT"> {
  try {
    const value = await exchange("/v1/index", Buffer.alloc(0), "application/pdf", INDEX_TIMEOUT_MS, {
      "X-Session-Agent": agentId,
      "X-Session-Hash": sessionDigest,
      "X-Attachment-Id": attachmentId,
      "X-Attachment-Filename": encodeURIComponent(opened.attachment.filename),
    }, signal, opened);
    if (!Value.Check(indexSchema, value) || value.attachment_id !== attachmentId) {
      throw new Error("invalid session document index response");
    }
    return value.status;
  } finally { await opened.handle.close(); }
}
