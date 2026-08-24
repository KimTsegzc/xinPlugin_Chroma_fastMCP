# xinPlugin_Chroma_fastMCP

> 版本 **v2.0.0** —— 两段式检索闭环：`search` 粗定位 + `read_range` 精读回退。新增**全文索引层**（`fulltext.sqlite3`，标准库 sqlite3 零依赖），入库时「分块→向量库」与「写全文索引」同步完成，解决 v1「只有片段、无法精读」的问题。

MinIO 知识库的检索层：把上传到 MinIO 的文档（PDF/docx/ofd/txt/md 等）抽取文本 → 分块 → 向量入库（Chroma）**＋ 写全文索引**，并通过 **FastMCP** 把「检索 + 精读」暴露成 MCP 工具，供 DSH（DeepSeek Harness）的 `dsh-mcp-client` 连接、让 agent 在问答时**先定位、再精读、标出处**。

## 架构与两段式检索（核心）

```
文件 → MinIO(事实源) → 解析工具层(ingest) → Chroma(向量库·片段)  ─┐
                                │                                    ├→ FastMCP → DSH agent
                                └──────────→ fulltext.sqlite3(全文索引) ─┘
```

**为什么需要两段式？** 向量库里只有「片段」，`search` 只能粗定位（哪份文档、哪几页相关），回答不了「原文原文怎么写、上下文是什么」——表格/跨页内容被切碎后语义会断裂（例如指标名与数值分属不同片段）。所以必须：

| 阶段 | 工具 | 作用 | 返回 |
|---|---|---|---|
| ① 粗定位 | `search(query, top_k)` | 语义 + 字符二元组 BM25 混合检索，RRF 融合 | 片段 + **出处（文件/页码/行号）** |
| ② 精读 | `read_range(source, page, line_start, line_end, context)` | 按 source+page+line 从全文索引读原文（可扩上下文） | 带行号的**原文** |

行号语义严格对齐：全文索引与向量分块共用「原始页文本按 `\n` 切分的 1-based 行号」，因此 `search` 命中的 `line_start/line_end` 可直接喂给 `read_range` 精读同一位置。

## 组成

| 文件 | 作用 |
|---|---|
| `chroma_store.py` | Chroma 持久化 + 分块（带页码/行号）+ 混合检索（语义 + 字符二元组 BM25，RRF 融合） |
| `ingest.py` | **自包含解析工具层** + CLI 入库：`python ingest.py <文件> [source名]`。按类型抽文本（pdf→pdftotext/pdfminer/pypdf；docx/ofd→zip+XML；txt/md…→UTF-8/GBK）。**v2 起入库同步写全文索引**，输出 JSON 摘要含 `fulltext` 字段 |
| `fulltext.py` | **全文索引层（v2 新增）**：`fulltext.sqlite3`（sqlite3 零依赖），存来源元数据 + 每页每行原文，供 `read_range` 精读 |
| `server.py` | FastMCP stdio 服务，暴露 `search` / `read_range` / `ingest_file` / `list_sources` |
| `requirements.txt` | chromadb / fastmcp / pypdf / pdfminer.six |

## 安装

```powershell
# 一键安装 + 启动（依赖 + 校验 + 后台拉起 HTTP 服务，默认 127.0.0.1:8000）
powershell -ExecutionPolicy Bypass -File .\install.ps1
# 参数：-Port 8000  -BindHost 127.0.0.1  -NoLaunch(只安装)  -SkipInstall(跳过安装直接启动)
```

或手动：
```powershell
pip install -r requirements.txt
# 首次检索会下载默认 embedding（all-MiniLM-L6-v2，约 80MB，缓存在 ~/.cache/chroma）
```

## 使用

```powershell
# 入库（v2 起同时建全文索引）
python ingest.py "广州十五五规划.pdf" "广州十五五规划.pdf"

# ① 粗定位（或经 MCP 工具 search）
python -c "from chroma_store import search; import json; print(json.dumps(search('广州 GDP 增长目标', 6), ensure_ascii=False))"

# ② 精读（或经 MCP 工具 read_range）
python -c "from fulltext import read_range; import json; print(json.dumps(read_range('广州十五五规划.pdf', 16, 1, 20, context=2), ensure_ascii=False))"
```

## MCP 工具清单（4 个）

| 工具 | 参数 | 作用 |
|---|---|---|
| `search` | `query: str`, `top_k: int = 6` | 语义+关键词混合检索，返回原文片段及出处（文件/页码/行号）——**两段式①粗定位** |
| `read_range` | `source: str`, `page: int`, `line_start: int = 1`, `line_end: int = -1`, `context: int = 0` | 按来源+页+行区间从全文索引读原文（`-1`=读到页尾，`context`=上下各扩 N 行）——**两段式②精读** |
| `ingest_file` | `path: str`, `source_name: str = ""` | 本地文件入库：分块→向量库 + 写全文索引 |
| `list_sources` | — | 已入库来源清单（页数/片段数/解析工具/入库时间） |

DSH 端以 stdio 连接 `server.py`（`dsh-mcp-client`），工具名形如 `mcp__chroma__search`、`mcp__chroma__read_range`。

## 检索原理

- **分块**：按页提取文本，过滤页眉/页脚/页码噪声，每 6 行一块（重叠 1 行），元数据记录 `source/page/line_start/line_end`。
- **混合检索**：Chroma 语义向量（余弦） + 字符二元组 BM25 稀疏检索，**RRF 融合**——中文语义 embedding 偏弱时，BM25 兜住「大模型/算力/数据要素」等精确关键词，保证出处定位稳定。
- **全文索引**：入库时把每页原始行（1-based，含噪声行）写入 `fulltext.sqlite3`，与分块行号同源对齐，`read_range` 按区间点查原文。
- 入库即失效稀疏缓存，重复入库覆盖更新（向量 + 全文索引同步 upsert）。

## 升级说明（v1 → v2）

- v1 已入库的数据只有向量片段、无全文索引，`read_range` 不可用——请对存量文档**重新 `ingest_file` 一次**以建立全文索引（`list_sources` 会提示旧数据）。
- 新增 `fulltext.sqlite3` 为运行时数据（已加入 `.gitignore`），与 `chroma_data/` 同属本地持久化，不入库。
