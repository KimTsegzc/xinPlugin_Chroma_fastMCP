"""Chroma 知识库存储与检索核心（MinIO 上传 → 向量入库 → DSH 检索）。"""
import os
import re
import math
import hashlib
import chromadb

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_data")
COLLECTION = "knowledge_base"

_client = None


def get_client():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=DB_DIR)
    return _client


def get_collection():
    return get_client().get_or_create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


def _stable_id(source, page, line_start):
    h = hashlib.sha1(f"{source}\x00{page}\x00{line_start}".encode("utf-8")).hexdigest()[:16]
    return f"{source}_{page}_{line_start}_{h}"


_NOISE_PATTERNS = [
    "本文与正式文件同等效力",
    "广州市人民政府公报",
    "广州市人民政府文件",
    "穗府",
    "广州市国民经济和社会发展第十五个五年规划纲要",
]


def _norm(s):
    return re.sub(r"\s+", "", s)


def _is_noise_line(line):
    n = _norm(line)
    if not n:
        return True
    if n.isdigit():
        return True  # 页码
    for pat in _NOISE_PATTERNS:
        if pat in n:
            return True
    return False


def chunk_page_text(text, source, page, lines_per_chunk=6, overlap=1):
    """把一页文本按行切块，返回带 page/line 元数据的 chunks。

    过滤页眉/页脚/页码等噪声行，但保留原始页面行号，便于出处定位。
    """
    raw_lines = text.split("\n")
    # 保留非噪声行及其原始 1-based 行号
    entries = [(i + 1, ln) for i, ln in enumerate(raw_lines) if not _is_noise_line(ln)]

    chunks = []
    i = 0
    n = len(entries)
    while i < n:
        start = i
        j = i
        got = 0
        while j < n and got < lines_per_chunk:
            j += 1
            got += 1
        block = entries[start:j]
        text_lines = [ln for _, ln in block if ln.strip()]
        if not text_lines:
            i = j
            continue
        body = "\n".join(text_lines)
        line_start = block[0][0]
        line_end = block[-1][0]
        chunks.append({
            "id": _stable_id(source, page, line_start),
            "text": body,
            "metadata": {
                "source": source,
                "page": int(page),
                "line_start": int(line_start),
                "line_end": int(line_end),
            },
        })
        i = j - overlap if j - overlap > i else j
    return chunks


def ingest_chunks(chunks):
    """写入（可重复调用；同 id 覆盖更新）。"""
    if not chunks:
        return 0
    col = get_collection()
    col.upsert(
        ids=[c["id"] for c in chunks],
        documents=[c["text"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )
    _sparse_cache["built"] = False
    return len(chunks)


def search(query, top_k=6):
    """混合检索：语义检索 + 字符二元组 BM25 稀疏检索，用 RRF 融合。

    中文语义 embedding（all-MiniLM）偏弱，稀疏 BM25 能兜住「大模型/算力/数据要素」
    这类精确关键词，两者融合后召回更稳。返回 [{text, source, page, line_start, line_end, distance}]。
    """
    if not query:
        return []
    col = get_collection()
    if col.count() == 0:
        return []
    dense = _dense_search(col, query, top_k * 4)
    sparse = _sparse_search(query, top_k * 4)
    # RRF 融合
    rrf = {}
    for rank, r in enumerate(dense):
        rrf[r["id"]] = rrf.get(r["id"], 0) + 1.0 / (60 + rank)
    for rank, d in enumerate(sparse):
        rrf[d["id"]] = rrf.get(d["id"], 0) + 1.0 / (60 + rank)
    # 建立 id -> 记录 的映射
    by_id = {}
    for r in dense:
        by_id[r["id"]] = r
    for d in sparse:
        if d["id"] not in by_id:
            by_id[d["id"]] = {
                "id": d["id"], "text": d["text"], "source": d["meta"].get("source", ""),
                "page": int(d["meta"].get("page", 0)), "line_start": int(d["meta"].get("line_start", 0)),
                "line_end": int(d["meta"].get("line_end", 0)), "distance": None,
            }
    ranked = sorted(rrf.keys(), key=lambda k: -rrf[k])
    return [by_id[k] for k in ranked[:top_k]]


def _dense_search(col, query, top_k):
    res = col.query(query_texts=[query], n_results=min(top_k, col.count()))
    out = []
    ids = res.get("ids", [[]])[0]
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    for i, cid in enumerate(ids):
        m = metas[i] or {}
        out.append({
            "id": cid, "text": docs[i], "source": m.get("source", ""),
            "page": int(m.get("page", 0)), "line_start": int(m.get("line_start", 0)),
            "line_end": int(m.get("line_end", 0)),
            "distance": round(float(dists[i]), 4) if dists and i < len(dists) else None,
        })
    return out


# ---- 稀疏 BM25 检索（字符二元组） ----
_sparse_cache = {"built": False, "docs": [], "df": {}, "N": 0, "avg_len": 0}


def _tokenize(text):
    s = re.sub(r"\s+", "", text)
    if len(s) < 2:
        return [s] if s else []
    return [s[i:i + 2] for i in range(len(s) - 1)]


def _build_sparse():
    if _sparse_cache["built"]:
        return
    col = get_collection()
    docs = []
    df = {}
    total_len = 0
    if col.count() > 0:
        res = col.get(include=["metadatas", "documents"])
        ids = res.get("ids") or []
        documents = res.get("documents") or []
        metas = res.get("metadatas") or []
        for i, cid in enumerate(ids):
            toks = _tokenize(documents[i])
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
            docs.append({"id": cid, "text": documents[i], "meta": metas[i] or {}, "toks": toks})
            total_len += len(toks)
    _sparse_cache["built"] = True
    _sparse_cache["docs"] = docs
    _sparse_cache["df"] = df
    _sparse_cache["N"] = len(docs)
    _sparse_cache["avg_len"] = (total_len / len(docs)) if docs else 0


def _sparse_search(query, top_k):
    _build_sparse()
    docs = _sparse_cache["docs"]
    if not docs:
        return []
    N = _sparse_cache["N"]
    df = _sparse_cache["df"]
    avg = _sparse_cache["avg_len"]
    k1, b = 1.5, 0.75
    q_toks = set(_tokenize(query))
    if not q_toks:
        return []
    scored = []
    for d in docs:
        tl = len(d["toks"]) or 1
        tf = {}
        for t in d["toks"]:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for t in q_toks:
            if t not in tf:
                continue
            n = df.get(t, 0)
            idf = math.log(1 + (N - n + 0.5) / (n + 0.5))
            tfn = tf[t]
            score += idf * tfn * (k1 + 1) / (tfn + k1 * (1 - b + b * tl / avg))
        if score > 0:
            scored.append((score, d))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored[:top_k]]


def list_sources():
    """列出已入库的来源文件与块数。"""
    col = get_collection()
    if col.count() == 0:
        return []
    res = col.get(include=["metadatas"])
    counts = {}
    for m in (res.get("metadatas") or []):
        if not m:
            continue
        s = m.get("source", "?")
        counts[s] = counts.get(s, 0) + 1
    return [{"source": k, "chunks": v} for k, v in sorted(counts.items())]


def remove_source(source):
    """删除某来源的全部块。"""
    col = get_collection()
    if col.count() == 0:
        return 0
    res = col.get(include=["metadatas"])
    ids = res.get("ids") or []
    metas = res.get("metadatas") or []
    doomed = [i for i, m in zip(ids, metas) if m and m.get("source") == source]
    if doomed:
        col.delete(ids=doomed)
        _sparse_cache["built"] = False
    return len(doomed)
