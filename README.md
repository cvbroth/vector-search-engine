# 家庭 NAS 本地知识库 V1

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `config.py` | `chen` / `family` 白名单、各自的文档/数据库/日志路径及本地 Embedding 配置 |
| `parsers.py` | 从 `.md`、`.txt`、`.pdf`、`.docx` 提取文本；PDF 保留可提取文本的页码 |
| `chunker.py` | 按标题、段落、句子组织 chunk，并保留来源元数据与 overlap |
| `embeddings.py` | 批量请求 `127.0.0.1:19433` 的既有 Embedding 服务，验证 768 维结果 |
| `database.py` | 建立 SQLite、FTS5、sqlite-vec 表；事务式写入与删除 |
| `ingest.py` | 仅扫描选定 scope，新增、跳过、重建或清理该库索引 |
| `search.py` | 单库 FTS5 + 语义检索 RRF；另提供多库 `search_scopes()` API |
| `relevance.py` | 独立的三态检索判断函数；不做答案生成或事实核验 |
| `rag_context.py` | 将现有门控检索结果转为稳定的机器可读 JSON 证据 |
| `kb_service.py` | 仅通过宿主机 Unix socket 提供只读 RAG Context 查询接口 |
| `kb_policy_broker.py` | 宿主机中央授权 Broker；按 Agent 和工具类型解析 scope 并转发到查询后端 |
| `knowledge_importer.py` | 根据独立 policy 将稳定 Inbox 文档验证、去重、发布到正式 Source，再批量调用既有增量索引 |
| `knowledge_import_broker.py` | 固定 Unix socket、宿主机 policy 授权，将可信附件字节流排队到映射用户的私人 Inbox；不解析或索引 |
| `atomic_publish.py` | 严格的 Linux `renameat2(RENAME_NOREPLACE)` 隔离封装；底层原语本身不做降级 |
| `scope_lock.py` | 为同一知识库 scope 的发布和索引提供独立文件锁 |
| `examples/knowledge-broker-policy.json` | Broker policy 示例；真实 policy 应单独放在 `/etc/knowledge-broker/policy.json` |
| `examples/knowledge-import-policy.json` | Importer policy 示例；真实账号和目录须由部署者核对 |
| `examples/knowledge-import-broker-policy.json` | 独立的 Agent→NAS uploader 与导入权限 policy 示例 |
| `clients/openclaw_kb_client.mjs` | 仅供可信宿主机诊断的旧式直接查询客户端；不可作为 Agent 授权入口 |
| `integrations/openclaw-knowledge-query/` | 仅提供 `knowledge_private` / `knowledge_shared` 的 OpenClaw Tool Plugin |
| `calibrate_relevance.py` | 用人工标注查询记录单库 Top1 诊断分数，汇总并扫描候选阈值；不参与生产搜索 |

## 安装依赖

适用环境：Python 3.14.4、SQLite 3.46.1（FTS5 可用）、sqlite-vec 0.1.9。

```bash
python -m pip install -r requirements.txt
```

数据库父目录和日志目录须由运行该程序的普通用户可写；程序本身不修改权限，也不需要 root。Embedding 服务须已在本机 `127.0.0.1:19433` 启动，并接受模型名 `EmbeddingGemma 300M`。

## CLI 用法

在 `knowledge-base/` 目录执行：

```bash
python ingest.py
python ingest.py --scope chen
python ingest.py --scope family
python search.py "问题"
python search.py "问题" --top-k 5
python search.py "问题" --scope chen
python search.py "问题" --scope family
python search.py "赤狐星云" --scope family --debug-scores
```

`--scope` 只接受 `chen` 或 `family`；省略时默认 `chen`，所以原有不带参数的 `kb-chen-ingest.service` 调用方式保持兼容。CLI 搜索仍只查询一个库，不会自动混合两个 scope。导入只处理四种支持的扩展名。失败的文件保留旧索引（如有）；扫描不完整时停止删除清理。

复用多库搜索时可从 Python 调用：

```python
from search import search_scopes

results = search_scopes("问题", ["chen", "family"], 5)
```

返回 `SearchResult` 列表，保留原有的 `scope`、全局 `rank`、`fused_score`、`source_path`、`filename`、`page`、`chunk_index`、`snippet`，并增加 `semantic_distance`、`semantic_score`、`lexical_match`、`lexical_score` 诊断字段及完整 `text`。每个数据库先独立执行原有 Hybrid Search，再按各库内名次做第二层 RRF；同名次按调用者给出的 scope 顺序稳定排序，不比较跨库的原始 BM25 分数、向量距离或库内融合分数。请求的任一数据库不存在或查询失败时会抛出异常，不会静默返回不完整的多库结果。

## 相关度诊断

`fused_score` 是 RRF 排名分数，只表示候选在两路（或多库第二层）排名中的位置，不是绝对相关度，也不能直接作为拒答阈值。向量表配置为 `distance_metric=cosine`；sqlite-vec 0.1.9 的 `distance` 为余弦距离 `1 - cosine_similarity`。因此 `semantic_distance` 是原始距离（越小越近），`semantic_score = 1 - semantic_distance` 是余弦相似度（越大越近，理论范围 -1 至 1，浮点计算可能有微小误差）。它也不是已经校准的“查询相关概率”。

`lexical_match` 表示该 chunk 是否进入本次 FTS 候选 Top-N；`lexical_score` 是对应的 FTS5 原始 BM25 值（越小排序越靠前），仅作诊断，不参与跨库数值比较。未进入 FTS 候选 Top-N 时，`lexical_score` 为 `None`，不等于零分或证明完全没有词面匹配。对于仅由 FTS 路选出的最终结果，程序也会按 chunk ID 计算余弦距离；只有对应向量行不存在时，语义字段才为 `None`。

单库 CLI 默认输出和排序不变；加 `--debug-scores` 后，每条结果额外显示上述原始分数。例如：

```bash
python search.py "赤狐星云" --scope family --debug-scores
```

默认仍只提供诊断数据，不启用过滤；无关查询仍可能有语义 Top-K 结果。可显式启用下面的暂定检索门控，但它不判断文档是否真正能回答问题。

## 暂定 Retrieval Gate

两个 provisional 阈值集中定义在 `config.py`，并校验 `reject < accept`。它们来自当前少量标定样本，未来应继续重标定，不应视为通用或生产级 answerability 阈值：

| Top1/候选 `semantic_score` | `RelevanceDecision` | 含义 |
| --- | --- | --- |
| `< 0.55` | `REJECT` | 当前 embedding 证据明显不足 |
| `0.55` 至 `< 0.64` | `UNCERTAIN` | 主题可能相关，但不能证明文档能回答；未来可交给 answerability judge |
| `>= 0.64` | `ACCEPT` | 语义相关度较高，但事实答案仍可能不存在 |

缺少语义分数时保守标为 `UNCERTAIN`。`lexical_match` 和 RRF `fused_score` **不参与三态判断**；词面命中不能把结果自动升级为 `ACCEPT`。尤其 `ACCEPT` 绝不表示“可以直接编答案”。当前没有 LLM、reranker、自动回答或事实核验。

```bash
python search.py "家庭服务器的公网 IP 是什么？" --scope family --debug-scores
python search.py "问题" --scope family --relevance-gate
python search.py "问题" --scope family --relevance-gate --debug-scores
```

默认关闭门控，原有候选与 RRF 排序及输出保持不变。开启 `--relevance-gate` 后，仅在既有 Top-K 排序**之后**隐藏 `REJECT`，保留 `UNCERTAIN` / `ACCEPT` 并显示 decision；不会回填更多候选。保留结果按原顺序重新编号，原 RRF 分数不变。`--debug-scores` 无论是否开启门控都会显示语义分数、距离、FTS 诊断和 decision。

Python API 的 `search_scope()` 与 `search_scopes()` 结果均可读取 `result.relevance_decision`；默认不会过滤。需要过滤时显式传入 `relevance_gate=True`，例如 `search_scopes("问题", ["chen", "family"], 5, relevance_gate=True)`。`classify_relevance(score)` 是独立纯函数，可用显式 `reject_threshold`、`accept_threshold` 参数进行离线试验；这不会修改正在运行的默认搜索配置。

## RAG Context

`rag_context.py` 复用现有单库 `search_scope()` / 多库 `search_scopes()` 和 Retrieval Gate，不重新实现 Embedding、FTS、向量检索或 RRF。调用者明确列出的 scope 才会被查询；单独指定 `chen` 不会自动访问 `family`，反之亦然。默认启用门控：REJECT 候选不进入 `evidence`，UNCERTAIN 和 ACCEPT 保留。这里的 `top_k` 表示**门控后的最终 evidence 上限**：RAG Context 会在现有搜索允许的 50 条范围内有限 overfetch，再把门控后的结果截取为最多 `top_k` 条。这样可降低前几名全被拒绝导致的 false REJECT，但不能保证绝对召回；它不改变 `search.py` 的默认搜索行为。

```bash
python rag_context.py "家庭共享服务器的测试代号是什么？" --scope family
python rag_context.py "服务器测试代号是什么？" --scope chen --scope family --top-k 5
python rag_context.py "服务器测试代号是什么？" --scope family --output context.json
```

正常查询时，CLI 的 stdout 只输出 JSON，不混入 INFO 日志或人类说明；`--output` 会另外写入同一份 UTF-8 JSON 文件。错误消息写 stderr。正常检索成功但没有证据时，仍输出 `retrieval_status="REJECT"`、空 `evidence` 并以退出码 0 结束；数据库、配置、Embedding、非法 scope 或 JSON 序列化错误则以非零状态结束，**不会伪装成 REJECT**。

Python API：

```python
from rag_context import build_rag_context

context = build_rag_context("服务器测试代号是什么？", ["chen", "family"], top_k=5)
payload = context.to_dict()  # 可直接 json.dumps(payload, ensure_ascii=False)
```

JSON schema `1.0` 的字段固定显式生成，例如：

```json
{
  "schema_version": "1.0",
  "query": "家庭共享服务器的测试代号是什么？",
  "scopes": ["family"],
  "retrieval_status": "ACCEPT",
  "evidence_count": 1,
  "evidence": [
    {
      "scope": "family",
      "rank": 1,
      "fused_score": 0.032787,
      "semantic_score": 0.759152,
      "semantic_distance": 0.240848,
      "lexical_match": true,
      "lexical_score": -0.000012,
      "relevance_decision": "ACCEPT",
      "source_path": "/srv/storage/knowledge/shared/family/family_test.md",
      "filename": "family_test.md",
      "page": null,
      "chunk_index": 0,
      "text": "这里是完整的 chunk 文本，不是 280 字摘要。"
    }
  ]
}
```

`text` 直接来自检索时已读取的完整 `chunks.text`；现有 `search.py` 终端输出仍只显示 `snippet`。顶层 `retrieval_status` 只表示**检索阶段状态**：没有门控后的结果为 REJECT；有结果且最高仅为 UNCERTAIN 则为 UNCERTAIN；至少一个 ACCEPT 则为 ACCEPT。UNCERTAIN 保留给未来的 Answerability Judge 检查。ACCEPT 只表示有较强语义候选，**不等于 answerable，更不允许直接编答案**。当前项目尚未实现 Judge、LLM 调用或自动回答。

## 宿主机 Unix socket 查询服务

`kb_service.py` 只复用 `build_rag_context()`，不重新实现搜索或门控，也不提供 ingest、文件读取、数据库查询/修改、命令执行等接口。它**只监听 Unix domain socket**，不监听任何 TCP 地址。Socket 父目录须预先存在；服务不会自动创建 `/run/knowledge-base`，也不会修改其权限。

在支持 Unix socket 的宿主机上，可由已有的普通用户运行：

```bash
python kb_service.py --socket /run/knowledge-base/backend.sock --socket-mode 0660
```

唯一查询路由是 `POST /v1/context`，请求体为 UTF-8 JSON：

```json
{"query":"家庭共享服务器叫什么？","scopes":["family"],"top_k":5}
```

成功响应为现有 RAG Context schema `1.0` 的完整 JSON，包括 `retrieval_status`、`evidence_count` 和 `evidence`。`GET /health` 仅返回 `{"status":"ok","schema_version":"1.0"}`，表示进程在响应，**不证明**数据库或 Embedding 服务可用。其他路由不存在，也没有任意路径、数据库名或文件名参数。请求体最多 64 KiB，query 最多 4096 个 Unicode 字符；scope 只能是 `chen` / `family`，`top_k` 为 1–50，省略时为 5。

| 情况 | HTTP 状态 | 响应含义 |
| --- | --- | --- |
| 检索成功，包括无证据 | `200` | 无证据时 `retrieval_status="REJECT"`、`evidence=[]` |
| 无效 JSON / query / scope / top_k | `400` | 错误对象，不是 RAG Context |
| 请求体超过限制 | `413` | 错误对象 |
| Embedding 服务失败 | `503` | 错误对象；不能当作无证据 |
| SQLite 或其他内部查询错误 | `500` | 错误对象；不能当作无证据 |

旧 Node.js 客户端通过 backend socket 发出固定的 `POST /v1/context`，stdout 只输出服务返回的 JSON；正常的 REJECT 仍以退出码 0 结束，参数/服务/协议错误写 stderr 并以非零退出。`--socket` 仅用于指定另一个**本地 Unix socket 路径**，不接受 HTTP URL。它仅供可信宿主机诊断，**不能放进 OpenClaw Agent 作为授权工具**：

```bash
node clients/openclaw_kb_client.mjs --query "家庭共享服务器叫什么？" --scope family --top-k 5
node clients/openclaw_kb_client.mjs --query "问题" --scope chen --scope family --top-k 5
```

systemd unit 示例（仅供按实际安装路径和权限修改，**未在服务器部署**）：

```ini
[Unit]
Description=Local knowledge-base query service
After=network.target

[Service]
Type=simple
User=chen
WorkingDirectory=/opt/knowledge-base
ExecStart=/opt/knowledge-base/.venv/bin/python /opt/knowledge-base/kb_service.py --socket /run/knowledge-base/backend.sock --socket-mode 0660
RuntimeDirectory=knowledge-base
RuntimeDirectoryMode=0750
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Backend socket **没有 Agent 授权**，只能让 Broker 与可信宿主机诊断进程连接，不能挂载给 OpenClaw 容器。示例 `0660` 仅允许 owner/group 连接；目录 `0750` 也必须控制遍历权限。不要使用 `0777`。服务启动时仅清理已确认失效的旧 socket，并尽可能在退出时清理自己的 socket。上面的 systemd 示例只是后端示例，不表示服务器已迁移。

## KB Policy Broker：Agent 到 scope 的中央映射

Broker 是独立的宿主机本地 Unix socket 服务，固定监听 `/run/knowledge-broker/query.sock`，从 `/etc/knowledge-broker/policy.json` 读取 ACL，转发到 `/run/knowledge-base/backend.sock`。它不监听 TCP，也不直接检索数据库或修改索引。Broker 的 policy 示例见 [`examples/knowledge-broker-policy.json`](examples/knowledge-broker-policy.json)。实际服务器上的文件、目录、socket 权限和服务部署需单独规划；本仓库没有替用户操作服务器。

Policy schema `1.0` 顶层只包含 `schema_version`、`shared_scope`（当前固定为已配置的 `family`）及 `agents`。每个 Agent 映射为 `{ "private_scope": "chen" | null, "shared": true | false }`。示例中 `main`、`chen` 的 private 都指向 `chen`；`liang`、`ziling` 的 private 为 `null`；四者的 shared 都指向 `family`。Agent ID 不写死在 Python 代码中。未知 Agent、缺失 policy、非法 schema、未知 scope 都 fail closed；未来只有当后端正式增加 `liang`/`ziling` 私库配置后，policy 才能把对应 `private_scope` 改为这些值，模型工具接口无需改变。

Broker 只接受 `POST /v1/private-context` 或 `POST /v1/shared-context`，请求体为 `{"agent_id":"chen","query":"问题","top_k":5}`。前者用 policy 的该 Agent `private_scope`；后者先检查 `shared=true`，再用 policy `shared_scope`。由 Broker 而非模型生成后端的 `scopes=[...]`。`GET /health` 只返回状态与 schema 版本，不暴露 ACL/用户列表。请求字段严格限制为 `agent_id`、`query`、`top_k`；query 最多 4096 字符，top_k 为 1–10。授权失败返回 403，输入错误 400；正常无证据返回 200/REJECT/空 evidence；后端超时或故障返回 5xx，绝不伪装成 REJECT。

Broker 先严格验证 backend 的内部 RAG Context（其中仍有 `scope`、`scopes`、`source_path`），然后明确投影为给插件/模型的 schema `1.0`。顶层只含 `schema_version`、`query`、`retrieval_status`、`evidence_count`、`evidence`；每条 evidence 只含 `rank`、`fused_score`、`semantic_score`、`semantic_distance`、`lexical_match`、`lexical_score`、`relevance_decision`、`filename`、`page`、`chunk_index`、`text`。外部结果与插件 outputSchema 都不含内部 scope 名或 `source_path`。后端 `kb_service.py` 的 RAG Context 协议保持原样。文档正文或文件名本身若提到这些普通词语，不属于结构字段清理范围。

Broker 的 systemd unit 示例（`knowledge-broker.service`，**未在真实服务器部署**）：

```ini
[Unit]
Description=Local knowledge-base policy broker
After=knowledge-base.service
Requires=knowledge-base.service

[Service]
Type=simple
User=chen
WorkingDirectory=/opt/knowledge-base
ExecStart=/opt/knowledge-base/.venv/bin/python /opt/knowledge-base/kb_policy_broker.py
RuntimeDirectory=knowledge-broker
RuntimeDirectoryMode=0750
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

示例假定 `knowledge-base.service` 已按上文使用 backend socket，且 `chen` 可读取 `/etc/knowledge-broker/policy.json`、连接 backend socket。实际安装路径、unit 名称、目录与组权限须在服务器部署前核对；仅增加 unit 文本不会自动建立隔离或开放容器访问。

Broker 为每次 POST 写结构化 audit：UTC 时间、UUID4 request_id、Agent ID、PRIVATE/SHARED、解析出的 scope、ALLOW/DENY/ERROR、retrieval_status、evidence_count、耗时，以及 Linux 支持时的 peer UID/GID/PID。默认仅记录 query 长度与 SHA-256 前缀，不记录完整 query、evidence 或 chunk。应将日志权限限制在可信管理员范围。

OpenClaw 插件只请求 Broker socket，模型仅看到 `knowledge_private(query, top_k?)` 与 `knowledge_shared(query, top_k?)`；模型参数和模型侧结构化结果均不含 Agent ID 或内部 scope。插件从可信 `toolContext.agentId` 注入身份，本地 `agents: {id: {private:boolean, shared:boolean}}` 只用于工具可见性/第一层防误调用，**Broker policy 是最终 ACL**。详见 [`integrations/openclaw-knowledge-query/README.md`](integrations/openclaw-knowledge-query/README.md)。同一个 Gateway/容器/Unix UID 中，拥有任意代码执行能力的 Agent 理论上仍可直连 Broker socket 并伪造 JSON 中的 `agent_id`；此设计不是密码学身份认证或强租户隔离。未来需要 per-agent sandbox、独立 UID/容器或 capability boundary。切勿宣称当前 Broker 能防任意代码执行攻击。

## Knowledge Import Pipeline（V2.1）

```text
/srv/storage/ai-inbox/<uploader>/{private,shared}/
  → 一次扫描与稳定性复核 → 类型/大小/安全性与 Parser 校验
  → policy route → scope 内 SHA-256 去重/文件名冲突检查
  → /srv/storage/knowledge/... 原始 Source → 每个受影响 scope 一次增量 ingest
  → RAG SQLite / FTS5 / vector 派生索引
```

**正式原始文件是唯一事实源。** 每用户 `/var/lib/knowledge-import/<uploader>/imports.db` 是独立的导入元数据/审计库，不是 RAG 的 `knowledge.db`；它只存 import_id、上传者、private/shared、scope、原/目标文件名和路径、SHA-256、大小、状态、错误码/简短错误消息、创建/更新时间与 indexed_at，绝不保存正文、解析文本或 chunk。Importer 不直接写 RAG 数据库。一次扫描成功导入同一 scope 多个文件时只调用一次现有 `ingest_scope(scope)`；以其 `failed == 0` 且无异常为索引成功依据。`INDEX_ERROR` 保留 Source，仅由同一 uploader 的下一轮 Importer 重试；现有 ingest timer 也可重新构建索引。

指定 `--uploader` 后，输入**只来自该用户** `/srv/storage/ai-inbox/<uploader>/private/` 与 `shared/` 的第一层文件；即使 policy 包含其他用户，也不扫描其 Inbox。不会递归扫描 `rejected/`，也不把 Source 当输入。无需 `.ready`、特殊文件名或特定上传协议。第一轮记录大小、纳秒 mtime、设备和 inode；全轮仅等待一次（默认 2 秒）后逐个复核，变化文件留在 Inbox 供下一轮处理。隐藏文件及 `.tmp`、`.part`、`~` 尾缀暂不处理。仅支持大小写不敏感的 MD/TXT/DOCX/可提取文字的 PDF；空文件、超限、不支持格式、symlink、FIFO 等非普通文件及 hard link 均拒绝。扫描 PDF 无可提取文字时为 `NO_EXTRACTABLE_TEXT`，未来 OCR 阶段才会支持；坏文档为 `PARSER_ERROR`。每个文件独立处理，不因单份坏文件中断其他候选。

去重键是**目标 scope + 流式 SHA-256**，不跨 private/shared 全局去重；即使 Source 文件早于 Importer 已存在，也检查 Source 原文件。普通且在大小上限内的候选逐块计算 SHA；空文件记录空内容 SHA，超限或非普通文件不读取/哈希。同 scope 相同内容判 `DUPLICATE`，同名不同内容判 `CONFLICT`，均不写 Source、不自动改名或覆盖，原 Inbox 文件移至 `<user>/rejected/<kind>/<status>/<import_id>__<original_filename>`。其他拒绝文件也按状态保留在那里，不静默删除；如果隔离移动失败，原 Inbox 文件仍保留并在元数据中标记。导入审计和日志不记录正文；日志仅记 ID、上传者、类别、scope、文件名、大小、SHA 前缀、状态与耗时。当前 `parsers.py` 接收内存中的字节快照，因此解析接近大小上限的文件仍需相应内存；上线前应按机器内存评估 policy 上限。

正式发布使用目标目录内独占临时文件：写入后对打开的文件描述符显式 `fchmod(0640)` 并 `fsync`。首选 Linux `renameat2(RENAME_NOREPLACE)` 将同目录 temp 原子改名为 final；底层 `rename_noreplace()` 保持严格语义，不自行降级。若它明确返回 `UnsupportedPublicationError`（例如 mergerfs 挂载层对该 flag 返回 `EINVAL`），**仅 Importer 已持有同一 scope 的 `publish.lock` 时**，再次 `lstat` 确认 final 不存在，才执行同目录普通 `os.rename(temp, final)`；final 已存在（包括 symlink）则拒绝并走 duplicate/conflict 路径。其他 I/O 错误不触发 fallback。两级策略均不使用 hard link、`os.replace`，不绕过 `/srv/storage` 直接写底层 branch，因此仍遵循 mergerfs 的 branch placement policy；发布后 `fsync` 目标目录。final 从首次可见起就是单链接普通文件（`st_nlink == 1`），不会出现旧版 `link + unlink` 的瞬时双链接窗口。Importer **不 chmod Inbox 原文件**。private Source 的目录访问控制仍由其私有目录决定；shared Source 能否被其他用户读取，还取决于目录遍历权限、属组与 setgid/ACL。这种先复制到目标目录的方式也适用于 Inbox 与 Source 不在同一 filesystem；中途失败不会把半写入内容作为 final 文件暴露。fallback 的 no-clobber 保证**来自单一受信任写入路径及所有参与者遵守 per-scope `publish.lock`**，不是内核级 `RENAME_NOREPLACE`；不受锁约束的外部写入者仍可能在 `lstat` 与普通 rename 之间抢占 final，因此必须禁止其他进程绕过 Importer 写该 Source。真实 NAS 上的 mergerfs 普通 rename、目录 `fsync`、权限及容量行为仍须实测。每个 uploader 使用自己的 `/run/knowledge-import/<uploader>/import.lock` 独占锁：同一用户不能并发导入，不同用户的锁互不阻塞；Linux 使用 `fcntl.flock`。运行用户必须控制自己的锁目录和 Importer DB 目录。

同一 scope 还有两把用途不同的锁：`<scope.state_dir>/publish.lock` 保护 Source 中的 **SHA-256 去重检查到 final 发布**，避免不同 uploader 以不同文件名同时发布相同内容；`<scope.state_dir>/ingest.lock` 由 `ingest_scope()` 自己持有，覆盖完整的扫描、SQLite 更新和删除阶段，因此 Importer、既有 ingest timer、手动 CLI 等调用方都受同一把锁约束。实际路径分别位于 `/var/lib/knowledge-base/private/chen/` 或 `/var/lib/knowledge-base/shared/family/` 下。锁顺序为「每用户 Importer 锁 → scope 发布锁 → 释放发布锁 → scope ingest 锁」，不会嵌套持有两个 scope 锁。锁文件只是持久的同步入口，不以文件存在与否判断占用；进程退出后内核释放 `flock`。所有参与者必须使用同一可信 state 目录和这些锁；手工绕过 Importer 直接写 Source 不受发布锁保护。

Importer policy 固定由 `--policy` 指向 JSON，格式示例见 [`examples/knowledge-import-policy.json`](examples/knowledge-import-policy.json)；示例文件含多个 uploader 仅用于说明格式，生产实例建议拆成单 uploader policy。顶层必须有 `schema_version="1.0"`、正整数 `max_file_size_bytes`、`routes`；每位上传者必须显式配置 `private` 和 `shared`。启用的 route 必须含 `enabled=true`、当前 `config.py` 已知的 `scope`、绝对 `destination`，且 destination 必须**精确等于**该 scope 的正式 Source 根目录，不能指向 Inbox 或任意其他路径。关闭的 route 仅写 `{"enabled":false}`。非法/缺失 policy、重复键、未知 scope、无效或不在 policy 中的 `--uploader`、路径不匹配均拒绝启动；CLI 不接受任意 source/destination/scope/state/lock 参数。当前只启用 `chen`、`family` 两个知识库 scope；`liang`、`azl` 的 private route 仍关闭。

CLI 每次只扫描一轮，`--once` 可显式写出；无 daemon/watch loop。生产多用户实例应显式指定 `--uploader`，并让 policy 只包含该 Unix/Inbox 用户的 routes：

```bash
python knowledge_importer.py --policy /etc/knowledge-import/chen.json --uploader chen --once
python knowledge_importer.py --policy /etc/knowledge-import/liang.json --uploader liang --once --dry-run
python knowledge_importer.py --policy /etc/knowledge-import/azl.json --uploader azl --once --settle-seconds 5
```

不传 `--uploader` 保留 V2.1 单实例开发/测试模式：扫描 policy 中所有用户，使用旧的 `/var/lib/knowledge-import/imports.db` 和 `/run/knowledge-import/import.lock`；**不要用于生产多用户权限隔离**。`--dry-run` 会扫描、复核稳定性、解析并判断 `WOULD_IMPORT` / `WOULD_REJECTED` / `WOULD_DUPLICATE` / `WOULD_CONFLICT`，但不会移动文件、创建或修改 `imports.db`、调用 ingest。普通导入成功后 Inbox 文件消失；再次运行空 Inbox 不重复建记录或重复索引。Importer 的 Python API 是 `load_policy()` 加 `KnowledgeImporter(policy, uploader="chen").run()`，受信任调用方和本地测试可以注入临时状态库/锁路径。

规划的独立 policy 为 `/etc/knowledge-import/chen.json`、`/etc/knowledge-import/liang.json`、`/etc/knowledge-import/azl.json`，每份只含对应 uploader。三者的 route 分别是：`chen: private→chen, shared→family`；`liang: private→disabled, shared→family`；`azl: private→disabled, shared→family`。这里 `chen`/`family` 是现有知识库 scope，`liang`/`azl` **不是**已启用的 private scope。OpenClaw Agent 名 `ziling` 与 Unix/Inbox 用户 `azl` 不同；本层只认真实 uploader 身份，未来由尚未实现的 Import Broker 负责 `ziling → azl` 映射。不要把 Agent 名直接作为 Inbox 用户猜测。

systemd template 仅供代码验收后的单独部署规划，**未创建 unit，也未修改或测试真实服务器**：

```ini
# knowledge-import@.service
[Unit]
Description=Knowledge Importer for %i
After=kb-embed.service
Requires=kb-embed.service

[Service]
Type=oneshot
User=%i
Group=%i
# 仅在部署确认 nas-users 的授权范围后启用；不是私人 Inbox 的通行证。
SupplementaryGroups=nas-users
WorkingDirectory=/opt/knowledge-base
StateDirectory=knowledge-import/%i
StateDirectoryMode=0700
RuntimeDirectory=knowledge-import/%i
RuntimeDirectoryMode=0700
UMask=0077
ExecStart=/opt/knowledge-base/.venv/bin/python /opt/knowledge-base/knowledge_importer.py --policy /etc/knowledge-import/%i.json --uploader %i --once
```

```ini
# knowledge-import@.timer
[Unit]
Description=Run Knowledge Importer for %i

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
Unit=knowledge-import@%i.service

[Install]
WantedBy=timers.target
```

[systemd.exec 手册](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)允许 `StateDirectory=`、`RuntimeDirectory=` 使用相对嵌套目录（如 `foo/bar`），管理器创建其父目录，并为最内层目录设置服务用户/组和 Mode；`RuntimeDirectory` 最内层会在 oneshot 停止后移除，下次运行重建。仍须在实际服务器核对 systemd 版本、unit 解析结果、目录属主与权限、timer 行为及失败恢复。不要把上面示例视为已部署成功。

每个私人 Inbox 应保持 `/srv/storage/ai-inbox/{chen,liang,azl}` 分别由同名 Unix 用户拥有，目录模式 `0700`；不要为方便 Importer 把所有 Inbox 改成 `0770`，也不要让 `chen` 读取 `liang` 的 Inbox。每个实例只需本人的 Inbox 读写、本人 state/runtime 目录和 policy 读取权限。`family` 共享 Source 目录须由 `nas-users` 等经审核的组持有、启用 setgid，并给予该组所需的目录遍历/读写权限；这样新发布的 `0640` 文件才能继承正确属组并供授权 indexer 读取。`UMask=0077` 可以继续保护其他私有状态，但不会覆盖 Source 的显式 `fchmod(0640)`。private Source 和 Inbox 的目录仍须保持私人访问控制。

`family` 派生 state 目录 `/var/lib/knowledge-base/shared/family` 及 `index/`、`logs/` 也须预先按受信任组配置 setgid/权限，让所有获授权的 family ingest 用户能打开同一个 `0660` 锁文件并实际读写 SQLite 主库、日志及其临时/journal 文件。锁代码不会自动修复目录属组或数据库/日志的权限；`UMask=0077` 也可能使这些派生文件成为仅 owner 可读写。部署前必须在真实主机核验并设计受控 ACL/属组、SQLite 并发和 mergerfs 行为；若无法满足，不要启用多用户轮流执行 family ingest。不要靠放宽私人 Inbox 权限解决。服务用户无需 root。未来若加 OpenClaw 导入工具，可提供 `knowledge_import_private` / `knowledge_import_shared`，只接收可信 attachment handle；模型不应传任意路径、scope、agent_id 或 destination。本次没有实现 Import Broker、插件、Web 上传或 OCR。

## 可信聊天附件排队（未部署）

OpenClaw 2026.9.4 固定依赖的公开 Plugin SDK 提供 `inbound_claim` Hook：其 `event.media` 是暂存后的本地附件事实，`event.mediaStagingPending` 表示尚不可用，`ctx.agentId` / `ctx.sessionKey` 用于隔离。插件只登记该公开 Hook 的 `media.path`，不读取聊天文字中的路径，不使用 `originalMedia`、URL 或 `/app/dist` 私有接口。Hook 没有独立的原始文件名字段，因此目前取暂存路径的 basename 作为文件名；实际渠道若把暂存文件改成不带受支持扩展名的随机名，将返回 `UNSUPPORTED_TYPE`，不能靠模型覆写文件名。Hook 缺少受信任 Agent/会话键、附件仍在 staging、不是普通文件等情况均不登记 READY。实际渠道是否触发该 Hook、是否给出可读暂存路径和 Agent ID，仍须在 OpenClaw 真实容器内验证。

新链路与查询链路独立：`knowledge_import_private()` / `knowledge_import_shared()` 的模型参数严格为 `{}`；工具从可信 `toolContext.agentId` 和 `toolContext.sessionKey` 选取当前会话最近一条单附件消息。多附件返回 `SELECTION_REQUIRED`，没有 READY 附件返回 `NO_ATTACHMENT`。进程内 Registry 最多保留 100 个会话、每会话最多 8 个附件、TTL 30 分钟；重启即丢失。已经发起传输的附件不会自动重发：超时可能意味着 Broker 已接收但响应丢失，工具返回 `BROKER_ERROR`，再次调用返回 `ALREADY_QUEUED`，需要人工核对 Inbox。此阶段**不会主动提示用户入库**，只在用户调用工具时排队。

发送前插件对可信暂存路径 `lstat` 并以 `O_NOFOLLOW` 打开，核对打开 fd 的 dev/inode/size/mtime、普通文件类型、扩展名和 100 MiB 上限。附件正文走固定 Unix socket `/run/knowledge-import-broker/import.sock` 的 HTTP raw body 流，不使用 base64 JSON、不由模型指定路径、URL、scope、uploader 或目标。Broker 的独立固定 policy 示例是 [`examples/knowledge-import-broker-policy.json`](examples/knowledge-import-broker-policy.json)：`main`、`chen` 映射 `chen` 且允许 private/shared；`liang` 只允许 shared；`ziling` 映射 `azl` 且只允许 shared；未知 Agent 拒绝。Broker 的 `max_file_size_bytes` 必须不高于对应用户 Importer policy 的限制，当前示例均为 100 MiB。插件 `imports` 配置缺失时两个写入工具完全隐藏；现有 `agents.<id>.private/shared` 查询配置**不会自动继承写权限**。Broker policy 才是最终 ACL，插件配置仅是工具可见性控制。

宿主机 Broker 只允许固定路由，把流式正文写入 `/srv/storage/ai-inbox/<policy 映射 uploader>/private|shared` 的同目录独占临时文件，计算 SHA-256，`fchown` 至目标 Unix 用户、`fchmod(0600)`、`fsync` 后改名为随机队列文件，再 `fsync` 目录。它不解析、不中转到 Source、不执行附件，也不触碰 SQLite、Embedding 或 ingest。`QUEUED` 只表示 Inbox 排队成功，**不是 `INDEXED`**；后续仍由现有 `knowledge_import@<user>.timer/service` 与 `knowledge_importer.py` 处理。单一 Broker 串行处理请求，随机 final 名并复查是否存在；普通 rename 的防碰撞依赖这个受控写入路径及私人 Inbox 的目录权限，不能防护绕过 Broker 的恶意并发写入者。不要将 Inbox、Source 或 backend.sock 挂载到 OpenClaw，只向容器提供 Import Broker socket。

建议单独的宿主机 systemd 服务（**仅示例，尚未部署**）：

```ini
[Unit]
Description=Knowledge attachment import broker
After=local-fs.target

[Service]
Type=simple
User=root
Group=openclaw
WorkingDirectory=/opt/knowledge-base
ExecStart=/opt/knowledge-base/.venv/bin/python /opt/knowledge-base/knowledge_import_broker.py
RuntimeDirectory=knowledge-import-broker
RuntimeDirectoryMode=0750
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX
ReadWritePaths=/srv/storage/ai-inbox /run/knowledge-import-broker
CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

中央 Broker 需要对 `chen`、`liang`、`azl` 的 `0700` Inbox 写入和 `chown`，因此示例使用受严格约束的 root 服务；真实部署时必须核实 `Group=openclaw`、容器映射 UID/GID、socket 属组/模式、systemd sandbox 与 mergerfs 上的目录 fsync/rename。policy 应由 root 管理并仅允许 Broker 读取。Unix socket 限制本机连接，但客户端提交的 `agent_id` 本身不是密码学身份：同 UID/组拥有任意代码执行能力的进程仍可能伪造它；若需强隔离，必须另设 per-agent 进程/UID 或可信身份通道。不要将此示例视为已在服务器运行成功。

## 相关度标定

准备一个 UTF-8 JSON 数组；每条查询必须标注 `query`、白名单 scope（`chen` 或 `family`）和布尔值 `expected_relevant`：

```json
[
  {"query": "家庭共享服务器叫什么？", "scope": "family", "expected_relevant": true},
  {"query": "量子引力黑洞蒸发霍金辐射的实验验证", "scope": "family", "expected_relevant": false}
]
```

```bash
python calibrate_relevance.py relevance_cases.json
python calibrate_relevance.py relevance_cases.json --output results.json
```

脚本调用 `search.py` 的单库 Hybrid Search，记录每条查询的 Top1：`query`、`scope`、`expected_relevant`、`filename`、`chunk_index`、`semantic_score`、`semantic_distance`、`lexical_match`、`lexical_score` 和单库原有 `fused_score`。没有结果时仍保留该样本，`has_result=false` 且结果字段为 `null`；单条查询失败会记录 `error`，并使进程返回非零状态，不会误当作“无结果”。

控制台显示每条结果、正负样本数、两组语义分数的 min/median/max，以及 `lexical_match=true` 的比例。无结果样本计入标签数量和比例分母，但没有语义分数；出错样本不进入统计比例和阈值计算。`--output` 另外写入机器可读的 JSON，包含 `cases`、`summary`、完整的 `threshold_scan` 和按 F1 排名前五的 `best_thresholds`。不要将输出文件设为输入测试集本身。

阈值扫描只尝试测试集中出现过的 Top1 `semantic_score` 值。规则为 `semantic_score >= threshold` 判为相关；无结果或缺少语义分数判为不相关。对每个候选计算 TP、FP、TN、FN、precision、recall、F1；同分时按 precision、recall、阈值降序稳定排序。所有阈值仅是**诊断建议**：样本少或不具代表性时不能视作生产阈值；脚本不会修改 `search.py` 的默认返回或增加拒答过滤。

## 数据库结构

两个 scope 的数据库、日志完全独立：

| scope | 文档根目录 | 数据库 | 日志目录 |
| --- | --- | --- | --- |
| `chen` | `/srv/storage/knowledge/private/chen` | `/var/lib/knowledge-base/private/chen/index/knowledge.db` | `/var/lib/knowledge-base/private/chen/logs` |
| `family` | `/srv/storage/knowledge/shared/family` | `/var/lib/knowledge-base/shared/family/index/knowledge.db` | `/var/lib/knowledge-base/shared/family/logs` |

| 表 | 内容 |
| --- | --- |
| `documents` | 文件 ID、路径、文件名、类型、大小、纳秒级 mtime、SHA-256、索引时间 |
| `chunks` | chunk ID、文档 ID、顺序、页码、文本、路径、文件名 |
| `chunks_fts` | FTS5 trigram 文本索引；`rowid = chunks.id` |
| `chunks_vec` | `embedding float[768]`，余弦距离；`rowid = chunks.id` |

`documents.id` 由固定范围内的完整源路径计算，文件内容变化时保持稳定。每个文件的 `chunks`、FTS 行和向量行在同一个 SQLite 事务中替换或删除。

## 设计说明

- 使用文件大小、mtime 和 SHA-256 联合判断变化；每轮仍计算 SHA-256，以发现时间戳与大小未变但内容改变的文件。
- Markdown、纯文本按段落处理；Markdown 与 DOCX 标题参与切块。超长段落优先沿句子、词语边界拆分，最后才拆分无法再细分的长串。
- PDF 页码从 1 开始；扫描版 PDF 若无可提取文本会报错，不包含 OCR。其他格式的 `page` 为 `NULL`。
- FTS5 使用 trigram 支持中文片段；少于三个字符的独立查询词无法进入该路检索，但仍可由语义检索返回结果。
- 两路候选各按自身名次进入 RRF，原始 BM25 分数与向量距离不会直接相加。
- scope 仅有固定的 `chen` / `family` 布局，CLI 和 Python API 都不接受任意目录；两库不共享 SQLite 文件。源目录、数据库及日志路径的已有符号链接组件会被拒绝；文档读取还逐级使用 `O_NOFOLLOW`，拒绝 `..` 和硬链接文件。搜索结果也必须属于当前 scope 的固定源目录。不启用 `liang`、`azl`。
- 程序仅通过本机 loopback HTTP 调用已存在的 Embedding 服务，不加载模型。
- Embedding 模型或维度变更时需重新建立索引；V1 不实现模型迁移或数据库迁移。
- 新增 `family` 后需在实际服务器上验证目录权限、挂载、日志/数据库创建及两个 scope 的隔离。索引与日志目录须由可信运行用户控制，不应允许其他本地用户并发替换路径；符号链接检查不能替代正确的目录权限。这里不代表已运行过服务器测试。
