/** Minimal query-only client. No URL, TCP, file scanning, or shell execution. */
import http from "node:http";
import path from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULT_SOCKET = "/run/knowledge-base/backend.sock";
const MAX_QUERY_CHARS = 4096;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const ALLOWED_SCOPES = new Set(["chen", "family"]);

export function parseArgs(argv) {
  let query;
  let topK = 5;
  let socketPath = DEFAULT_SOCKET;
  const scopes = [];
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (typeof value !== "string") throw new Error(`Missing value for ${flag}`);
    if (flag === "--query") {
      if (query !== undefined) throw new Error("--query may appear only once");
      query = value;
    } else if (flag === "--scope") {
      scopes.push(value);
    } else if (flag === "--top-k") {
      if (!/^[0-9]+$/.test(value)) throw new Error("--top-k must be an integer from 1 to 50");
      topK = Number(value);
    } else if (flag === "--socket") {
      socketPath = value;
    } else {
      throw new Error(`Unknown option: ${flag}`);
    }
  }
  if (!query || !query.trim() || Array.from(query).length > MAX_QUERY_CHARS) {
    throw new Error("--query must contain 1 to 4096 Unicode characters");
  }
  if (!scopes.length || scopes.some((scope) => !ALLOWED_SCOPES.has(scope)) ||
      new Set(scopes).size !== scopes.length) {
    throw new Error("At least one unique --scope chen|family is required");
  }
  if (!Number.isInteger(topK) || topK < 1 || topK > 50) {
    throw new Error("--top-k must be an integer from 1 to 50");
  }
  if (!path.posix.isAbsolute(socketPath) || socketPath.includes("://")) {
    throw new Error("--socket must be an absolute local socket path, not a URL");
  }
  return { query, scopes, topK, socketPath };
}

export function buildRequest({ query, scopes, topK, socketPath }) {
  const body = JSON.stringify({ query, scopes, top_k: topK });
  const bodyBytes = Buffer.byteLength(body);
  if (bodyBytes > 64 * 1024) throw new Error("Request exceeds 64 KiB");
  return {
    options: {
      socketPath,
      path: "/v1/context",
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": bodyBytes,
        Accept: "application/json",
      },
    },
    body,
  };
}

export function validateResponse(payload, args) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
      payload.schema_version !== "1.0" || payload.query !== args.query ||
      !Array.isArray(payload.scopes) ||
      JSON.stringify(payload.scopes) !== JSON.stringify(args.scopes) ||
      !["ACCEPT", "UNCERTAIN", "REJECT"].includes(payload.retrieval_status) ||
      !Array.isArray(payload.evidence) ||
      !Number.isInteger(payload.evidence_count) ||
      payload.evidence_count !== payload.evidence.length ||
      payload.evidence_count > args.topK ||
      (payload.retrieval_status === "REJECT" && payload.evidence_count !== 0) ||
      (payload.retrieval_status !== "REJECT" && payload.evidence_count === 0)) {
    throw new Error("Invalid RAG Context response from service");
  }
  return payload;
}

export function queryContext(args) {
  const { options, body } = buildRequest(args);
  return new Promise((resolve, reject) => {
    const request = http.request(options, (response) => {
      const parts = [];
      let bytes = 0;
      response.on("data", (part) => {
        bytes += part.length;
        if (bytes > MAX_RESPONSE_BYTES) {
          request.destroy(new Error("Response exceeds 2 MiB"));
          return;
        }
        parts.push(part);
      });
      response.on("end", () => {
        let payload;
        try {
          payload = JSON.parse(Buffer.concat(parts).toString("utf8"));
        } catch (error) {
          reject(new Error(`Invalid service JSON: ${error.message}`));
          return;
        }
        if (response.statusCode !== 200) {
          const message = payload?.error?.message ?? "service error";
          reject(new Error(`HTTP ${response.statusCode}: ${message}`));
          return;
        }
        try {
          resolve(validateResponse(payload, args));
        } catch (error) {
          reject(error);
        }
      });
    });
    request.setTimeout(60_000, () => request.destroy(new Error("Service timed out")));
    request.on("error", reject);
    request.end(body);
  });
}

async function main() {
  try {
    const args = parseArgs(process.argv.slice(2));
    const payload = await queryContext(args);
    process.stdout.write(`${JSON.stringify(payload)}\n`);
  } catch (error) {
    process.stderr.write(`Knowledge query failed: ${error.message}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await main();
}
