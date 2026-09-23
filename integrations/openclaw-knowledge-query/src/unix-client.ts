/** Fixed local broker transport. Neither socket path nor scope is caller supplied. */

import http from "node:http";
import Value from "typebox/value";
import { modelContextSchema, type ModelContext } from "./contracts.js";

const SOCKET_PATH = "/run/knowledge-broker/query.sock";
const TIMEOUT_MS = 10_000;
const MAX_RESPONSE_BYTES = 1024 * 1024;

export type AccessKind = "private" | "shared";
export type BrokerQuery = {
  kind: AccessKind;
  agentId: string;
  query: string;
  topK: number;
  signal?: AbortSignal;
};

function validateResponse(value: unknown, request: BrokerQuery): ModelContext {
  if (!Value.Check(modelContextSchema, value)) {
    throw new Error("knowledge broker protocol error: invalid RAG Context schema");
  }
  const context = value as ModelContext;
  if (context.query !== request.query || context.evidence_count !== context.evidence.length ||
      context.evidence_count > request.topK ||
      context.evidence.some((item, index) => item.rank !== index + 1)) {
    throw new Error("knowledge broker protocol error: response does not match request");
  }
  const hasAccept = context.evidence.some((item) => item.relevance_decision === "ACCEPT");
  const expectedStatus = context.evidence_count === 0 ? "REJECT" :
    hasAccept ? "ACCEPT" : "UNCERTAIN";
  if (context.retrieval_status !== expectedStatus) {
    throw new Error("knowledge broker protocol error: inconsistent retrieval status");
  }
  return context;
}

export function queryBroker(request: BrokerQuery): Promise<ModelContext> {
  if (request.kind !== "private" && request.kind !== "shared") {
    return Promise.reject(new Error("invalid knowledge access kind"));
  }
  if (request.signal?.aborted) return Promise.reject(new Error("knowledge query aborted"));
  const body = Buffer.from(JSON.stringify({
    agent_id: request.agentId,
    query: request.query,
    top_k: request.topK,
  }), "utf8");
  const requestPath = request.kind === "private" ? "/v1/private-context" : "/v1/shared-context";

  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error?: Error, value?: ModelContext) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      request.signal?.removeEventListener("abort", onAbort);
      if (error) reject(error);
      else resolve(value!);
    };
    const client = http.request({
      socketPath: SOCKET_PATH,
      path: requestPath,
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": body.byteLength,
        Accept: "application/json",
      },
    }, (response) => {
      const parts: Buffer[] = [];
      let size = 0;
      response.on("data", (part: Buffer) => {
        size += part.byteLength;
        if (size > MAX_RESPONSE_BYTES) {
          client.destroy(new Error("knowledge broker response exceeds 1 MiB"));
          return;
        }
        parts.push(part);
      });
      response.on("error", (error) => finish(error));
      response.on("aborted", () => finish(new Error("knowledge broker response aborted")));
      response.on("end", () => {
        if (settled) return;
        if (response.statusCode !== 200) {
          const category = response.statusCode === 403 ? "authorization denied" :
            response.statusCode && response.statusCode >= 500 ? "service failure" :
            "request or protocol failure";
          finish(new Error(`knowledge broker ${category}: HTTP ${response.statusCode ?? "unknown"}`));
          return;
        }
        try {
          finish(undefined, validateResponse(JSON.parse(Buffer.concat(parts, size).toString("utf8")), request));
        } catch (error) {
          finish(error instanceof Error ? error : new Error("knowledge broker protocol error"));
        }
      });
    });
    const onAbort = () => client.destroy(new Error("knowledge query aborted"));
    const timer = setTimeout(() => client.destroy(new Error("knowledge query timed out after 10s")), TIMEOUT_MS);
    request.signal?.addEventListener("abort", onAbort, { once: true });
    client.on("error", (error) => finish(error));
    if (request.signal?.aborted) onAbort();
    else client.end(body);
  });
}
