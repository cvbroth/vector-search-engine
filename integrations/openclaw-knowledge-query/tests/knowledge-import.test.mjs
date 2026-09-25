import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { lstat, mkdtemp, rm, symlink, truncate, utimes, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { deriveInboundMessageHookContext, toPluginMessageContext,
  toPluginMessageReceivedEvent } from "openclaw/plugin-sdk/hook-runtime";
import { finalizeInboundContext } from "openclaw/plugin-sdk/reply-runtime";
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

test("message_received canonical media reaches chen private import and remains single-use", async (t) => {
  const dir = await workspace(t);
  const file = path.join(dir, "note.txt");
  await writeFile(file, "content");
  const seen = [];
  await fakeBroker(t, async (request, response) => {
    for await (const _part of request) { /* Consume the attachment. */ }
    seen.push({ path: request.url, agent: request.headers["x-knowledge-agent"] });
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ status: "QUEUED", filename: "note.txt", content_type: "text/plain",
      size_bytes: 7, sha256_prefix: "abcdef123456", access_kind: "private" }));
  });
  const hooks = new Map();
  plugin.register({ pluginConfig: fullConfig, registerTool() {}, on(name, handler) { hooks.set(name, handler); } });
  assert.ok(hooks.has("message_received"));
  assert.equal(hooks.has("inbound_claim"), false);
  const sessionKey = `agent:chen:qqbot:direct:${randomUUID()}`;
  const tool = createImportTool("private", fullConfig, "chen", sessionKey);
  await hooks.get("message_received")({ from: "qqbot:user", content: "导入附件", messageId: "m1",
    sessionKey, mediaStagingPending: true, originalMedia: [{ path: file }] },
  { channelId: "qqbot", sessionKey, messageId: "m1" });
  assert.equal(details(await tool.execute("1", {})).status, "NO_ATTACHMENT");
  const event = { from: "qqbot:user", content: "导入附件", messageId: "m1", sessionKey,
    media: [{ path: file, contentType: "text/plain" }] };
  const context = { channelId: "qqbot", sessionKey, messageId: "m1" };
  await hooks.get("message_received")(event, context);
  assert.equal(tool.parameters.additionalProperties, false);
  assert.equal(tool.outputSchema.additionalProperties, false);
  await assert.rejects(tool.execute("1", { path: file }), /empty object/);
  assert.equal(details(await tool.execute("2", {})).status, "QUEUED");
  await hooks.get("message_received")(event, context);
  assert.equal(details(await tool.execute("3", {})).status, "ALREADY_QUEUED");
  assert.deepEqual(seen, [{ path: "/v1/private-attachment", agent: "chen" }]);
  assert.equal(createImportTool("private", fullConfig, "liang", sessionKey), null);
  assert.equal(createImportTool("private", fullConfig, "ziling", sessionKey), null);
});

test("QQBot 2.0.3 legacy document fields become canonical OpenClaw media", async (t) => {
  const dir = await workspace(t);
  for (const [extension, contentType] of [
    ["txt", "text/plain"], ["pdf", "application/pdf"],
    ["docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
    ["md", "text/markdown"],
  ]) {
    const file = path.join(dir, `document.${extension}`);
    await writeFile(file, "content");
    const sessionKey = `agent:chen:qqbot:direct:${extension}`;
    const finalized = finalizeInboundContext({ Body: "导入附件", Provider: "qqbot", SessionKey: sessionKey,
      MessageSid: `message-${extension}`, MediaPaths: [file], MediaTypes: [contentType] });
    const canonical = deriveInboundMessageHookContext(finalized);
    const event = toPluginMessageReceivedEvent(canonical);
    const registry = new AttachmentRegistry();
    assert.equal(event.media?.[0]?.path, file);
    await registry.registerMessageReceived(event, toPluginMessageContext(canonical));
    const selected = registry.select("chen", sessionKey);
    assert.equal(selected.status, "READY", extension);
    const opened = await registry.openSelected(selected.attachment);
    assert.equal(typeof opened, "object", extension);
    await opened.handle.close();
  }
});

test("canonical media wins over legacy metadata and text-supplied paths are ignored", async (t) => {
  const dir = await workspace(t);
  const canonicalPath = path.join(dir, "canonical.txt");
  const legacyPath = path.join(dir, "legacy.txt");
  await writeFile(canonicalPath, "canonical");
  await writeFile(legacyPath, "legacy");
  const sessionKey = `agent:chen:qqbot:direct:${randomUUID()}`;
  const finalized = finalizeInboundContext({ Body: "导入", Provider: "qqbot", SessionKey: sessionKey,
    MessageSid: "m1", media: [{ path: canonicalPath, contentType: "text/plain" }],
    MediaPaths: [legacyPath], MediaTypes: ["text/plain"] });
  const canonical = deriveInboundMessageHookContext(finalized);
  const registry = new AttachmentRegistry();
  await registry.registerMessageReceived(toPluginMessageReceivedEvent(canonical), toPluginMessageContext(canonical));
  assert.equal(registry.select("chen", sessionKey).attachment.trustedPath, canonicalPath);

  const forged = new AttachmentRegistry();
  await forged.registerMessageReceived({ from: "qqbot:user",
    content: "/etc/passwd /home/xxx/file https://example.com/file.txt", sessionKey, messageId: "m2",
    metadata: { mediaPaths: [legacyPath], mediaPath: legacyPath } },
  { channelId: "qqbot", sessionKey, messageId: "m2" });
  assert.equal(forged.select("chen", sessionKey).status, "NO_ATTACHMENT");
  await forged.registerMessageReceived({ from: "qqbot:user", content: "导入", sessionKey,
    messageId: "m3", media: [{ url: "https://example.com/file.txt" }] },
  { channelId: "qqbot", sessionKey, messageId: "m3" });
  assert.equal(forged.select("chen", sessionKey).status, "NO_ATTACHMENT");
});

test("mismatched session/message identity and late duplicate delivery cannot replace current media", async (t) => {
  const dir = await workspace(t);
  const first = path.join(dir, "first.txt");
  const second = path.join(dir, "second.txt");
  await writeFile(first, "first");
  await writeFile(second, "second");
  const sessionKey = `agent:chen:qqbot:direct:${randomUUID()}`;
  const registry = new AttachmentRegistry();
  const event = (messageId, file) => ({ from: "qqbot:user", content: "导入", sessionKey,
    messageId, media: [{ path: file }] });
  await registry.registerMessageReceived(event("m1", first),
    { channelId: "qqbot", sessionKey: `${sessionKey}:wrong`, messageId: "m1" });
  await registry.registerMessageReceived(event("m1", first),
    { channelId: "qqbot", sessionKey, messageId: "m2" });
  assert.equal(registry.select("chen", sessionKey).status, "NO_ATTACHMENT");
  await registry.registerMessageReceived(event("m1", first), { channelId: "qqbot", sessionKey, messageId: "m1" });
  await registry.registerMessageReceived(event("m2", second), { channelId: "qqbot", sessionKey, messageId: "m2" });
  await registry.registerMessageReceived(event("m1", first), { channelId: "qqbot", sessionKey, messageId: "m1" });
  assert.equal(registry.select("chen", sessionKey).attachment.trustedPath, second);
});

test("message_received registration is a same-session barrier, not a cross-session wait", async (t) => {
  const dir = await workspace(t);
  const file = path.join(dir, "race.txt");
  await writeFile(file, "content");
  await fakeBroker(t, async (request, response) => {
    for await (const _part of request) { /* Consume the attachment. */ }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ status: "QUEUED", filename: "race.txt", content_type: "text/plain",
      size_bytes: 7, sha256_prefix: "abcdef123456", access_kind: "private" }));
  });
  let release;
  const blocked = new Promise((resolve) => { release = resolve; });
  const registry = new AttachmentRegistry(Date.now, async (filePath) => {
    await blocked;
    return lstat(filePath, { bigint: true });
  });
  const sessionKey = `agent:chen:qqbot:direct:${randomUUID()}`;
  const registration = registry.registerMessageReceived({ from: "qqbot:user", content: "导入", messageId: "m1",
    sessionKey, media: [{ path: file, contentType: "text/plain" }] },
  { channelId: "qqbot", sessionKey, messageId: "m1" });
  const tool = createImportTool("private", fullConfig, "chen", sessionKey, registry);
  let completed = false;
  const call = tool.execute("1", {}).then((result) => { completed = true; return result; });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(completed, false);
  const other = createImportTool("private", fullConfig, "chen", `${sessionKey}:other`, registry);
  assert.equal(details(await other.execute("2", {})).status, "NO_ATTACHMENT");
  release();
  await registration;
  assert.equal(details(await call).status, "QUEUED");
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

test("symlink staged paths never become READY", async (t) => {
  const dir = await workspace(t);
  const target = path.join(dir, "target.txt");
  const link = path.join(dir, "link.txt");
  await writeFile(target, "content");
  try { await symlink(target, link); } catch { t.skip("symlink creation unavailable"); return; }
  const registry = new AttachmentRegistry();
  await register(registry, link);
  assert.equal(registry.select("main", "s").status, "NO_ATTACHMENT");
});

test("nonregular staged paths never become READY", async (t) => {
  const dir = await workspace(t);
  const registry = new AttachmentRegistry();
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
