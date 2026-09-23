import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import plugin, { createKnowledgeTool } from "../dist/index.js";
import { queryKnowledgeBase } from "../dist/unix-client.js";

const config = {
  agentScopes: {
    privateAgent: ["chen", "family"],
    sharedAgent: ["family"],
  },
};
const fixedSocket = "/run/knowledge-base/kb.sock";

function evidence(decision = "ACCEPT", scope = "family") {
  return {
    scope,
    rank: 1,
    fused_score: 0.032787,
    semantic_score: decision === "ACCEPT" ? 0.75 : 0.60,
    semantic_distance: decision === "ACCEPT" ? 0.25 : 0.40,
    lexical_match: true,
    lexical_score: -0.00001,
    relevance_decision: decision,
    source_path: `/srv/storage/knowledge/${scope === "chen" ? "private/chen" : "shared/family"}/test.md`,
    filename: "test.md",
    page: null,
    chunk_index: 0,
    text: "完整 chunk 文本",
  };
}

function context(status = "ACCEPT", query = "问题", scopes = ["family"]) {
  const items = status === "REJECT" ? [] : [evidence(status, scopes[0])];
  return {
    schema_version: "1.0",
    query,
    scopes,
    retrieval_status: status,
    evidence_count: items.length,
    evidence: items,
  };
}

function runtimeFactory(pluginConfig = config) {
  let factory;
  plugin.register({
    pluginConfig,
    registerTool(value, options) {
      assert.equal(options.name, "knowledge_query");
      factory = value;
    },
  });
  assert.equal(typeof factory, "function");
  return factory;
}

async function fakeService(t, handler) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "kb-tool-test-"));
  const socketPath = process.platform === "win32"
    ? `\\\\.\\pipe\\kb-tool-test-${randomUUID()}`
    : path.join(directory, "kb.sock");
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
    calls += 1;
    assert.equal(options.socketPath, fixedSocket);
    assert.equal(options.path, "/v1/context");
    assert.equal(options.method, "POST");
    assert.equal(options.hostname, undefined);
    assert.equal(options.port, undefined);
    return realRequest({ ...options, socketPath }, callback);
  });
  return { get calls() { return calls; } };
}

async function sendJson(response, payload, statusCode = 200) {
  response.writeHead(statusCode, { "Content-Type": "application/json" });
  response.end(JSON.stringify(payload));
}

test("factory denies missing, unknown, and invalidly configured agents", () => {
  const factory = runtimeFactory();
  assert.equal(factory({}), null);
  assert.equal(factory({ agentId: "unknownAgent" }), null);
  assert.equal(factory({ agentId: "sharedAgent" }).name, "knowledge_query");
  assert.equal(runtimeFactory({ agentScopes: { bad: ["liang"] } })({ agentId: "bad" }), null);
  assert.equal(runtimeFactory({ agentScopes: {} })({ agentId: "sharedAgent" }), null);
});

test("runtime schema lists only authorized scopes", () => {
  const factory = runtimeFactory();
  assert.deepEqual(factory({ agentId: "sharedAgent" }).parameters.properties.scopes.items.enum,
    ["family"]);
  assert.deepEqual(factory({ agentId: "privateAgent" }).parameters.properties.scopes.items.enum,
    ["chen", "family"]);
});

test("ACCEPT, UNCERTAIN, and empty REJECT retain RAG Context status", async (t) => {
  for (const status of ["ACCEPT", "UNCERTAIN", "REJECT"]) {
    await t.test(status, async (child) => {
      const transport = await fakeService(child, (request, response) => {
        assert.equal(request.url, "/v1/context");
        assert.equal(request.method, "POST");
        sendJson(response, context(status));
      });
      const result = await createKnowledgeTool(config, "sharedAgent")
        .execute("call-1", { query: "问题", scopes: ["family"], top_k: 3 });
      assert.equal(result.details.retrieval_status, status);
      assert.equal(result.details.evidence_count, status === "REJECT" ? 0 : 1);
      assert.deepEqual(JSON.parse(result.content[0].text), result.details);
      assert.equal(transport.calls, 1);
    });
  }
});

test("family-only agent may query family but not chen", async (t) => {
  let received;
  const transport = await fakeService(t, async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    sendJson(response, context("ACCEPT", received.query, received.scopes));
  });
  const tool = createKnowledgeTool(config, "sharedAgent");
  await tool.execute("call-1", { query: "问题", scopes: ["family"] });
  assert.deepEqual(received, { query: "问题", scopes: ["family"], top_k: 5 });
  await assert.rejects(tool.execute("call-2", { query: "问题", scopes: ["chen"] }),
    /requested scope is not authorized for this agent/);
  assert.equal(transport.calls, 1);
});

test("real RAG Context nullable score fields and PDF page fit the output contract", async (t) => {
  const payload = context("UNCERTAIN");
  Object.assign(payload.evidence[0], {
    semantic_score: null,
    semantic_distance: null,
    lexical_match: false,
    lexical_score: null,
    page: 7,
  });
  await fakeService(t, (_request, response) => sendJson(response, payload));
  const result = await createKnowledgeTool(config, "sharedAgent")
    .execute("call-1", { query: "问题", scopes: ["family"] });
  assert.equal(result.details.evidence[0].page, 7);
  assert.equal(result.details.evidence[0].semantic_score, null);
});

test("agent authorized for both scopes can request each and both", async (t) => {
  const received = [];
  await fakeService(t, async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    received.push(body.scopes);
    sendJson(response, context("ACCEPT", body.query, body.scopes));
  });
  const tool = createKnowledgeTool(config, "privateAgent");
  for (const scopes of [["chen"], ["family"], ["chen", "family"]]) {
    await tool.execute("call-1", { query: "问题", scopes });
  }
  assert.deepEqual(received, [["chen"], ["family"], ["chen", "family"]]);
});

test("invalid scope sets, top_k, query, or extra transport options fail before I/O", async (t) => {
  const transport = await fakeService(t, (_request, response) => sendJson(response, context()));
  const tool = createKnowledgeTool(config, "privateAgent");
  const bad = [
    [{ query: "问题", scopes: ["liang"] }, /scopes/],
    [{ query: "问题", scopes: [] }, /scopes/],
    [{ query: "问题", scopes: ["family", "family"] }, /scopes/],
    [{ query: "问题", scopes: ["family"], top_k: 0 }, /top_k/],
    [{ query: "问题", scopes: ["family"], top_k: 11 }, /top_k/],
    [{ query: "问题", scopes: ["family"], top_k: 1.5 }, /top_k/],
    [{ query: "", scopes: ["family"] }, /query/],
    [{ query: "字".repeat(4097), scopes: ["family"] }, /query/],
    [{ query: "问题", scopes: ["family"], socketPath: "/tmp/other.sock" }, /unknown parameter/],
    [{ query: "问题", scopes: ["family"], url: "https://example.com" }, /unknown parameter/],
  ];
  for (const [params, message] of bad) {
    await assert.rejects(tool.execute("call-1", params), message);
  }
  assert.equal(transport.calls, 0);
});

test("malformed JSON, wrong schema, invalid evidence, and mismatched count fail", async (t) => {
  const responses = [
    "not-json",
    JSON.stringify({ ...context(), schema_version: "9.9" }),
    JSON.stringify({ ...context(), evidence_count: 2 }),
    JSON.stringify({ ...context(), evidence: [{ ...evidence(), relevance_decision: "REJECT" }] }),
    JSON.stringify({ ...context(), unexpected: true }),
    JSON.stringify({ ...context(), query: "other question" }),
  ];
  let index = 0;
  await fakeService(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(responses[index++]);
  });
  for (const _ of responses) {
    await assert.rejects(queryKnowledgeBase({ query: "问题", scopes: ["family"], topK: 3 }),
      /protocol error|JSON/);
  }
});

test("HTTP 400 and 503 remain tool failures, never REJECT", async (t) => {
  let status = 400;
  await fakeService(t, (_request, response) => sendJson(response, { error: "failure" }, status));
  await assert.rejects(queryKnowledgeBase({ query: "问题", scopes: ["family"], topK: 3 }),
    /request or protocol failure: HTTP 400/);
  status = 503;
  await assert.rejects(queryKnowledgeBase({ query: "问题", scopes: ["family"], topK: 3 }),
    /service failure: HTTP 503/);
});

test("timeout aborts a silent service after approximately 10 seconds", async (t) => {
  await fakeService(t, () => {});
  const started = Date.now();
  await assert.rejects(queryKnowledgeBase({ query: "问题", scopes: ["family"], topK: 3 }),
    /timed out after 10s/);
  assert.ok(Date.now() - started >= 9_000);
});

test("AbortSignal cancels an in-flight request", async (t) => {
  await fakeService(t, () => {});
  const controller = new AbortController();
  const pending = queryKnowledgeBase({
    query: "问题", scopes: ["family"], topK: 3, signal: controller.signal,
  });
  controller.abort();
  await assert.rejects(pending, /aborted/);
});

test("responses over 1 MiB are aborted before Buffer.concat", async (t) => {
  await fakeService(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end("x".repeat(1024 * 1024 + 1));
  });
  await assert.rejects(queryKnowledgeBase({ query: "问题", scopes: ["family"], topK: 3 }),
    /exceeds 1 MiB/);
});

test("missing Unix socket is a transport error", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "kb-tool-missing-"));
  const nonexistent = process.platform === "win32"
    ? `\\\\.\\pipe\\kb-tool-missing-${randomUUID()}`
    : path.join(directory, "missing.sock");
  t.after(() => rm(directory, { recursive: true, force: true }));
  const realRequest = http.request.bind(http);
  t.mock.method(http, "request", (options, callback) => {
    assert.equal(options.socketPath, fixedSocket);
    return realRequest({ ...options, socketPath: nonexistent }, callback);
  });
  await assert.rejects(queryKnowledgeBase({ query: "问题", scopes: ["family"], topK: 3 }),
    /ENOENT|ECONNREFUSED/);
});

test("metadata owns knowledge_query; runtime contains no shell or arbitrary URL client", async () => {
  const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
  const manifest = JSON.parse(await readFile(path.join(root, "openclaw.plugin.json"), "utf8"));
  assert.equal(manifest.id, "local-knowledge-query");
  assert.deepEqual(manifest.contracts.tools, ["knowledge_query"]);
  for (const filename of ["index.ts", "unix-client.ts"]) {
    const source = await readFile(path.join(root, "src", filename), "utf8");
    assert.doesNotMatch(source, /child_process|\bexecFile\b|\bspawn\s*\(|\bexec\s*\(/);
    assert.doesNotMatch(source, /https?:\/\//);
  }
});
