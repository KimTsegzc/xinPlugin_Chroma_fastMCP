"""文件入库 CLI：python ingest.py <file> [source_name]

供 MinIO 插件上传后调用，也支持手动入库。输出 JSON 摘要便于联动方解析。
"""
import sys
import os
import json

from chroma_store import ingest_chunks, chunk_page_text, remove_source


def extract_pdf(path):
    import pypdf
    reader = pypdf.PdfReader(path)
    pages = []
    for i, page in enumerate(reader.pages):
        txt = page.extract_text() or ""
        pages.append((i + 1, txt))
    return pages


def extract_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return [(1, f.read())]


def ingest_file(path, source=None, reingest=True):
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    name = source or os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        pages = extract_pdf(path)
    else:
        pages = extract_text(path)

    if reingest:
        remove_source(name)

    all_chunks = []
    for page, txt in pages:
        if not txt.strip():
            continue
        all_chunks.extend(chunk_page_text(txt, name, page))
    n = ingest_chunks(all_chunks)
    return {"ok": True, "source": name, "chunks": n, "pages": len(pages)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "usage: python ingest.py <file> [source_name]"}, ensure_ascii=False))
        sys.exit(2)
    path = sys.argv[1]
    source = sys.argv[2] if len(sys.argv) > 2 else None
    r = ingest_file(path, source)
    print(json.dumps(r, ensure_ascii=False))
    sys.exit(0 if r.get("ok") else 1)
