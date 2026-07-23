"""用户上传的数据文件（JSON / CSV / 文本）解析 + 给 AI 的按需查询工具。

设计原则和 marketdata.py 一致：**不把整份文件丢给模型**。
一份 1.3MB 的 result.json 直接进上下文既烧 token、又超窗口，模型还会算错。
这里把文件留在服务端，先给模型一份「结构摘要」（有哪些字段、什么类型、多少条、
样例几条），再开放几个查询工具让它按需取数：取路径、抽样、搜关键字、算统计。
模型于是能分析远超上下文的文件，而每次只看几 KB。

文件缓存放模块级字典（不是 chat_data）——chat_data 会被 PicklePersistence 落盘，
把上兆的文件写进 pickle 每次都要序列化，得不偿失。代价是重启后缓存丢失，可接受。
"""
import csv
import io
import json
import re
import time
import logging

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024   # Telegram getFile 下载上限就是 20MB
FILE_TTL = 3600                     # 缓存 1 小时
MAX_CACHED_CHATS = 20               # 防止内存无限涨
MAX_TOOL_CHARS = 3500               # 单次工具返回给模型的上限

TEXT_EXT = (".json", ".csv", ".tsv", ".txt", ".log", ".md", ".yaml", ".yml",
            ".ndjson", ".jsonl")
TEXT_MIME = ("application/json", "text/", "application/x-ndjson",
             "application/csv", "application/x-yaml")

_CACHE = {}   # {chat_id: {name, size, kind, text, parsed, ts}}


# ── 判定 / 解码 ──────────────────────────────────────────────────
def is_supported(doc):
    """这个 document 是不是我们能读的文本类文件。"""
    if not doc:
        return False
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    if name.endswith(TEXT_EXT):
        return True
    return any(mime.startswith(m) or mime == m for m in TEXT_MIME)


def decode(raw):
    """按 utf-8 → utf-8-sig → gbk 依次尝试。都不行就 utf-8 忽略错误。

    国内导出的 CSV 十有八九是 GBK，硬按 utf-8 解会整份乱码。
    """
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            # 必须剥 BOM：Windows 导出的 CSV 几乎都带 BOM，不剥的话首个字段名会变成
            # "﻿symbol"，之后所有按字段名的查询/统计都会静默失配。
            return raw.decode(enc).lstrip("﻿"), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace").lstrip("﻿"), "utf-8(有损)"


def parse(text, name=""):
    """返回 (kind, parsed)。kind ∈ json / jsonl / csv / text。解析不了就当纯文本。"""
    low = (name or "").lower()
    stripped = text.lstrip()
    # JSON
    if low.endswith(".json") or stripped[:1] in ("{", "["):
        try:
            return "json", json.loads(text)
        except json.JSONDecodeError:
            pass
    # JSON Lines（每行一个对象）
    if low.endswith((".jsonl", ".ndjson")) or (
            stripped[:1] == "{" and "\n{" in text[:2000]):
        rows, bad = [], 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
        if rows and bad <= len(rows) * 0.1:
            return "jsonl", rows
    # CSV / TSV
    if low.endswith((".csv", ".tsv")):
        delim = "\t" if low.endswith(".tsv") else None
        try:
            if delim is None:
                try:
                    delim = csv.Sniffer().sniff(text[:8000],
                                                delimiters=",;\t|").delimiter
                except csv.Error:
                    # 单列 CSV 探不出分隔符——那是合法文件，别整份退化成纯文本
                    delim = ","
            rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
            if rows:
                return "csv", rows
        except Exception as e:
            log.warning(f"CSV 解析失败，退化为纯文本: {e}")
    return "text", text


# ── 结构摘要 ────────────────────────────────────────────────────
def _tname(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def _sample_val(v, maxlen=40):
    s = json.dumps(v, ensure_ascii=False, default=str) if not isinstance(v, str) else v
    s = s.replace("\n", " ")
    return s[:maxlen] + ("…" if len(s) > maxlen else "")


def _schema_of_records(rows, limit=200):
    """一组同构记录 → 字段清单（类型、缺失率、样例值）。只扫前 limit 条。"""
    scan = rows[:limit]
    fields = {}
    for r in scan:
        if not isinstance(r, dict):
            continue
        for k, v in r.items():
            f = fields.setdefault(k, {"types": set(), "nonnull": 0, "sample": None})
            f["types"].add(_tname(v))
            if v is not None and v != "":
                f["nonnull"] += 1
                if f["sample"] is None:
                    f["sample"] = _sample_val(v)
    out = []
    n = max(1, len(scan))
    for k, f in fields.items():
        types = "/".join(sorted(f["types"]))
        miss = 100 - f["nonnull"] * 100 // n
        out.append(f"  · {k} ({types})"
                   + (f" 缺失{miss}%" if miss else "")
                   + (f" 例:{f['sample']}" if f["sample"] is not None else ""))
    return out


def _outline(obj, depth=0, max_depth=3):
    """任意 JSON 的树状轮廓（键 + 类型 + 规模），不展开大数组。"""
    pad = "  " * depth
    if isinstance(obj, dict):
        lines = []
        for k, v in list(obj.items())[:40]:
            if isinstance(v, dict):
                lines.append(f"{pad}· {k} (dict, {len(v)}键)")
                if depth < max_depth:
                    lines += _outline(v, depth + 1, max_depth)
            elif isinstance(v, list):
                inner = _tname(v[0]) if v else "空"
                lines.append(f"{pad}· {k} (list[{inner}], {len(v)}条)")
                if v and isinstance(v[0], dict) and depth < max_depth:
                    lines.append(f"{pad}  └ 元素字段: "
                                 + ", ".join(list(v[0].keys())[:15]))
            else:
                lines.append(f"{pad}· {k} ({_tname(v)}) = {_sample_val(v)}")
        if len(obj) > 40:
            lines.append(f"{pad}…共 {len(obj)} 个键")
        return lines
    if isinstance(obj, list):
        return [f"{pad}(list, {len(obj)}条, 元素类型 {_tname(obj[0]) if obj else '空'})"]
    return [f"{pad}{_tname(obj)} = {_sample_val(obj)}"]


def digest(entry):
    """给模型的首屏摘要：文件基本信息 + 结构 + 样例。"""
    kind, obj, name = entry["kind"], entry["parsed"], entry["name"]
    size_kb = entry["size"] / 1024
    head = [f"📄 文件 {name}｜{size_kb:,.0f} KB｜类型 {kind}｜编码 {entry.get('enc','?')}"]

    if kind == "text":
        lines = obj.splitlines()
        head.append(f"纯文本，{len(lines):,} 行 / {len(obj):,} 字符")
        head.append("开头 20 行：")
        head += ["  " + l[:120] for l in lines[:20]]
        return "\n".join(head)[:MAX_TOOL_CHARS]

    # 记录型（csv / jsonl / json数组）
    rows = obj if isinstance(obj, list) else None
    if rows is not None and rows and isinstance(rows[0], dict):
        head.append(f"共 {len(rows):,} 条记录，字段如下（扫前200条统计）：")
        head += _schema_of_records(rows)
        head.append("\n第 1 条样例：")
        head.append("  " + json.dumps(rows[0], ensure_ascii=False, default=str)[:600])
        return "\n".join(head)[:MAX_TOOL_CHARS]
    if rows is not None:
        head.append(f"数组，共 {len(rows):,} 条，元素类型 {_tname(rows[0]) if rows else '空'}")
        head.append("前 5 条：" + json.dumps(rows[:5], ensure_ascii=False, default=str)[:600])
        return "\n".join(head)[:MAX_TOOL_CHARS]

    head.append("结构轮廓：")
    head += _outline(obj)
    return "\n".join(head)[:MAX_TOOL_CHARS]


# ── 缓存 ────────────────────────────────────────────────────────
def put(chat_id, name, size, text, enc):
    kind, parsed = parse(text, name)
    _CACHE[str(chat_id)] = {"name": name, "size": size, "text": text,
                            "kind": kind, "parsed": parsed, "ts": time.time(),
                            "enc": enc}
    # 超量就先淘汰最旧的
    if len(_CACHE) > MAX_CACHED_CHATS:
        oldest = min(_CACHE, key=lambda k: _CACHE[k]["ts"])
        _CACHE.pop(oldest, None)
    return _CACHE[str(chat_id)]


def get(chat_id):
    e = _CACHE.get(str(chat_id))
    if not e:
        return None
    if time.time() - e["ts"] > FILE_TTL:
        _CACHE.pop(str(chat_id), None)
        return None
    return e


def clear(chat_id):
    return _CACHE.pop(str(chat_id), None) is not None


# ── 路径导航 ────────────────────────────────────────────────────
_IDX = re.compile(r"\[(-?\d+)\]")


def dig(obj, path):
    """按 a.b[0].c 取值。取不到抛 KeyError（带可读信息）。"""
    cur = obj
    if not path or path in (".", "$"):
        return cur
    for part in path.replace("$", "").strip(".").split("."):
        if not part:
            continue
        idxs = _IDX.findall(part)
        key = _IDX.sub("", part)
        if key:
            if isinstance(cur, dict):
                if key not in cur:
                    raise KeyError(f"没有键 {key}（可用：{', '.join(list(cur)[:12])}）")
                cur = cur[key]
            else:
                raise KeyError(f"{key} 不适用于 {_tname(cur)}")
        for i in idxs:
            if not isinstance(cur, list):
                raise KeyError(f"[{i}] 不适用于 {_tname(cur)}")
            i = int(i)
            if not -len(cur) <= i < len(cur):
                raise KeyError(f"下标 {i} 越界（共 {len(cur)} 条）")
            cur = cur[i]
    return cur


def _as_records(entry, path=""):
    """定位到一组记录（给 search/stats 用）。"""
    obj = dig(entry["parsed"], path) if path else entry["parsed"]
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        # 常见形态 {"data": [...]} / {"result": {"list": [...]}}
        for k in ("data", "list", "items", "records", "rows", "result"):
            v = obj.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in ("list", "data", "items"):
                    if isinstance(v.get(k2), list):
                        return v[k2]
    return None


def _clip(s):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)
    return s[:MAX_TOOL_CHARS] + ("\n…（已截断）" if len(s) > MAX_TOOL_CHARS else "")


# ── 给 AI 的工具实现 ─────────────────────────────────────────────
def tool_info(entry):
    return digest(entry)


def tool_get(entry, path="", limit=20):
    try:
        v = dig(entry["parsed"], path)
    except KeyError as e:
        return f"取不到路径 {path or '.'}：{e}"
    if isinstance(v, list):
        return (f"{path or '.'} 是数组，共 {len(v):,} 条，前 {min(limit, len(v))} 条：\n"
                + _clip(json.dumps(v[:limit], ensure_ascii=False,
                                   indent=1, default=str)))
    return f"{path or '.'} =\n" + _clip(json.dumps(v, ensure_ascii=False,
                                                   indent=1, default=str))


def tool_search(entry, keyword, limit=10, path=""):
    """在记录里搜关键字（不区分大小写），返回命中的记录。纯文本文件则搜行。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return "关键字不能为空"
    if entry["kind"] == "text":
        hits = [f"第{i+1}行: {l[:200]}"
                for i, l in enumerate(entry["parsed"].splitlines()) if kw in l.lower()]
        if not hits:
            return f"没搜到「{keyword}」"
        return f"命中 {len(hits)} 行，前 {min(limit, len(hits))} 条：\n" + \
            _clip("\n".join(hits[:limit]))
    rows = _as_records(entry, path)
    if rows is None:
        blob = json.dumps(entry["parsed"], ensure_ascii=False, default=str)
        return f"该文件不是记录数组；全文命中 {blob.lower().count(kw)} 次"
    hits = [r for r in rows
            if kw in json.dumps(r, ensure_ascii=False, default=str).lower()]
    if not hits:
        return f"{len(rows):,} 条里没搜到「{keyword}」"
    return (f"{len(rows):,} 条里命中 {len(hits):,} 条，前 {min(limit, len(hits))} 条：\n"
            + _clip(json.dumps(hits[:limit], ensure_ascii=False, indent=1, default=str)))


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").replace("%", "").strip())
        except ValueError:
            return None
    return None


def tool_stats(entry, field, path="", top=10):
    """对某字段做统计：数值型给 count/min/max/均值/求和/分位，其余给取值分布 TopN。"""
    rows = _as_records(entry, path)
    if rows is None:
        return "该文件不是记录数组，没法按字段统计（先用 file_get 看结构）"
    vals = [r.get(field) for r in rows if isinstance(r, dict) and field in r]
    if not vals:
        keys = sorted({k for r in rows[:200] if isinstance(r, dict) for k in r})
        return f"没有字段「{field}」。可用字段：{', '.join(keys[:30])}"
    nums = [n for n in (_num(v) for v in vals) if n is not None]
    out = [f"字段「{field}」：共 {len(rows):,} 条记录，其中 {len(vals):,} 条有该字段"]
    if len(nums) >= max(1, len(vals) * 0.6):     # 多数是数值 → 当数值统计
        nums.sort()
        n = len(nums)
        def q(p):
            return nums[min(n - 1, int(n * p))]
        out += [f"数值统计（{n:,} 个有效值）：",
                f"  合计 {sum(nums):,.6g}｜均值 {sum(nums)/n:,.6g}",
                f"  最小 {nums[0]:,.6g}｜P25 {q(.25):,.6g}｜中位 {q(.5):,.6g}"
                f"｜P75 {q(.75):,.6g}｜最大 {nums[-1]:,.6g}",
                f"  >0 的 {sum(1 for x in nums if x > 0):,} 个｜"
                f"<0 的 {sum(1 for x in nums if x < 0):,} 个"]
    else:
        cnt = {}
        for v in vals:
            k = _sample_val(v, 30)
            cnt[k] = cnt.get(k, 0) + 1
        ranked = sorted(cnt.items(), key=lambda x: -x[1])[:top]
        out.append(f"取值分布（共 {len(cnt):,} 种，Top{len(ranked)}）：")
        out += [f"  {k}: {c:,} 次 ({c*100/len(vals):.1f}%)" for k, c in ranked]
    return _clip("\n".join(out))


def tool_head(entry, n=10, path=""):
    if entry["kind"] == "text":
        lines = entry["parsed"].splitlines()[:n]
        return _clip("\n".join(lines))
    rows = _as_records(entry, path)
    if rows is None:
        return tool_get(entry, path, limit=n)
    return (f"共 {len(rows):,} 条，前 {min(n, len(rows))} 条：\n"
            + _clip(json.dumps(rows[:n], ensure_ascii=False, indent=1, default=str)))


def tool_fields(entry, path=""):
    rows = _as_records(entry, path)
    if rows is None:
        return "不是记录数组；结构轮廓：\n" + _clip("\n".join(_outline(entry["parsed"])))
    if not rows or not isinstance(rows[0], dict):
        return f"数组 {len(rows):,} 条，元素不是对象（类型 {_tname(rows[0]) if rows else '空'}）"
    return (f"共 {len(rows):,} 条记录，字段：\n" + _clip("\n".join(_schema_of_records(rows))))


# 给 chat.py 注册用的 OpenAI function schema
_PATH = {"type": "string",
         "description": "可选，定位到文件里的子结构，如 data.list 或 result[0].items；留空=根"}

TOOLS = [
    {"type": "function", "function": {
        "name": "file_info",
        "description": ("查看用户刚上传的文件：文件名、大小、类型、字段结构、记录数、样例。"
                        "用户提到「我传的文件/这个json/这份数据」时**先调这个**。"),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "file_fields",
        "description": "列出文件里记录的所有字段名、类型、缺失率、样例值。要按字段分析前先看这个。",
        "parameters": {"type": "object", "properties": {"path": _PATH}}}},
    {"type": "function", "function": {
        "name": "file_head",
        "description": "取文件开头 n 条记录（或 n 行）看实际内容。n 默认10，别一次要太多。",
        "parameters": {"type": "object", "properties": {
            "n": {"type": "integer", "description": "取几条，默认10，上限50"},
            "path": _PATH}}}},
    {"type": "function", "function": {
        "name": "file_get",
        "description": ("按路径取文件里的具体内容，如 data.list[0] 或 summary。"
                        "数组会只返回前 limit 条。"),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "路径，如 data.list[0].symbol"},
            "limit": {"type": "integer", "description": "数组最多返回几条，默认20"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "file_search",
        "description": ("在文件里搜关键字（不区分大小写），返回命中的记录/行数与样例。"
                        "找某个币、某个订单号、某类错误时用。"),
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "要搜的关键字"},
            "limit": {"type": "integer", "description": "最多返回几条，默认10"},
            "path": _PATH},
            "required": ["keyword"]}}},
    {"type": "function", "function": {
        "name": "file_stats",
        "description": ("对某个字段做统计：数值字段给 合计/均值/最小/P25/中位/P75/最大/正负个数，"
                        "文本字段给取值分布 TopN。要下「整体怎么样」的结论必须用它，别靠抽样几条硬猜。"),
        "parameters": {"type": "object", "properties": {
            "field": {"type": "string", "description": "字段名"},
            "path": _PATH,
            "top": {"type": "integer", "description": "文本字段返回前几名，默认10"}},
            "required": ["field"]}}},
]

_DISPATCH = {
    "file_info": lambda e, a: tool_info(e),
    "file_fields": lambda e, a: tool_fields(e, a.get("path", "")),
    "file_head": lambda e, a: tool_head(e, min(int(a.get("n") or 10), 50),
                                        a.get("path", "")),
    "file_get": lambda e, a: tool_get(e, a.get("path", ""),
                                      min(int(a.get("limit") or 20), 50)),
    "file_search": lambda e, a: tool_search(e, a.get("keyword", ""),
                                            min(int(a.get("limit") or 10), 30),
                                            a.get("path", "")),
    "file_stats": lambda e, a: tool_stats(e, a.get("field", ""), a.get("path", ""),
                                          min(int(a.get("top") or 10), 30)),
}

TOOL_NAMES = set(_DISPATCH)


def run_tool(chat_id, name, args):
    """chat.py 的工具执行器转发到这里。任何异常都转成说明文本，不抛。"""
    entry = get(chat_id)
    if not entry:
        return ("当前没有已上传的文件（可能没传、传的是不支持的格式，或已超过1小时过期）。"
                "请用户重新发一次文件。")
    fn = _DISPATCH.get(name)
    if not fn:
        return f"未知文件工具 {name}"
    try:
        return fn(entry, args or {})
    except Exception as e:
        log.warning(f"文件工具 {name} 出错: {e}")
        return f"（{name} 执行出错：{str(e)[:100]}）"
