import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, readFile, rm, symlink, truncate, utimes, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import plugin, { createImportTool } from "../dist/index.js";
import { AttachmentRegistry } from "../dist/attachment-registry.js";

const fullConfig = { agents: {}, imports: {
  main: { private: true, shared: true }, chen: { private: true, shared: true },
  liang: { private: false, shared: true }, ziling: { private: false, shared: true },
} };

async function workspace(t) {
  const dir = await mkdtemp(path.join(os.tmpdir(), "kb-import-test-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  return dir;
}

async function register(registry, file, { agentId = "main", sessionKey = "s", messageId = "m", pending = false,
  media } = {}) {
  await registry.register({ messageId, media: media ?? [{ path: file, contentType: "text/plain" }],
    mediaStagingPending: pending }, { agentId, sessionKey });
}

function details(result) { return result.details; }

async function fakeBroker(t, handler) {
  const directory = await workspace(t);
  const socketPath = process.platform === "win32" ? `\\\\.\\pipe\\kb-import-test-${randomUUID()}` :
    path.join(directory, "broker.sock");
  const server = http.createServer(handler);
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(socketPath, resolve); });
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  });
  const original = http.request.bind(http);
  t.mock.method(http, "request", (options, callback) => {
    assert.equal(options.socketPath, "/run/knowledge-import-broker/import.sock");
    assert.ok(["/v1/private-attachment", "/v1/shared-attachment"].includes(options.path));
    assert.equal(options.method, "POST");
    assert.equal(options.hostname, undefined);
    return original({ ...options, socketPath }, callback);
  });
}

test("import tools are invisible unless imports is explicitly configured", () => {
  const found = new Map();
  plugin.register({ pluginConfig: { agents: {} }, registerTool(value, options) { found.set(options.name, value); }, on() {} });
  assert.equal(found.get("knowledge_import_private")({ agentId: "main", sessionKey: "s" }), null);
  assert.equal(createImportTool("private", fullConfig, "liang", "s"), null);
  assert.equal(createImportTool("private", fullConfig, "ziling", "s"), null);
  assert.equal(createImportTool("shared", fullConfig, "unknown", "s"), null);
  assert.equal(createImportTool("shared", fullConfig, "main", undefined), null);
  for (const agent of ["main", "chen"]) {
    assert.ok(createImportTool("private", fullConfig, agent, "s"));
    assert.ok(createImportTool("shared", fullConfig, agent, "s"));
  }
  assert.ok(createImportTool("shared", fullConfig, "liang", "s"));
  assert.ok(createImportTool("shared", fullConfig, "ziling", "s"));
});

test("model-facing import schema is strictly empty", () => {
  const found = new Map();
  plugin.register({ pluginConfig: fullConfig, registerTool(value, options) { found.set(options.name, value); }, on() {} });
  for (const name of ["knowledge_import_private", "knowledge_import_shared"]) {
    const tool = found.get(name)({ agentId: "main", sessionKey: "s" });
    assert.deepEqual(tool.parameters.properties, {});
    assert.equal(tool.parameters.additionalProperties, false);
    for (const forbidden of ["agent_id", "scope", "destination", "path", "url", "uploader", "socket"]) {
      assert.equal(Object.hasOwn(tool.parameters.properties, forbidden), false);
      assert.doesNotMatch(JSON.stringify(tool.outputSchema), new RegExp(`"${forbidden}"`));
    }
    assert.equal(tool.outputSchema.additionalProperties, false);
    assert.doesNotMatch(JSON.stringify(tool.outputSchema), /\/srv\/|chen|family/);
  }
});

test("public inbound_claim hook registers only staged media path", async (t) => {
  const dir = await workspace(t);
  const file = path.join(dir, "note.txt");
  await writeFile(file, "content");
  const hooks = new Map();
  plugin.register({ pluginConfig: fullConfig, registerTool() {}, on(name, handler) { hooks.set(name, handler); } });
  assert.ok(hooks.has("inbound_claim"));
  const sessionKey = randomUUID();
  await hooks.get("inbound_claim")({ messageId: "m1", mediaStagingPending: true,
    originalMedia: [{ path: file }] }, { agentId: "main", sessionKey });
  const tool = createImportTool("shared", fullConfig, "main", sessionKey);
  assert.equal(details(await tool.execute("1", {})).status, "NO_ATTACHMENT");
  await hooks.get("inbound_claim")({ messageId: "m2", media: [{ path: file, contentType: "text/plain" }] },
    { agentId: "main", sessionKey });
  assert.equal(tool.parameters.additionalProperties, false);
  assert.equal(tool.outputSchema.additionalProperties, false);
  await assert.rejects(tool.execute("1", { path: file }), /empty object/);
});

test("no attachment, pending staging, and multiple media fail closed", async (t) => {
  const dir = await workspace(t);
  const a = path.join(dir, "a.txt");
  const b = path.join(dir, "b.txt");
  await writeFile(a, "a"); await writeFile(b, "b");
  const registry = new AttachmentRegistry();
  const tool = createImportTool("shared", fullConfig, "main", "s", registry);
  assert.equal(details(await tool.execute("1", {})).status, "NO_ATTACHMENT");
  await register(registry, a, { pending: true });
  assert.equal(details(await tool.execute("2", {})).status, "NO_ATTACHMENT");
  await register(registry, a, { messageId: "m2", media: [{ path: a }, { path: b }] });
  assert.equal(details(await tool.execute("3", {})).status, "SELECTION_REQUIRED");
});

test("registry isolates agent and session and ignores missing trusted identity", async (t) => {
  const dir = await workspace(t);
  const file = path.join(dir, "note.txt");
  await writeFile(file, "content");
  const registry = new AttachmentRegistry();
  await registry.register({ media: [{ path: file }] }, { sessionKey: "s" });
  assert.equal(registry.select("main", "s").status, "NO_ATTACHMENT");
  await register(registry, file, { agentId: "main", sessionKey: "s" });
  assert.equal(registry.select("main", "s").status, "READY");
  assert.equal(registry.select("chen", "s").status, "NO_ATTACHMENT");
  assert.equal(registry.select("main", "other").status, "NO_ATTACHMENT");
});

test("symlink and nonregular staged paths never become READY", async (t) => {
  const dir = await workspace(t);
  const target = path.join(dir, "target.txt");
  const link = path.join(dir, "link.txt");
  await writeFile(target, "content");
  try { await symlink(target, link); } catch { t.skip("symlink creation unavailable"); return; }
  const registry = new AttachmentRegistry();
  await register(registry, link);
  assert.equal(registry.select("main", "s").status, "NO_ATTACHMENT");
  await register(registry, dir, { messageId: "dir" });
  assert.equal(registry.select("main", "s").status, "NO_ATTACHMENT");
});

test("inode, size, and mtime changes are rejected before transfer", async (t) => {
  const dir = await workspace(t);
  for (const kind of ["inode", "size", "mtime"]) {
    const file = path.join(dir, `${kind}.txt`);
    await writeFile(file, "content");
    const registry = new AttachmentRegistry();
    await register(registry, file);
    if (kind === "inode") { await rm(file); await writeFile(file, "content"); }
    if (kind === "size") await writeFile(file, "longer content");
    if (kind === "mtime") await utimes(file, new Date(1), new Date(1));
    const selected = registry.select("main", "s");
    assert.equal(selected.status, "READY");
    assert.equal(await registry.openSelected(selected.attachment), "ATTACHMENT_CHANGED");
  }
});

test("unsupported extension and oversized attachment are rejected", async (t) => {
  const dir = await workspace(t);
  const registry = new AttachmentRegistry();
  const bad = path.join(dir, "bad.exe");
  await writeFile(bad, "content");
  await register(registry, bad);
  let selected = registry.select("main", "s");
  assert.equal(await registry.openSelected(selected.attachment), "UNSUPPORTED_TYPE");
  const huge = path.join(dir, "huge.txt");
  await writeFile(huge, "x");
  await truncate(huge, 100 * 1024 * 1024 + 1);
  await register(registry, huge, { messageId: "huge" });
  selected = registry.select("main", "s");
  assert.equal(await registry.openSelected(selected.attachment), "TOO_LARGE");
});

test("raw bytes stream to fixed Unix broker and duplicate call cannot queue again", async (t) => {
  const dir = await workspace(t);
  const file = path.join(dir, "note.txt");
  await writeFile(file, "raw attachment bytes");
  const seen = [];
  await fakeBroker(t, async (request, response) => {
    const parts = [];
    for await (const part of request) parts.push(part);
    seen.push({ body: Buffer.concat(parts).toString("utf8"), headers: request.headers, path: request.url });
    const result = { status: "QUEUED", filename: "queued.txt", content_type: "text/plain",
      size_bytes: 20, sha256_prefix: "abcdef123456", access_kind: "shared" };
    response.writeHead(200, { "Content-Type": "application/json" }); response.end(JSON.stringify(result));
  });
  const registry = new AttachmentRegistry();
  await register(registry, file);
  const tool = createImportTool("shared", fullConfig, "main", "s", registry);
  assert.equal(details(await tool.execute("1", {})).status, "QUEUED");
  assert.equal(details(await tool.execute("2", {})).status, "ALREADY_QUEUED");
  assert.equal(seen.length, 1);
  assert.equal(seen[0].body, "raw attachment bytes");
  assert.equal(seen[0].path, "/v1/shared-attachment");
  assert.equal(seen[0].headers["x-knowledge-agent"], "main");
  assert.equal(seen[0].headers["x-knowledge-filename"], "note.txt");
  assert.equal(seen[0].headers["content-length"], "20");
  assert.doesNotMatch(JSON.stringify(details(await tool.execute("3", {}))), /\/run\/|\/srv\/|trustedPath/);
});

test("broker 5xx and abort never become QUEUED", async (t) => {
  const dir = await workspace(t);
  const file = path.join(dir, "note.txt");
  await writeFile(file, "content");
  await fakeBroker(t, (_request, response) => { response.writeHead(500); response.end("failure"); });
  const registry = new AttachmentRegistry();
  await register(registry, file);
  const tool = createImportTool("shared", fullConfig, "main", "s", registry);
  assert.equal(details(await tool.execute("1", {})).status, "BROKER_ERROR");
  assert.equal(details(await tool.execute("2", {})).status, "ALREADY_QUEUED");
  const another = new AttachmentRegistry();
  await register(another, file);
  const aborted = new AbortController(); aborted.abort();
  const otherTool = createImportTool("shared", fullConfig, "main", "s", another);
  assert.equal(details(await otherTool.execute("3", {}, aborted.signal)).status, "BROKER_ERROR");
});

test("aborting an in-flight import keeps the attachment from auto-requeue", async (t) => {
  const dir = await workspace(t);
  const file = path.join(dir, "note.txt");
  await writeFile(file, "content");
  await fakeBroker(t, async (request, _response) => {
    for await (const _part of request) { /* Delay the response until cancellation. */ }
  });
  const registry = new AttachmentRegistry();
  await register(registry, file);
  const tool = createImportTool("shared", fullConfig, "main", "s", registry);
  const controller = new AbortController();
  const pending = tool.execute("1", {}, controller.signal);
  setTimeout(() => controller.abort(), 30);
  assert.equal(details(await pending).status, "BROKER_ERROR");
  assert.equal(details(await tool.execute("2", {})).status, "ALREADY_QUEUED");
});

test("broker size rejection is distinct from a queued file", async (t) => {
  const dir = await workspace(t);
  const file = path.join(dir, "note.txt");
  await writeFile(file, "content");
  await fakeBroker(t, async (request, response) => {
    for await (const _part of request) { /* Consume body. */ }
    response.writeHead(413); response.end("too large");
  });
  const registry = new AttachmentRegistry();
  await register(registry, file);
  const tool = createImportTool("shared", fullConfig, "main", "s", registry);
  assert.equal(details(await tool.execute("1", {})).status, "TOO_LARGE");
  assert.equal(details(await tool.execute("2", {})).status, "TOO_LARGE");
});

test("broker cannot smuggle a host path into filename output", async (t) => {
  const dir = await workspace(t);
  const file = path.join(dir, "note.txt");
  await writeFile(file, "content");
  await fakeBroker(t, async (request, response) => {
    for await (const _part of request) { /* Consume body. */ }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ status: "QUEUED", filename: "/srv/storage/ai-inbox/secret.txt",
      content_type: "text/plain", size_bytes: 7, sha256_prefix: "abcdef123456", access_kind: "shared" }));
  });
  const registry = new AttachmentRegistry();
  await register(registry, file);
  const tool = createImportTool("shared", fullConfig, "main", "s", registry);
  assert.deepEqual(details(await tool.execute("1", {})), { status: "BROKER_ERROR" });
});

test("registry TTL and session cap bound retained state", async (t) => {
  const dir = await workspace(t);
  const file = path.join(dir, "note.txt");
  await writeFile(file, "content");
  let clock = 0;
  const registry = new AttachmentRegistry(() => clock);
  for (let index = 0; index < 101; index++) {
    clock = index;
    await register(registry, file, { sessionKey: `session-${index}` });
  }
  assert.equal(registry.select("main", "session-0").status, "NO_ATTACHMENT");
  assert.equal(registry.select("main", "session-100").status, "READY");
  clock += 31 * 60 * 1000;
  assert.equal(registry.select("main", "session-100").status, "NO_ATTACHMENT");
});

test("per-session attachment cap never silently chooses one from an overflow", async (t) => {
  const dir = await workspace(t);
  const file = path.join(dir, "note.txt");
  await writeFile(file, "content");
  const registry = new AttachmentRegistry();
  await register(registry, file, { media: Array.from({ length: 9 }, () => ({ path: file })) });
  assert.equal(registry.select("main", "s").status, "SELECTION_REQUIRED");
});
