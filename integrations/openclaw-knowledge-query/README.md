# OpenClaw 本地知识库 Tool Plugin

插件保留两个查询工具：`knowledge_private(query, top_k?)`（当前 Agent 的私人知识）和 `knowledge_shared(query, top_k?)`（家庭共享知识）。二者的模型参数 schema 均严格为 `query`（1–4096 个 Unicode 字符）和可选 `top_k`（1–10，默认 5），`additionalProperties=false`。另新增两个按需排队工具 `knowledge_import_private()` / `knowledge_import_shared()`，其模型参数严格为 `{}`。不提供 `agent_id`、`agentId`、`scope`、`scopes`、数据库、文件或网络地址参数，也不再提供旧的 `knowledge_query`。

```text
模型 → knowledge_private / knowledge_shared
     → 插件从可信 toolContext.agentId 取 Agent ID
     → /run/knowledge-broker/query.sock
     → 宿主机 Broker 根据 policy 解析 scope
     → /run/knowledge-base/backend.sock
     → kb_service.py /v1/context → 内部 RAG Context
     → Broker 严格校验并去除内部 scope/path → 模型侧 Context JSON
```

查询插件只用 Node 内置 `http.request({socketPath})` 对 Query Broker 发请求，不调用 shell、其他命令行客户端、NAS 文件或 SQLite，也不提供 TCP/任意 URL。私人/共享分别固定调用 `POST /v1/private-context` 与 `POST /v1/shared-context`。`agent_id` 由插件从 OpenClaw 运行时上下文注入，绝不取自模型参数。导入工具只访问 OpenClaw 已暂存的可信附件，以及固定的 Import Broker Unix socket；不访问 NAS Inbox 或 Source。

## Knowledge Ingestion Skill

插件附带 `skills/knowledge-ingestion/SKILL.md`。从 0.2.4 起，它指导三层交互：收到附件时仅做 1–3 句初识并询问用户想如何处理；实质性讨论并完成当前任务后，才可对明显有长期价值的附件在末尾建议一次；只有用户用可信消息明确提出保存动作和私人/家庭共享目标，才调用对应导入工具。第一层不提知识库或授权，也不把上传等同于保存意图。它只指导对话，不负责存储、索引，也不赋予新权限；是否可调用工具仍由现有插件配置和 Broker 授权决定。OpenClaw 2026.9.4 会在启用插件且 Skill 符合 Agent 可见性配置时发现它；源码提交本身不等于已在真实 Gateway 加载。

当前底层 consent gate **不**组合“Agent 提议私人库”与用户随后单独回复“可以”；该回复仍得到 `CONSENT_REQUIRED`。Skill 会请用户再给出完整的“保存/导入 + 目标库”指令，不会放宽 `src/import-consent.ts` 的规则。仓库中的 Skill 测试验证文档约束与确定性授权边界，不等于已经评测真实模型每一次回复。

```text
Knowledge Ingestion Skill → knowledge_import_private/shared → Local Knowledge Query plugin
                          → Knowledge Import Broker → Importer / Indexer
Agent → knowledge_private/shared → Knowledge Broker → vector / lexical retrieval
```

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

## 可信附件导入（按需）

从 0.2.3 起，导入插件还会在写入前检查可信 `message_received` 用户消息中的明确导入指令。授权绑定当前 Agent、会话、唯一可信附件和 private/shared 目标，5 分钟内有效且开始一次导入后即消费。普通上传/讨论或“可以”“存起来”等含糊表达不会授权；此时工具返回 `CONSENT_REQUIRED`，不会连接 Import Broker，也不会把附件变为 SENDING。Agent 应请用户用完整指令明确目标，不得立即重试或改用另一导入工具。模型工具参数不能提供授权；该规则是保守的确定性匹配，不尝试理解全部自然语言。

插件通过 OpenClaw 2026.9.4 的 `message_received` 观察 Hook 捕获普通入站消息，不再依赖仅面向已绑定会话的 `inbound_claim`。只接受同一可信 OpenClaw 会话中的 canonical `event.media.path`，并核对 sessionKey / messageId；Agent 身份来自可信会话键与工具运行上下文，不取自模型参数。QQBot 2.0.3 的普通文档虽以 legacy `MediaPaths` / `MediaTypes` 提交，但 OpenClaw 2026.9.4 的入站定稿流程会将其投影为 canonical `event.media`；插件不直接解析 legacy metadata。canonical media 缺失时不会从消息文本、`originalMedia`、任意宿主路径或 URL 补取。公开媒体事实没有独立的原始文件名字段，目前以暂存路径 basename 作为文件名；若渠道暂存名不保留 `.pdf/.docx/.md/.txt` 扩展名，则拒绝而不是让模型覆写。

`message_received` 在宿主机中以 fire-and-forget 方式触发，插件会同步登记每个 Agent/会话的 pending registration；同一轮工具调用仅在本会话内有界等待最多 2 秒，避免文件尚未完成校验时误报 `NO_ATTACHMENT`。缺少可信会话键或暂存未完成时不登记 READY。Registry 是进程内状态：30 分钟 TTL，最多 100 个会话和每会话 8 个附件；Gateway 重启即失效。一次只自动选择最近一条**单附件**消息；多附件返回 `SELECTION_REQUIRED`，无附件返回 `NO_ATTACHMENT`。插件本身不会主动建议入库；这类交互由上面的 Skill 指导 Agent 决定。

导入前重新核对暂存文件的 dev/inode/size/mtime，以 `O_NOFOLLOW` 打开并 `fstat` 已打开文件；拒绝符号链接和非普通文件，仅允许 `.pdf`、`.docx`、`.md`、`.txt`，上限 100 MiB。正文以 raw HTTP body 流式发送到固定 `/run/knowledge-import-broker/import.sock`，不在 JSON 中 base64 编码。Broker 根据固定宿主机 policy 映射 `agentId` 到 NAS uploader，并只把附件排入其私人 Inbox；`QUEUED` **只表示进入异步导入队列，不代表已索引**。传输错误或响应不确定时返回 `BROKER_ERROR`，同一附件不会自动重发，需人工核对 Inbox。本次源码及本地测试不等于已在真实 QQ 会话完成端到端验证。

查询可见性仍由 `agents` 配置决定；写入工具由**独立、可选**的 `imports` 配置决定。缺少 `imports` 时两个写入工具完全不暴露，旧配置不会获得写权限。示例：

```json
{
  "agents": {
    "main": {"private": true, "shared": true},
    "chen": {"private": true, "shared": true},
    "liang": {"private": false, "shared": true},
    "ziling": {"private": false, "shared": true}
  },
  "imports": {
    "main": {"private": true, "shared": true},
    "chen": {"private": true, "shared": true},
    "liang": {"private": false, "shared": true},
    "ziling": {"private": false, "shared": true}
  }
}
```

插件配置只决定工具可见性；宿主机 Import Broker policy 才是最终授权与 `main→chen`、`ziling→azl` 等映射。禁止把 Inbox、Source、Query backend.sock 暴露给容器；只挂载必要的两个 Broker socket。中央 Import Broker 的示例 systemd 安全设置与身份边界见仓库根目录 README。

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

正常 `HTTP 200` 返回 Broker 转换后的模型侧 Context JSON。顶层字段为 `schema_version`、`query`、`retrieval_status`、`evidence_count`、`evidence`；证据保留排名、评分诊断、decision、`filename`、`page`、`chunk_index` 与完整 `text`，但没有 `scope`、`scopes` 或 `source_path`。插件对响应字段类型、结果数量、顺序和状态一致性再次验证，输出 schema 设置 `additionalProperties=false`，且不含内部 `chen`/`family` 名称。Backend 内部协议仍保留完整 RAG Context。

空证据、`retrieval_status=REJECT` 是一次成功检索；`403` 权限拒绝、`400` 请求错误、`5xx` 后端故障、无效协议、超时、取消或超过 1 MiB 的响应都作为工具错误，不伪装成 REJECT。插件超时为 10 秒。

`retrieval_status` 只是检索相关性，不是 answerability。`ACCEPT` 不证明文档能够回答问题，不能据此补事实；`UNCERTAIN` 仍须检查完整证据文本。

## 当前威胁模型

正常 Tool 调用时 `toolContext.agentId` 来自 OpenClaw 运行时，不是模型参数，所以模型无法通过工具参数伪造 Agent ID 或指定 `chen` 等 scope。但这些 Agent 可能运行在同一个 Gateway/容器并共享 Unix UID。如果某 Agent 拥有 arbitrary exec/code execution，它理论上能绕过插件，直接向 Broker socket 发送伪造的 `agent_id` JSON。Broker 提供中央 ACL、scope 隐藏、正常工具授权、防误调用、审计和 backend 隐藏，**不提供密码学认证的 Agent 身份或针对任意代码执行的强隔离**。需要强隔离时，未来应采用 per-agent sandbox、独立 UID/容器或 capability boundary；本项目尚未实现。

此外，Broker 与 backend 的 Unix socket 目录权限必须只授予适当进程；不要把 backend socket 暴露给 OpenClaw 容器。仓库代码没有修改任何真实服务器、OpenClaw 配置或容器。
