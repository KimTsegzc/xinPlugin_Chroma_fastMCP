"""全文索引层（精读回退）——两段式检索的「原文」侧。

两段式闭环：
  ① search()   向量/BM25 粗定位 → 命中 source + page + line_start/line_end
  ② read_range() 按 source+page+line 读原文片段（可扩上下文）→ 精读 + 标注出处

向量库（Chroma）只存「片段」，无法精读；精读必须回到解析层维护的全文索引。
本层用标准库 sqlite3 持久化（零额外依赖）。

行号语义（关键对齐）：
  chroma_store.chunk_page_text 以「原始页文本按 \\n 切分的 1-based 行号」作为
  line_start/line_end（噪声行被过滤但保留原始行号）。本层 lines 表存「原始页文本
  的每一行（含噪声行），1-based」，因此 search 命中的 line_start 可直接喂给
  read_range，两段式定位严格对齐。
"""
import os
import sqlite3
import threading
from contextlib import closing

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fulltext.sqlite3")
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  source TEXT PRIMARY KEY,
  pages INTEGER NOT NULL,
  chunks INTEGER NOT NULL,
  parser TEXT,
  ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lines (
  source TEXT NOT NULL,
  page INTEGER NOT NULL,
  line INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (source, page, line)
);
"""


def _run(fn):
    with _lock:
        with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
            con.executescript(_SCHEMA)
            con.commit()
            return fn(con)


def upsert_source(source, pages, chunks, parser, lines_by_page):
    """写入/覆盖某来源的全文索引（幂等，重复入库覆盖更新）。

    lines_by_page: {page: [line_text, ...]}，1-based 原始行（含噪声行）。
    """

    def go(con):
        con.execute("DELETE FROM lines WHERE source=?", (source,))
        con.execute("DELETE FROM sources WHERE source=?", (source,))
        con.execute(
            "INSERT INTO sources (source, pages, chunks, parser, ingested_at) "
            "VALUES (?,?,?,?,datetime('now','localtime'))",
            (source, int(pages), int(chunks), parser),
        )
        rows = []
        for page in sorted(lines_by_page):
            for i, t in enumerate(lines_by_page[page], 1):
                rows.append((source, int(page), i, t))
        con.executemany(
            "INSERT INTO lines (source, page, line, text) VALUES (?,?,?,?)", rows
        )
        con.commit()
        return {"ok": True, "source": source, "pages": int(pages), "lines": len(rows)}

    return _run(go)


def remove_source(source):
    """删除某来源的全文索引（供 reingest 前清理，通常 upsert 已覆盖，此函数备用）。"""

    def go(con):
        con.execute("DELETE FROM lines WHERE source=?", (source,))
        con.execute("DELETE FROM sources WHERE source=?", (source,))
        con.commit()

    return _run(go)


def list_sources():
    """列出已建立全文索引的来源（含页数/片段数/解析工具/入库时间）。"""

    def go(con):
        cur = con.execute(
            "SELECT source, pages, chunks, parser, ingested_at FROM sources ORDER BY source"
        )
        return [
            {
                "source": r[0],
                "pages": r[1],
                "chunks": r[2],
                "parser": r[3],
                "ingested_at": r[4],
            }
            for r in cur.fetchall()
        ]

    return _run(go)


def _resolve_source(con, source):
    """精确匹配 source；失败则包含匹配（唯一命中才返回），否则 None。"""
    if not source:
        return None
    hit = con.execute(
        "SELECT DISTINCT source FROM lines WHERE source=?", (source,)
    ).fetchone()
    if hit:
        return hit[0]
    like = con.execute(
        "SELECT DISTINCT source FROM lines WHERE source LIKE ?", ("%" + source + "%",)
    ).fetchall()
    if len(like) == 1:
        return like[0][0]
    return None


def _page_count(con, source):
    r = con.execute(
        "SELECT MAX(page) FROM lines WHERE source=?", (source,)
    ).fetchone()
    return r[0] or 0


def read_range(source, page, line_start=1, line_end=-1, context=0):
    """按 来源+页+行区间 读原文，返回带行号的原文行列表。

    line_end=-1 表示读到页尾；context 表示在行区间上下各多读的行数。
    """

    def go(con):
        src = _resolve_source(con, source)
        if not src:
            return {
                "ok": False,
                "error": (
                    f"全文索引中未找到来源「{source}」。"
                    "可先 list_sources 查看已索引来源；若为 v1 旧数据请重新 ingest_file 建立索引。"
                ),
            }
        pages = _page_count(con, src)
        pg = int(page)
        n = con.execute(
            "SELECT COUNT(*) FROM lines WHERE source=? AND page=?", (src, pg)
        ).fetchone()[0]
        if n == 0:
            return {
                "ok": False,
                "error": f"「{src}」无第 {pg} 页（共 {pages} 页）",
                "source": src,
                "pages": pages,
            }
        ctx = int(context or 0)
        lo = max(1, int(line_start) - ctx)
        hi = int(line_end) if int(line_end) > 0 else n
        hi = min(n, hi + ctx)
        rows = con.execute(
            "SELECT line, text FROM lines WHERE source=? AND page=? AND line BETWEEN ? AND ? ORDER BY line",
            (src, pg, lo, hi),
        ).fetchall()
        return {
            "ok": True,
            "source": src,
            "page": pg,
            "pages": pages,
            "line_start": lo,
            "line_end": hi,
            "page_lines": n,
            "lines": [{"line": r[0], "text": r[1]} for r in rows],
        }

    return _run(go)
