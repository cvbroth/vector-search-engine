# Local Knowledge Query — OpenClaw Tool Plugin

`local-knowledge-query` adds the `knowledge_query` tool:

```text
OpenClaw Agent → knowledge_query → /run/knowledge-base/kb.sock
               → POST /v1/context → kb_service.py → RAG Context JSON
```

The plugin connects directly with Node's built-in `http.request({ socketPath })`. It does **not** call a shell or `clients/openclaw_kb_client.mjs`, read NAS documents or SQLite directly, use Docker socket, accept an arbitrary URL, or listen on TCP. It does not implement answer generation or an answerability judge.

## Requirements and build

Target: OpenClaw 2026.9.4 and Node.js 24.16+ (the target container has Node 24.19.0). `typebox` is a runtime dependency; `openclaw` is a peer dependency. Run in this directory:

```bash
pnpm install
pnpm run build
pnpm run test
pnpm run plugin:build
pnpm run plugin:validate
```

`plugin:build` generates the official `openclaw.plugin.json` metadata, including `contracts.tools: ["knowledge_query"]`. Ship the built `dist/` directory together with `package.json` and the manifest. These commands are local development checks, not evidence of installation in the user's OpenClaw container.

## Agent-level authorization

Configure `agentScopes` under this plugin's OpenClaw entry. The following is an **example only**; verify the real runtime `agentId` values before deployment. No agent names are hard-coded in the source:

```json
{
  "plugins": {
    "entries": {
      "local-knowledge-query": {
        "enabled": true,
        "config": {
          "agentScopes": {
            "<verified-private-agent-id>": ["chen", "family"],
            "<verified-shared-agent-id>": ["family"]
          }
        }
      }
    }
  }
}
```

The factory reads OpenClaw's trusted `toolContext.agentId`. Missing or unconfigured IDs return `null`, so the tool is absent for that Agent. Invalid scope configuration also denies creation. The runtime parameter schema lists only that Agent's allowed scopes, and `execute()` checks the requested scopes again before any socket request. An unauthorized `chen` request fails explicitly; it is never silently reduced to `family`. Current knowledge scopes are only `chen` and `family`; `top_k` defaults to 5 and is limited to 1–10. The query limit is 4096 Unicode code points.

Unix socket file permissions remain an additional, coarser boundary. Anyone who can connect to the host service directly can currently query both scopes; `agentScopes` protects calls through this OpenClaw tool, **not** arbitrary processes with socket access. Keep the socket accessible only to trusted processes.

## Results and failures

Successful tool output preserves the existing RAG Context JSON (`schema_version: "1.0"`, query, scopes, retrieval status, count, full evidence). `REJECT` with empty evidence is a successful retrieval result. HTTP 4xx, HTTP 5xx, invalid JSON/schema, missing socket, timeout, cancellation, or a response over 1 MiB are tool errors—not `REJECT` results. The request timeout is 10 seconds.

`retrieval_status` describes retrieval relevance, not final answerability. `ACCEPT` does not prove that a document answers the question; `UNCERTAIN` still needs inspection by the upper-layer model. The model must not invent facts merely because a result is `ACCEPT`.

No server, Docker Compose, OpenClaw configuration, or NAS files are changed by this package.
