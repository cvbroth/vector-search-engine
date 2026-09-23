# OpenClaw 本地知识库 Tool Plugin

插件只向模型提供两个工具：`knowledge_private(query, top_k?)`（当前 Agent 的私人知识）和 `knowledge_shared(query, top_k?)`（家庭共享知识）。二者的模型参数 schema 均严格为 `query`（1–4096 个 Unicode 字符）和可选 `top_k`（1–10，默认 5），`additionalProperties=false`。不提供 `agent_id`、`agentId`、`scope`、`scopes`、数据库、文件或网络地址参数，也不再提供旧的 `knowledge_query`。

```text
模型 → knowledge_private / knowledge_shared
     → 插件从可信 toolContext.agentId 取 Agent ID
     → /run/knowledge-broker/query.sock
     → 宿主机 Broker 根据 policy 解析 scope
     → /run/knowledge-base/backend.sock
     → kb_service.py /v1/context → RAG Context JSON
```

插件只用 Node 内置 `http.request({socketPath})` 对 Broker 发请求，不调用 shell、其他命令行客户端、NAS 文件或 SQLite，也不提供 TCP/任意 URL。私人/共享分别固定调用 `POST /v1/private-context` 与 `POST /v1/shared-context`。`agent_id` 由插件从 OpenClaw 运行时上下文注入，绝不取自模型参数。

## 构建和验证

目标版本：OpenClaw 2026.9.4，Node.js 24.16+。在本目录执行：

```bash
pnpm install
pnpm run build
pnpm run test
pnpm run plugin:build
pnpm run plugin:validate
```

打包时保留 `dist/`、`package.json` 和 `openclaw.plugin.json`。本地测试与 manifest 验证不等于已在用户的 OpenClaw 容器部署或验证。

## 本地可见性与中央授权

以下是插件本地 capability 配置示例，Agent ID 应以真实运行时值为准：

```json
{
  "agents": {
    "main": {"private": true, "shared": true},
    "chen": {"private": true, "shared": true},
    "liang": {"private": false, "shared": true},
    "ziling": {"private": false, "shared": true}
  }
}
```

此对象放在 OpenClaw 的 `local-knowledge-query` 插件 `config` 内；未配置/未知的 Agent 或缺失的 `toolContext.agentId` 看不到工具。`liang` / `ziling` 只看到共享工具；`main` / `chen` 可看到两个。此配置只控制**工具可见性和第一层防误调用**，不是最终授权。宿主机的 Broker policy 才是 authoritative ACL，且可否决插件允许的请求。详见仓库根目录 README 与 `examples/knowledge-broker-policy.json`。

## 结果和错误

正常 `HTTP 200` 返回既有 RAG Context JSON。空证据、`retrieval_status=REJECT` 是一次成功检索；`403` 权限拒绝、`400` 请求错误、`5xx` 后端故障、无效协议、超时、取消或超过 1 MiB 的响应都作为工具错误，不伪装成 REJECT。插件超时为 10 秒。

`retrieval_status` 只是检索相关性，不是 answerability。`ACCEPT` 不证明文档能够回答问题，不能据此补事实；`UNCERTAIN` 仍须检查完整证据文本。

## 当前威胁模型

正常 Tool 调用时 `toolContext.agentId` 来自 OpenClaw 运行时，不是模型参数，所以模型无法通过工具参数伪造 Agent ID 或指定 `chen` 等 scope。但这些 Agent 可能运行在同一个 Gateway/容器并共享 Unix UID。如果某 Agent 拥有 arbitrary exec/code execution，它理论上能绕过插件，直接向 Broker socket 发送伪造的 `agent_id` JSON。Broker 提供中央 ACL、scope 隐藏、正常工具授权、防误调用、审计和 backend 隐藏，**不提供密码学认证的 Agent 身份或针对任意代码执行的强隔离**。需要强隔离时，未来应采用 per-agent sandbox、独立 UID/容器或 capability boundary；本项目尚未实现。

此外，Broker 与 backend 的 Unix socket 目录权限必须只授予适当进程；不要把 backend socket 暴露给 OpenClaw 容器。仓库代码没有修改任何真实服务器、OpenClaw 配置或容器。
