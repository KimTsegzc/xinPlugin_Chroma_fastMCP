"""Chroma 知识库 FastMCP 服务（stdio 模式）。

两段式检索（v2）：
  ① search(query, top_k)        向量/BM25 粗定位 → 命中 source + page + line_start/line_end
  ② read_range(source, page, ...) 按 source+page+line 从全文索引精读原文（可扩上下文）

暴露工具（4 个）：
  - search(query, top_k=6)    语义+关键词混合检索，返回原文片段及出处（文件/页码/行号）。
  - read_range(source, page, line_start=1, line_end=-1, context=0) 精读原文片段。
  - ingest_file(path, source_name) 本地文件入库（PDF/docx/ofd/txt/md 等），同时建全文索引。
  - list_sources()            已入库来源清单（页数/片段数/解析工具/入库时间）。

DSH 通过 dsh-mcp-client 以 stdio 连接本服务，工具名形如 mcp__chroma__search。
"""
from fastmcp import FastMCP
from chroma_store import search as _search
from chroma_store import list_sources as _cs_list_sources
from fulltext import read_range as _read_range
from fulltext import list_sources as _ft_list_sources
from ingest import ingest_file as _ingest_file

mcp = FastMCP(
    "chroma-kb",
    instructions=(
        "MinIO 知识库检索服务（Chroma 向量 + 全文索引）。两段式："
        "① search 向量/BM25 粗定位（命中 source/page/line_start/line_end）；"
        "② read_range 按 source+page+line 从全文索引精读原文及上下文。"
        "工具清单：search / read_range / ingest_file / list_sources。"
    ),
)


@mcp.tool()
def search(query: str, top_k: int = 6) -> str:
    """按语义检索知识库，返回最相关的原文片段及出处（文件/页码/行号）。

    适用于：用户就知识库文档提问时，先检索原文再作答，务必在回答里标注出处。
    命中后如需精读上下文，用返回的 source/page/line_start/line_end 调 read_range。
    """
    if not query or not query.strip():
        return "错误：query 不能为空"
    results = _search(query.strip(), top_k=top_k)
    if not results:
        return "未检索到相关内容（知识库可能为空，先调用 ingest_file 入库）。"
    lines = []
    for i, r in enumerate(results, 1):
        src = r["source"]
        page = r["page"]
        ls = r["line_start"]
        le = r["line_end"]
        lines.append(f"【片段{i}】出处：{src} 第{page}页 第{ls}-{le}行")
        lines.append(r["text"].strip())
        lines.append("")
    lines.append("提示：如需精读某片段上下文，调用 read_range(source=…, page=…, line_start=…, line_end=…)。")
    return "\n".join(lines)


@mcp.tool()
def read_range(source: str, page: int, line_start: int = 1, line_end: int = -1, context: int = 0) -> str:
    """两段式检索的「精读」环节：按 来源+页码+行号区间 从全文索引读原文（可扩展上下文）。

    用法：先 search 粗定位，拿到命中片段的 source/page/line_start/line_end，
    再调用本工具精读该出处及上下文；line_end 传 -1 表示读到页尾；
    context 表示在行区间上下各多读的行数（默认 0，可传 2~5 看上下文）。
    回答时务必标注出处（文件 + 页码 + 行号）。
    """
    r = _read_range(source, int(page), int(line_start), int(line_end), int(context or 0))
    if not r.get("ok"):
        return "错误：" + r.get("error", "读取失败")
    out = [
        f"【出处】{r['source']} 第{r['page']}页 第{r['line_start']}-{r['line_end']}行"
        f"（本页共 {r['page_lines']} 行，该书共 {r['pages']} 页）"
    ]
    for ln in r["lines"]:
        out.append(f"{ln['line']:>4}  {ln['text']}")
    return "\n".join(out)


@mcp.tool()
def ingest_file(path: str, source_name: str = "") -> str:
    """把本地文件（PDF/docx/ofd/txt/md 等）入库：分块→向量库 + 建全文索引（供 search / read_range）。"""
    r = _ingest_file(path, source_name or None)
    if r.get("ok"):
        ft = r.get("fulltext") or {}
        return (
            f"入库完成：{r['source']} 共 {r['chunks']} 个片段（{r['pages']} 页，{r['parser']}）。"
            f"全文索引已建：{ft.get('lines', 0)} 行，可 search 粗定位 + read_range 精读。"
        )
    return f"入库失败：{r.get('error')}"


@mcp.tool()
def list_sources() -> str:
    """列出已入库来源（文件 + 页数 + 片段数 + 解析工具 + 入库时间）。"""
    srcs = _ft_list_sources()
    if not srcs:
        legacy = _cs_list_sources()
        if legacy:
            return (
                "（提示：以下来源为 v1 旧数据，无全文索引，read_range 不可用；请重新 ingest_file 建立索引。）\n"
                + "\n".join(f"- {s['source']}：{s['chunks']} 片段" for s in legacy)
            )
        return "知识库为空。"
    return "\n".join(
        f"- {s['source']}：{s['pages']} 页 / {s['chunks']} 片段（{s['parser']}，{s['ingested_at']}）"
        for s in srcs
    )


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    transport = "stdio"
    host = "127.0.0.1"
    port = 8000
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--http":
            transport = "http"
        elif a == "--streamable-http":
            transport = "streamable-http"
        elif a == "--stdio":
            transport = "stdio"
        elif a == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 1
        elif a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 1
        i += 1
    if transport in ("http", "streamable-http"):
        # 独立启动为 HTTP 服务，供外部 MCP 客户端连接（也可手动 curl/浏览器访问）。
        mcp.run(transport=transport, host=host, port=port)
    else:
        # 默认 stdio：供 dsh-mcp-client 以子进程方式连接。
        mcp.run(transport="stdio")
