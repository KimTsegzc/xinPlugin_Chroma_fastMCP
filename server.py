"""Chroma 知识库 FastMCP 服务（stdio 模式）。

暴露工具：
- search(query, top_k)：语义检索，返回带出处（文件 + 页码 + 行号）的原文片段。
- ingest_file(path, source_name)：把本地文件入库（PDF/txt/md）。
- list_sources()：列出已入库来源。

DSH 通过 dsh-mcp-client 以 stdio 连接本服务，工具名形如 mcp__chroma__search。
"""
from fastmcp import FastMCP
from chroma_store import search as _search
from chroma_store import list_sources as _list_sources
from ingest import ingest_file as _ingest_file

mcp = FastMCP("chroma-kb", instructions="MinIO 知识库向量检索服务（Chroma）。")


@mcp.tool()
def search(query: str, top_k: int = 6) -> str:
    """按语义检索知识库，返回最相关的原文片段及出处（文件/页码/行号）。

    适用于：用户就知识库文档提问时，先检索原文再作答，务必在回答里标注出处。
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
    return "\n".join(lines)


@mcp.tool()
def ingest_file(path: str, source_name: str = "") -> str:
    """把本地文件（PDF/txt/md）入库到向量知识库，供后续 search 检索。"""
    r = _ingest_file(path, source_name or None)
    if r.get("ok"):
        return f"入库完成：{r['source']} 共 {r['chunks']} 个片段（{r['pages']} 页）。"
    return f"入库失败：{r.get('error')}"


@mcp.tool()
def list_sources() -> str:
    """列出已入库的来源文件及片段数量。"""
    srcs = _list_sources()
    if not srcs:
        return "知识库为空。"
    return "\n".join(f"- {s['source']}：{s['chunks']} 片段" for s in srcs)


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
