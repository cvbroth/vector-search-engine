# 家庭 NAS 本地知识库 V1

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `config.py` | 固定 `chen` 的文档、数据库、日志路径及本地 Embedding 配置 |
| `parsers.py` | 从 `.md`、`.txt`、`.pdf`、`.docx` 提取文本；PDF 保留可提取文本的页码 |
| `chunker.py` | 按标题、段落、句子组织 chunk，并保留来源元数据与 overlap |
| `embeddings.py` | 批量请求 `127.0.0.1:19433` 的既有 Embedding 服务，验证 768 维结果 |
| `database.py` | 建立 SQLite、FTS5、sqlite-vec 表；事务式写入与删除 |
| `ingest.py` | 扫描 `chen` 私有目录，新增、跳过、重建或清理索引 |
| `search.py` | FTS5 与语义检索并行排序，通过 RRF 融合结果 |

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
python search.py "问题"
python search.py "问题" --top-k 5
```

`ingest.py` 仅扫描 `/srv/storage/knowledge/private/chen`，并只处理四种支持的扩展名。失败的文件保留旧索引（如有），日志写入 `/var/lib/knowledge-base/private/chen/logs/ingest.log`。扫描不完整时停止删除清理。搜索日志写入同目录的 `search.log`。

## 数据库结构

数据库固定为 `/var/lib/knowledge-base/private/chen/index/knowledge.db`。

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
- 只对 `chen` 的固定目录建立索引；不跟随符号链接，也不导入硬链接文件。程序仅通过本机 loopback HTTP 调用已存在的 Embedding 服务，不加载模型。
- Embedding 模型或维度变更时需重新建立索引；V1 不实现模型迁移或数据库迁移。
