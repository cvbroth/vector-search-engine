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
```

`--scope` 只接受 `chen` 或 `family`；省略时默认 `chen`，所以原有不带参数的 `kb-chen-ingest.service` 调用方式保持兼容。CLI 搜索仍只查询一个库，不会自动混合两个 scope。导入只处理四种支持的扩展名。失败的文件保留旧索引（如有）；扫描不完整时停止删除清理。

复用多库搜索时可从 Python 调用：

```python
from search import search_scopes

results = search_scopes("问题", ["chen", "family"], 5)
```

返回 `SearchResult` 列表，每条包含 `scope`、全局 `rank`、`fused_score`、`source_path`、`filename`、`page`、`chunk_index`、`snippet`。每个数据库先独立执行原有 Hybrid Search，再按各库内名次做第二层 RRF；同名次按调用者给出的 scope 顺序稳定排序，不比较跨库的原始 BM25 分数、向量距离或库内融合分数。请求的任一数据库不存在或查询失败时会抛出异常，不会静默返回不完整的多库结果。

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
