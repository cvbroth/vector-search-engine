/** Fixed local Unix socket transport; no URL or socket-path input exists. */
import http from "node:http";
import Value from "typebox/value";
import { ragContextSchema } from "./contracts.js";
const SOCKET_PATH = "/run/knowledge-base/kb.sock";
const REQUEST_PATH = "/v1/context";
const TIMEOUT_MS = 10_000;
const MAX_RESPONSE_BYTES = 1024 * 1024;
function validateResponse(value, request) {
    if (!Value.Check(ragContextSchema, value)) {
        throw new Error("knowledge service protocol error: invalid RAG Context schema");
    }
    const context = value;
    if (context.query !== request.query ||
        context.scopes.length !== request.scopes.length ||
        context.scopes.some((scope, index) => scope !== request.scopes[index]) ||
        context.evidence_count !== context.evidence.length ||
        context.evidence_count > request.topK ||
        context.evidence.some((item, index) => item.rank !== index + 1 || !request.scopes.includes(item.scope))) {
        throw new Error("knowledge service protocol error: response does not match request");
    }
    const hasAccept = context.evidence.some((item) => item.relevance_decision === "ACCEPT");
    const expectedStatus = context.evidence_count === 0
        ? "REJECT"
        : hasAccept ? "ACCEPT" : "UNCERTAIN";
    if (context.retrieval_status !== expectedStatus) {
        throw new Error("knowledge service protocol error: inconsistent retrieval status");
    }
    return context;
}
export function queryKnowledgeBase(request) {
    if (request.signal?.aborted) {
        return Promise.reject(new Error("knowledge query aborted"));
    }
    const body = Buffer.from(JSON.stringify({
        query: request.query,
        scopes: request.scopes,
        top_k: request.topK,
    }), "utf8");
    return new Promise((resolve, reject) => {
        let settled = false;
        const finish = (error, value) => {
            if (settled)
                return;
            settled = true;
            clearTimeout(timer);
            request.signal?.removeEventListener("abort", onAbort);
            if (error)
                reject(error);
            else
                resolve(value);
        };
        const client = http.request({
            socketPath: SOCKET_PATH,
            path: REQUEST_PATH,
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Content-Length": body.byteLength,
                Accept: "application/json",
            },
        }, (response) => {
            const parts = [];
            let size = 0;
            response.on("data", (part) => {
                size += part.byteLength;
                if (size > MAX_RESPONSE_BYTES) {
                    client.destroy(new Error("knowledge service response exceeds 1 MiB"));
                    return;
                }
                parts.push(part);
            });
            response.on("error", (error) => finish(error));
            response.on("aborted", () => finish(new Error("knowledge service response aborted")));
            response.on("end", () => {
                if (settled)
                    return;
                if (response.statusCode !== 200) {
                    const category = response.statusCode && response.statusCode >= 500
                        ? "service failure" : "request or protocol failure";
                    finish(new Error(`knowledge ${category}: HTTP ${response.statusCode ?? "unknown"}`));
                    return;
                }
                let value;
                try {
                    value = JSON.parse(Buffer.concat(parts, size).toString("utf8"));
                    finish(undefined, validateResponse(value, request));
                }
                catch (error) {
                    finish(error instanceof Error ? error : new Error("knowledge service protocol error"));
                }
            });
        });
        const onAbort = () => client.destroy(new Error("knowledge query aborted"));
        const timer = setTimeout(() => client.destroy(new Error("knowledge query timed out after 10s")), TIMEOUT_MS);
        request.signal?.addEventListener("abort", onAbort, { once: true });
        client.on("error", (error) => finish(error));
        if (request.signal?.aborted)
            onAbort();
        else
            client.end(body);
    });
}
