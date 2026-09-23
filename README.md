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

返回 `SearchResult` 列表，保留原有的 `scope`、全局 `rank`、`fused_score`、`source_path`、`filename`、`page`、`chunk_index`、`snippet`，并增加 `semantic_distance`、`semantic_score`、`lexical_match`、`lexical_score` 诊断字段。每个数据库先独立执行原有 Hybrid Search，再按各库内名次做第二层 RRF；同名次按调用者给出的 scope 顺序稳定排序，不比较跨库的原始 BM25 分数、向量距离或库内融合分数。请求的任一数据库不存在或查询失败时会抛出异常，不会静默返回不完整的多库结果。

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
