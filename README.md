# xinPlugin_Chroma_fastMCP

MinIO 知识库的向量检索层：把上传到 MinIO 的文档（PDF/txt/md）抽取文本 → 分块 → 向量入库（Chroma），并通过 **FastMCP** 把「检索」暴露成 MCP 工具，供 DSH（DeepSeek Harness）的 `dsh-mcp-client` 连接、让 agent 在问答时直接检索原文。

## 组成

| 文件 | 作用 |
|---|---|
| `chroma_store.py` | Chroma 持久化 + 分块（带页码/行号）+ 混合检索（语义 + 字符二元组 BM25，RRF 融合） |
| `ingest.py` | CLI 入库：`python ingest.py <文件> [source名]`，输出 JSON 摘要（供 MinIO 插件联动调用） |
| `server.py` | FastMCP stdio 服务，暴露 `search` / `ingest_file` / `list_sources` |
| `requirements.txt` | chromadb / fastmcp / pypdf |

## 安装

```powershell
pip install -r requirements.txt
# 首次检索会下载默认 embedding（all-MiniLM-L6-v2，约 80MB，缓存在 ~/.cache/chroma）
```

## 使用

```powershell
# 入库
python ingest.py "广州十五五规划.pdf" "广州十五五规划.pdf"

# 检索（或经 MCP 工具 search）
python -c "from chroma_store import search; import json; print(json.dumps(search('广州 人工智能+ 大模型 算力 数据要素', 6), ensure_ascii=False))"
```

## MCP 工具

- `search(query, top_k=6)`：语义+关键词混合检索，返回原文片段及出处（**文件 + 页码 + 行号**）。
- `ingest_file(path, source_name)`：本地文件入库。
- `list_sources()`：已入库来源清单。

DSH 端以 stdio 连接 `server.py`（`dsh-mcp-client`），工具名形如 `mcp__chroma__search`。

## 检索原理

- **分块**：按页提取文本，过滤页眉/页脚/页码噪声，每 6 行一块（重叠 1 行），元数据记录 `source/page/line_start/line_end`。
- **混合检索**：Chroma 语义向量（余弦） + 字符二元组 BM25 稀疏检索，**RRF 融合**——中文语义 embedding 偏弱时，BM25 兜住「大模型/算力/数据要素」等精确关键词，保证出处定位稳定。
- 入库即失效稀疏缓存，重复入库覆盖更新。
