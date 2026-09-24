import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import plugin, { createKnowledgeTool } from "../dist/index.js";
import { queryBroker } from "../dist/unix-client.js";

const config = { agents: {
  chenAgent: { private: true, shared: true },
  sharedAgent: { private: false, shared: true },
} };
const fixedSocket = "/run/knowledge-broker/query.sock";

function context(status = "ACCEPT", query = "问题") {
  const evidence = status === "REJECT" ? [] : [{
    rank: 1, fused_score: 0.032787, semantic_score: 0.75,
    semantic_distance: 0.25, lexical_match: true, lexical_score: -0.00001,
    relevance_decision: status,
    filename: "test.md", page: null, chunk_index: 0, text: "完整 chunk 文本",
  }];
  return { schema_version: "1.0", query, retrieval_status: status,
    evidence_count: evidence.length, evidence };
}

function factories(pluginConfig = config) {
  const found = new Map();
  plugin.register({ pluginConfig, registerTool(value, options) { found.set(options.name, value); }, on() {} });
  return found;
}

async function fakeBroker(t, handler) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "kb-broker-test-"));
  const socketPath = process.platform === "win32"
    ? `\\\\.\\pipe\\kb-broker-test-${randomUUID()}` : path.join(directory, "broker.sock");
  const server = http.createServer(handler);
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  });
  const realRequest = http.request.bind(http);
  let calls = 0;
  t.mock.method(http, "request", (options, callback) => {
    calls++;
    assert.equal(options.socketPath, fixedSocket);
    assert.ok(["/v1/private-context", "/v1/shared-context"].includes(options.path));
    assert.equal(options.method, "POST");
    assert.equal(options.hostname, undefined);
    return realRequest({ ...options, socketPath }, callback);
  });
  return { get calls() { return calls; } };
}

async function bodyOf(request) {
  const chunks = [];
  for await (const part of request) chunks.push(part);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}
function send(response, payload, status = 200) {
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(JSON.stringify(payload));
}

test("query schemas remain limited to query and top_k", () => {
  const found = factories();
  assert.deepEqual([...found.keys()], ["knowledge_private", "knowledge_shared", "knowledge_import_private", "knowledge_import_shared"]);
  for (const name of ["knowledge_private", "knowledge_shared"]) {
    const factory = found.get(name);
    const tool = factory({ agentId: "chenAgent" });
    assert.deepEqual(Object.keys(tool.parameters.properties), ["query", "top_k"]);
    for (const forbidden of ["agent_id", "agentId", "scope", "scopes", "database", "source_path", "socketPath", "url", "host", "port"]) {
      assert.equal(Object.hasOwn(tool.parameters.properties, forbidden), false);
    }
    assert.equal(tool.parameters.additionalProperties, false);
    assert.doesNotMatch(JSON.stringify(tool.outputSchema), /\b(?:scope|scopes|source_path|chen|family)\b/);
    assert.equal(tool.outputSchema.additionalProperties, false);
    assert.equal(tool.outputSchema.properties.evidence.items.additionalProperties, false);
    assert.doesNotMatch(tool.description, /\b(?:chen|family)\b/);
  }
});

test("factory hides absent or unauthorized capabilities", () => {
  const found = factories();
  assert.equal(found.get("knowledge_private")({ agentId: "sharedAgent" }), null);
  assert.ok(found.get("knowledge_shared")({ agentId: "sharedAgent" }));
  assert.equal(found.get("knowledge_shared")({}), null);
  assert.equal(found.get("knowledge_private")({ agentId: "unknown" }), null);
  assert.equal(createKnowledgeTool("private", { agents: { bad: { private: "yes", shared: true } } }, "bad"), null);
});

test("trusted agent identity and fixed access route reach broker without scope", async (t) => {
  const seen = [];
  await fakeBroker(t, async (request, response) => {
    const body = await bodyOf(request);
    seen.push({ path: request.url, body });
    send(response, context("ACCEPT", body.query));
  });
  const found = factories();
  const privateTool = found.get("knowledge_private")({ agentId: "chenAgent" });
  const sharedTool = found.get("knowledge_shared")({ agentId: "chenAgent" });
  const privateResult = await privateTool.execute("1", { query: "问题", top_k: 3 });
  const sharedResult = await sharedTool.execute("2", { query: "问题" });
  for (const result of [privateResult, sharedResult]) {
    assert.equal(result.details.retrieval_status, "ACCEPT");
    for (const forbidden of ["scope", "scopes", "source_path"]) {
      assert.equal(Object.hasOwn(result.details, forbidden), false);
      assert.equal(Object.hasOwn(result.details.evidence[0], forbidden), false);
      assert.equal(Object.hasOwn(JSON.parse(result.content[0].text), forbidden), false);
    }
  }
  assert.deepEqual(seen, [
    { path: "/v1/private-context", body: { agent_id: "chenAgent", query: "问题", top_k: 3 } },
    { path: "/v1/shared-context", body: { agent_id: "chenAgent", query: "问题", top_k: 5 } },
  ]);
});

test("ACCEPT, UNCERTAIN, and empty REJECT preserve RAG Context", async (t) => {
  let status = "ACCEPT";
  await fakeBroker(t, (_request, response) => send(response, context(status)));
  const tool = createKnowledgeTool("shared", config, "sharedAgent");
  for (status of ["ACCEPT", "UNCERTAIN", "REJECT"]) {
    const result = await tool.execute("1", { query: "问题" });
    assert.equal(result.details.retrieval_status, status);
    assert.equal(result.details.evidence_count, status === "REJECT" ? 0 : 1);
    assert.deepEqual(JSON.parse(result.content[0].text), result.details);
    assert.doesNotMatch(result.content[0].text, /"(?:scope|scopes|source_path)"\s*:/);
  }
});

test("model cannot inject agent, scope, path, URL, or bad arguments", async (t) => {
  const transport = await fakeBroker(t, (_request, response) => send(response, context()));
  const tool = createKnowledgeTool("shared", config, "sharedAgent");
  for (const params of [
    { query: "问题", agent_id: "chenAgent" }, { query: "问题", agentId: "chenAgent" },
    { query: "问题", scope: "chen" }, { query: "问题", scopes: ["chen"] },
    { query: "问题", database: "/tmp/other.db" }, { query: "问题", socketPath: "/tmp/x" },
    { query: "问题", url: "https://example.com" }, { query: "" },
    { query: "字".repeat(4097) }, { query: "问题", top_k: 0 },
    { query: "问题", top_k: 11 }, { query: "问题", top_k: 1.5 },
  ]) await assert.rejects(tool.execute("1", params));
  assert.equal(transport.calls, 0);
});

test("malformed or inconsistent broker responses are errors", async (t) => {
  const badEvidence = (changes) => JSON.stringify({
    ...context(), evidence: [{ ...context().evidence[0], ...changes }],
  });
  const responses = [
    "not-json", JSON.stringify({ ...context(), schema_version: "9.9" }),
    JSON.stringify({ ...context(), evidence_count: 2 }),
    JSON.stringify({ ...context(), query: "other" }),
    JSON.stringify({ ...context(), scopes: ["chen", "family"] }),
    badEvidence({ source_path: "/private/chen" }),
    badEvidence({ semantic_score: "0.75" }),
    badEvidence({ lexical_match: "true" }),
    badEvidence({ filename: null }),
    badEvidence({ page: 0 }),
    badEvidence({ chunk_index: -1 }),
    badEvidence({ text: null }),
    badEvidence({ rank: 2 }),
    JSON.stringify({ ...context(), evidence: [{ ...context().evidence[0], relevance_decision: "REJECT" }] }),
  ];
  let index = 0;
  await fakeBroker(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(responses[index++]);
  });
  for (const _ of responses) await assert.rejects(queryBroker({ kind: "shared", agentId: "a", query: "问题", topK: 3 }));
});

test("403 authorization is distinct from 5xx and neither becomes REJECT", async (t) => {
  let status = 403;
  await fakeBroker(t, (_request, response) => send(response, { error: "denied" }, status));
  const query = { kind: "private", agentId: "a", query: "问题", topK: 3 };
  await assert.rejects(queryBroker(query), /authorization denied: HTTP 403/);
  status = 503;
  await assert.rejects(queryBroker(query), /service failure: HTTP 503/);
});

test("timeout, cancellation, and oversized response are transport errors", async (t) => {
  await fakeBroker(t, (request, response) => {
    if (request.url === "/v1/private-context") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end("x".repeat(1024 * 1024 + 1));
    }
  });
  const base = { agentId: "a", query: "问题", topK: 3 };
  await assert.rejects(queryBroker({ ...base, kind: "private" }), /exceeds 1 MiB/);
  const controller = new AbortController();
  const pending = queryBroker({ ...base, kind: "shared", signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, /aborted/);
  const started = Date.now();
  await assert.rejects(queryBroker({ ...base, kind: "shared" }), /timed out after 10s/);
  assert.ok(Date.now() - started >= 9_000);
});

test("plugin metadata and source contain no shell or arbitrary URL client", async () => {
  const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
  const manifest = JSON.parse(await readFile(path.join(root, "openclaw.plugin.json"), "utf8"));
  assert.deepEqual(manifest.contracts.tools, ["knowledge_private", "knowledge_shared", "knowledge_import_private", "knowledge_import_shared"]);
  for (const name of ["index.ts", "unix-client.ts", "import-client.ts", "attachment-registry.ts"]) {
    const source = await readFile(path.join(root, "src", name), "utf8");
    assert.doesNotMatch(source, /child_process|\bexecFile\b|\bspawn\s*\(|\bexec\s*\(/);
  }
  const clientSource = await readFile(path.join(root, "src", "unix-client.ts"), "utf8");
  assert.doesNotMatch(clientSource, /\/run\/knowledge-base\/backend\.sock/);
});
