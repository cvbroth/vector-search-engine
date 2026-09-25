/** Streaming, fixed-Unix-socket transfer of an already opened trusted attachment. */

import http from "node:http";
import Value from "typebox/value";
import { importResultSchema } from "./contracts.js";
import type { OpenedAttachment } from "./attachment-registry.js";
import type { AccessKind } from "./unix-client.js";

const SOCKET_PATH = "/run/knowledge-import-broker/import.sock";
const TIMEOUT_MS = 120_000;
const MAX_RESPONSE_BYTES = 4096;

export type ImportOutcome = {
  status: "QUEUED" | "NO_ATTACHMENT" | "SELECTION_REQUIRED" | "UNSUPPORTED_TYPE" |
    "ATTACHMENT_CHANGED" | "TOO_LARGE" | "ALREADY_QUEUED" | "CONSENT_REQUIRED" | "BROKER_ERROR";
  filename?: string;
  content_type?: string;
  size_bytes?: number;
  sha256_prefix?: string;
  access_kind?: AccessKind;
};

export async function queueAttachment(
  opened: OpenedAttachment, agentId: string, kind: AccessKind, signal?: AbortSignal,
): Promise<ImportOutcome> {
  if (signal?.aborted) throw new Error("knowledge import aborted");
  const { handle, attachment } = opened;
  const stream = handle.createReadStream({ autoClose: false });
  try {
    return await new Promise<ImportOutcome>((resolve, reject) => {
      let settled = false;
      const finish = (error?: Error, result?: ImportOutcome) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
        if (error) reject(error);
        else resolve(result!);
      };
      const request = http.request({
        socketPath: SOCKET_PATH,
        path: kind === "private" ? "/v1/private-attachment" : "/v1/shared-attachment",
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream",
          "Content-Length": Number(attachment.size),
          "X-Knowledge-Agent": agentId,
          "X-Knowledge-Filename": encodeURIComponent(attachment.filename),
          "X-Knowledge-Content-Type": encodeURIComponent(attachment.contentType),
          ...(attachment.messageId ? { "X-Knowledge-Message-Id": encodeURIComponent(attachment.messageId) } : {}),
          Accept: "application/json",
        },
      }, (response) => {
        const parts: Buffer[] = [];
        let total = 0;
        response.on("data", (part: Buffer) => {
          total += part.byteLength;
          if (total > MAX_RESPONSE_BYTES) request.destroy(new Error("knowledge import response too large"));
          else parts.push(part);
        });
        response.on("error", (error) => finish(error));
        response.on("aborted", () => finish(new Error("knowledge import response aborted")));
        response.on("end", () => {
          if (settled) return;
          if (response.statusCode === 413) {
            finish(undefined, { status: "TOO_LARGE" });
            return;
          }
          if (response.statusCode !== 200) {
            finish(new Error(`knowledge import broker HTTP ${response.statusCode ?? "unknown"}`));
            return;
          }
          try {
            const value: unknown = JSON.parse(Buffer.concat(parts, total).toString("utf8"));
            if (!Value.Check(importResultSchema, value) || (value as ImportOutcome).status !== "QUEUED" ||
                (value as ImportOutcome).access_kind !== kind ||
                typeof (value as ImportOutcome).filename !== "string" ||
                !/^[^/\\\0]+$/.test((value as ImportOutcome).filename!) ||
                (value as ImportOutcome).size_bytes !== Number(attachment.size) ||
                typeof (value as ImportOutcome).sha256_prefix !== "string" ||
                !/^[a-f0-9]{12}$/.test((value as ImportOutcome).sha256_prefix!) ||
                typeof (value as ImportOutcome).content_type !== "string") {
              throw new Error("knowledge import broker protocol error");
            }
            finish(undefined, value as ImportOutcome);
          } catch (error) { finish(error instanceof Error ? error : new Error("invalid broker response")); }
        });
      });
      const onAbort = () => request.destroy(new Error("knowledge import aborted"));
      const timer = setTimeout(() => request.destroy(new Error("knowledge import timed out")), TIMEOUT_MS);
      signal?.addEventListener("abort", onAbort, { once: true });
      request.on("error", (error) => finish(error));
      stream.on("error", (error) => request.destroy(error));
      if (signal?.aborted) onAbort();
      else stream.pipe(request);
    });
  } finally {
    stream.destroy();
    await handle.close();
  }
}
