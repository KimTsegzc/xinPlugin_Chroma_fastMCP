"""文件入库 CLI：python ingest.py <file> [source_name]

供 MinIO 插件上传后调用，也支持手动入库。输出 JSON 摘要便于联动方解析。

v2 起入库同时做两件事：
  1) 分块 → 向量入库（Chroma），供 search 粗定位；
  2) 写全文索引（fulltext.sqlite3），供 read_range 精读回退（两段式检索的「原文」侧）。

支持的格式（自包含解析层，不依赖外部技能/运行库）：
  - .pdf   优先内嵌 pdftotext.exe（含中文 CMap），其次 pdfminer.six（pip，中文干净），最后 pypdf 兜底
  - .docx  zip+XML 读 word/document.xml（段落/表格单元格分行），零依赖
  - .ofd   国标公文 zip+XML 提取 TextCode（按页码排序），零依赖
  - .md/.txt/.csv/.json/.html 等纯文本：自动识别 UTF-8 / GBK
  - 其他（.xlsx/.pptx/图片/旧版 .doc 等）→ unsupported，返回「暂不支持向量化」
"""
import sys
import os
import re
import json
import html as _html
import shutil
import zipfile
import tempfile
import subprocess
import time
from uuid import uuid4

__version__ = "2.0.0"

# ---- 文件类型判定 ------------------------------------------------------------
PDF_EXTS = ('.pdf',)
DOCX_EXTS = ('.docx',)
OFD_EXTS = ('.ofd',)
# 纯文本类：整份按 UTF-8/GBK 读
TEXT_EXTS = ('.txt', '.md', '.markdown', '.csv', '.json', '.yaml', '.yml', '.xml', '.html', '.htm', '.log', '.toml', '.ini', '.conf', '.env', '.cfg', '.rtf', '.js', '.mjs', '.ts', '.tsx', '.jsx', '.py', '.go', '.java', '.c', '.cpp', '.h', '.cs', '.rs', '.rb', '.php', '.swift', '.kt', '.scala', '.sh', '.ps1', '.bat', '.sql', '.graphql', '.css', '.scss', '.less')

SUPPORTED_DESC = 'pdf / docx / ofd / txt/md / csv / json / html 等纯文本'


def _kind(ext):
    e = (ext or '').lower()
    if e in PDF_EXTS:
        return 'pdf'
    if e in DOCX_EXTS:
        return 'docx'
    if e in OFD_EXTS:
        return 'ofd'
    if e in TEXT_EXTS:
        return 'text'
    return 'unsupported'


# ---- 纯文本（自动 UTF-8 / GBK）---------------------------------------------
def extract_text_file(path):
    with open(path, 'rb') as f:
        raw = f.read()
    try:
        s = raw.decode('utf-8')
    except UnicodeDecodeError:
        s = raw.decode('gbk', 'replace')
    if s.startswith('\ufeff'):
        s = s[1:]
    return [(1, s)]


# ---- .docx（zip + XML，零依赖）---------------------------------------------
def extract_docx(path):
    with zipfile.ZipFile(path) as z:
        try:
            xml = z.read('word/document.xml').decode('utf-8', 'replace')
        except KeyError:
            return ''
    text = re.sub(r'</w:tc>', '\n', xml)      # 表格单元格边界
    text = re.sub(r'<w:p[ >]', '\n', text)    # 段落边界
    text = re.sub(r'<w:tab[ /]', '\t', text)  # 制表符
    text = re.sub(r'<[^>]+>', '', text)        # 去 XML 标签
    text = _html.unescape(text)
    return text


# ---- .ofd（国标公文 zip + XML，提取 TextCode，按页码排序）--------------------
def extract_ofd(path):
    parts = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith('.xml')]

        def page_key(n):
            m = re.search(r'Page_(\d+)', n)
            return int(m.group(1)) if m else 10 ** 9

        for n in sorted(names, key=page_key):
            xml = z.read(n).decode('utf-8', 'replace')
            for m in re.finditer(r'<(?:[A-Za-z0-9_]+:)?TextCode\b[^>]*>(.*?)</(?:[A-Za-z0-9_]+:)?TextCode>', xml, re.S):
                parts.append(_html.unescape(m.group(1)))
    return '\n'.join(parts)


# ---- .pdf -----------------------------------------------------------------
def _find_pdftotext():
    env = os.environ.get('XIN_PDFTOTEXT')
    if env and os.path.isfile(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (os.path.join(here, 'bin', 'pdftotext.exe'), os.path.join(here, 'bin', 'pdftotext')):
        if os.path.isfile(c):
            return c
    p = shutil.which('pdftotext')
    if p:
        return p
    return None


def _build_xpdfcfg(cs_dir):
    lines = []
    add = os.path.join(cs_dir, 'add-to-xpdfrc')
    if os.path.isfile(add):
        with open(add, encoding='utf-8', errors='ignore') as f:
            for ln in f:
                s = ln.strip()
                if not s or s.startswith('#'):
                    continue
                if not re.match(r'^\s*(cidToUnicode|unicodeMap|cMapDir|toUnicodeDir)\b', s):
                    continue
                m = re.match(r'^(\s*\S+\s+\S+\s+)(\S+)(.*)$', s)
                if m:
                    local = os.path.join(cs_dir, os.path.basename(m.group(2)))
                    if os.path.isfile(local):
                        lines.append(m.group(1) + local)
                    else:
                        lines.append(s)
                else:
                    lines.append(s)
    if not lines:
        cid = os.path.join(cs_dir, 'Adobe-GB1.cidToUnicode')
        cmap = os.path.join(cs_dir, 'CMap')
        if os.path.isfile(cid):
            lines.append('cidToUnicode Adobe-GB1 ' + cid)
        if os.path.isdir(cmap):
            lines.append('cMapDir Adobe-GB1 ' + cmap)
        if os.path.isdir(cs_dir):
            for fn in os.listdir(cs_dir):
                if fn.endswith('.unicodeMap'):
                    lines.append('unicodeMap ' + os.path.splitext(fn)[0] + ' ' + os.path.join(cs_dir, fn))
    return '\n'.join(lines)


def extract_pdf(path):
    exe = _find_pdftotext()
    if exe:
        tmp = os.path.join(tempfile.gettempdir(), 'kb_' + uuid4().hex + '.txt')
        cfg = None
        cs = os.path.join(os.path.dirname(exe), 'xpdf-data', 'chinese-simplified')
        try:
            if os.path.isdir(cs):
                cfg = os.path.join(tempfile.gettempdir(), 'kb_' + uuid4().hex + '.xpdfrc')
                with open(cfg, 'w', encoding='utf-8') as f:
                    f.write(_build_xpdfcfg(cs))
                subprocess.run([exe, '-cfg', cfg, '-enc', 'UTF-8', '-raw', path, tmp], capture_output=True)
            else:
                subprocess.run([exe, '-enc', 'UTF-8', '-raw', path, tmp], capture_output=True)
            if os.path.exists(tmp):
                with open(tmp, encoding='utf-8', errors='replace') as f:
                    t = f.read()
                pages_raw = t.split('\x0c')
                pages = [(i + 1, p) for i, p in enumerate(pages_raw) if p.strip()]
                if pages:
                    return pages, 'pdftotext'
        finally:
            if cfg and os.path.exists(cfg):
                os.remove(cfg)
            if os.path.exists(tmp):
                os.remove(tmp)
    # pdfminer.six：内置中文 CMap，中文 PDF 提取干净（无需外部二进制）
    try:
        from pdfminer.high_level import extract_text
        whole = extract_text(path)
        pages = [(i + 1, p) for i, p in enumerate(whole.split('\x0c')) if p.strip()]
        if pages:
            return pages, 'pdfminer'
    except Exception:
        pass
    import pypdf
    reader = pypdf.PdfReader(path)
    pages = []
    for i, page in enumerate(reader.pages):
        pages.append((i + 1, page.extract_text() or ''))
    return pages, 'pypdf'


# ---- 汇总：按类型返回 (kind, pages|None, parser) ------------------------------
def extract(path, ext=None):
    # 插件上传时临时文件是 .minio-upload.bin，真实类型要看源文件名后缀
    ext = (ext or os.path.splitext(path)[1]).lower()
    k = _kind(ext)
    if k == 'pdf':
        pages, parser = extract_pdf(path)
        return k, pages, parser
    if k == 'docx':
        return k, [(1, extract_docx(path))], 'docx(zip+xml)'
    if k == 'ofd':
        return k, [(1, extract_ofd(path))], 'ofd(zip+xml TextCode)'
    if k == 'text':
        return k, extract_text_file(path), 'text(utf-8/gbk)'
    return 'unsupported', None, None


# ---- 入库 -------------------------------------------------------------------
def ingest_file(path, source=None, reingest=True):
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    name = source or os.path.basename(path)
    # 插件解码后的临时文件是 .minio-upload.bin，真实类型取源文件名后缀
    ext = os.path.splitext(name)[1].lower()

    t = time.perf_counter()
    k, pages, parser = extract(path, ext)
    parse_ms = int(round((time.perf_counter() - t) * 1000))
    if k == 'unsupported':
        return {
            "ok": False,
            "unsupported": True,
            "source": name,
            "ext": ext,
            "parser": None,
            "parse_ms": parse_ms,
            "ingest_ms": 0,
            "error": f"暂不支持向量化（{ext or '未知类型'}）：支持 {SUPPORTED_DESC}",
        }
    if not pages or all(not (t or '').strip() for _, t in pages):
        return {
            "ok": False,
            "unsupported": True,
            "source": name,
            "ext": ext,
            "parser": parser,
            "parse_ms": parse_ms,
            "ingest_ms": 0,
            "error": "未能提取到文本（可能是扫描件/图片型文档，无文字层）",
        }

    if reingest:
        remove_source(name)

    all_chunks = []
    for page, txt in pages:
        if not (txt or '').strip():
            continue
        all_chunks.extend(chunk_page_text(txt, name, page))
    if not all_chunks:
        return {"ok": False, "unsupported": True, "source": name, "ext": ext, "parser": parser, "parse_ms": parse_ms, "ingest_ms": 0, "error": "提取到文本但未生成可入库片段"}
    t1 = time.perf_counter()
    n = ingest_chunks(all_chunks)
    ingest_ms = int(round((time.perf_counter() - t1) * 1000))

    # v2：同步写全文索引（精读回退），行号 = 原始页文本 1-based 行，与分块行号对齐。
    lines_by_page = {}
    for page, txt in pages:
        lines_by_page[page] = (txt or '').split('\n')
    ft = upsert_source(name, len(pages), n, parser, lines_by_page)

    return {
        "ok": True,
        "source": name,
        "chunks": n,
        "pages": len(pages),
        "parser": parser,
        "parse_ms": parse_ms,
        "ingest_ms": ingest_ms,
        "fulltext": ft,
    }


# ---- 延迟导入 chroma_store（避免 unsupported 类型也必须装 chromadb）----------
def chunk_page_text(text, source, page, lines_per_chunk=6, overlap=1):
    from chroma_store import chunk_page_text as _cpt
    return _cpt(text, source, page, lines_per_chunk=lines_per_chunk, overlap=overlap)


def ingest_chunks(chunks):
    from chroma_store import ingest_chunks as _ic
    return _ic(chunks)


def remove_source(source):
    from chroma_store import remove_source as _rs
    return _rs(source)


def upsert_source(source, pages, chunks, parser, lines_by_page):
    """写入全文索引（v2 精读回退），延迟导入避免 unsupported 类型也依赖 fulltext。"""
    from fulltext import upsert_source as _fu
    return _fu(source, pages, chunks, parser, lines_by_page)


if __name__ == "__main__":
    # Windows 中文环境默认用 GBK 输出；插件按 UTF-8 解析子进程 stdout，会导致中文乱码。强制 UTF-8。
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "usage: python ingest.py <file> [source_name]"}, ensure_ascii=False))
        sys.exit(2)
    path = sys.argv[1]
    source = sys.argv[2] if len(sys.argv) > 2 else None
    r = ingest_file(path, source)
    print(json.dumps(r, ensure_ascii=False))
    sys.exit(0 if r.get("ok") else 1)
