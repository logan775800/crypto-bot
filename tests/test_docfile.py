"""上传文件解析 + AI 查询工具的测试（纯本地，不联网）。

重点锁三件事：
  1. 各种真实格式能认出来（JSON/JSONL/CSV/GBK/坏JSON）；
  2. 统计是**全量**算的，不是抽样——AI 靠它下整体结论；
  3. 路径错、字段错、没文件时给的是可读提示而不是异常。
"""
import json
import time
import pytest

from handlers import docfile as D


@pytest.fixture(autouse=True)
def _clean():
    D._CACHE.clear()
    yield
    D._CACHE.clear()


def _put(chat, name, obj_or_text):
    text = obj_or_text if isinstance(obj_or_text, str) else \
        json.dumps(obj_or_text, ensure_ascii=False)
    return D.put(chat, name, len(text.encode("utf-8")), text, "utf-8")


ROWS = [
    {"symbol": "BTCUSDT", "side": "Buy", "closedPnl": 10.0, "execType": "Trade"},
    {"symbol": "ETHUSDT", "side": "Sell", "closedPnl": -4.0, "execType": "Trade"},
    {"symbol": "BTCUSDT", "side": "Buy", "closedPnl": 6.0, "execType": "BustTrade"},
    {"symbol": "BTCUSDT", "side": "Sell", "closedPnl": -2.0, "execType": "Trade"},
]
NESTED = {"retCode": 0, "result": {"category": "linear", "list": ROWS}}


# ── 格式识别 ────────────────────────────────────────────────────
def test_parse_json_object():
    assert D.parse('{"a": 1}', "x.json")[0] == "json"


def test_parse_jsonl():
    kind, rows = D.parse('{"a":1}\n{"a":2}\n{"a":3}\n', "x.jsonl")
    assert kind == "jsonl" and len(rows) == 3


def test_parse_csv_into_records():
    kind, rows = D.parse("a,b\n1,2\n3,4\n", "x.csv")
    assert kind == "csv" and rows[0]["a"] == "1" and len(rows) == 2


def test_parse_tsv():
    kind, rows = D.parse("a\tb\n1\t2\n", "x.tsv")
    assert kind == "csv" and rows[0]["b"] == "2"


def test_broken_json_falls_back_to_text_not_crash():
    """坏 JSON 不能让整条链路炸，退化成纯文本仍可搜。"""
    assert D.parse('{"a": 1, oops}', "x.json")[0] == "text"


def test_decode_gbk():
    text, enc = D.decode("币种,盈亏\n比特币,100\n".encode("gbk"))
    assert text.startswith("币种") and enc in ("gbk", "gb18030")


def test_decode_utf8_bom():
    text, enc = D.decode("a,b\n".encode("utf-8-sig"))
    assert text.startswith("a,b")


def test_is_supported():
    class _D:
        def __init__(self, n, m):
            self.file_name, self.mime_type = n, m
    assert D.is_supported(_D("result.json", "application/json"))
    assert D.is_supported(_D("x.csv", None))
    assert D.is_supported(_D("noext", "text/plain"))
    assert not D.is_supported(_D("a.pdf", "application/pdf"))
    assert not D.is_supported(None)


# ── 路径导航 ────────────────────────────────────────────────────
def test_dig_nested_path():
    assert D.dig(NESTED, "result.list[0].symbol") == "BTCUSDT"
    assert D.dig(NESTED, "result.category") == "linear"


def test_dig_negative_index():
    assert D.dig(NESTED, "result.list[-1].closedPnl") == -2.0


def test_dig_missing_key_lists_available():
    with pytest.raises(KeyError) as ei:
        D.dig(NESTED, "result.nope")
    assert "category" in str(ei.value)


def test_dig_index_out_of_range():
    with pytest.raises(KeyError):
        D.dig(NESTED, "result.list[99]")


# ── 自动定位记录数组 ─────────────────────────────────────────────
def test_finds_records_under_common_wrappers():
    e = _put(1, "r.json", NESTED)
    out = D.run_tool(1, "file_fields", {})
    assert "closedPnl" in out and "4" in out, "应能自动钻进 result.list"


# ── 统计（AI 下整体结论的依据，必须是全量）────────────────────────
def test_stats_numeric_is_full_scan():
    _put(1, "r.json", ROWS)
    out = D.run_tool(1, "file_stats", {"field": "closedPnl"})
    assert "合计 10" in out          # 10-4+6-2 = 10
    assert ">0 的 2" in out and "<0 的 2" in out


def test_stats_categorical_distribution():
    _put(1, "r.json", ROWS)
    out = D.run_tool(1, "file_stats", {"field": "symbol"})
    assert "BTCUSDT" in out and "3" in out


def test_stats_numeric_strings_are_counted():
    """CSV 读出来全是字符串，'12.5' 必须当数值统计，否则整份 CSV 没法算。"""
    _put(1, "t.csv", "pnl\n12.5\n-3.5\n")
    out = D.run_tool(1, "file_stats", {"field": "pnl"})
    assert "合计 9" in out


def test_stats_unknown_field_lists_available():
    _put(1, "r.json", ROWS)
    out = D.run_tool(1, "file_stats", {"field": "nope"})
    assert "没有字段" in out and "closedPnl" in out


# ── 搜索 / 取值 ─────────────────────────────────────────────────
def test_search_counts_all_matches():
    _put(1, "r.json", ROWS)
    out = D.run_tool(1, "file_search", {"keyword": "btcusdt"})   # 不区分大小写
    assert "命中 3" in out


def test_search_miss_is_explicit():
    _put(1, "r.json", ROWS)
    assert "没搜到" in D.run_tool(1, "file_search", {"keyword": "zzz"})


def test_head_limit_is_capped():
    _put(1, "r.json", [{"i": i} for i in range(200)])
    out = D.run_tool(1, "file_head", {"n": 9999})
    assert out.count('"i"') <= 50, "n 必须被夹到上限，防止一次吐爆上下文"


def test_tool_output_is_length_capped():
    big = [{"note": "x" * 500, "i": i} for i in range(500)]
    _put(1, "r.json", big)
    out = D.run_tool(1, "file_head", {"n": 50})
    assert len(out) <= D.MAX_TOOL_CHARS + 200


# ── 缓存生命周期 ────────────────────────────────────────────────
def test_no_file_gives_actionable_message():
    out = D.run_tool(999, "file_info", {})
    assert "没有已上传的文件" in out


def test_cache_expires():
    _put(1, "r.json", ROWS)
    D._CACHE["1"]["ts"] = time.time() - D.FILE_TTL - 1
    assert D.get(1) is None


def test_cache_is_per_chat():
    _put(1, "a.json", ROWS)
    assert D.get(2) is None


def test_cache_evicts_oldest_when_full():
    for i in range(D.MAX_CACHED_CHATS + 5):
        _put(i, "f.json", ROWS)
    assert len(D._CACHE) <= D.MAX_CACHED_CHATS


def test_unknown_tool_name():
    _put(1, "r.json", ROWS)
    assert "未知文件工具" in D.run_tool(1, "file_nope", {})


def test_tool_errors_never_raise():
    """工具执行器必须吞掉一切异常——抛出去会中断整个 AI 对话。"""
    _put(1, "r.json", ROWS)
    for name in D.TOOL_NAMES:
        out = D.run_tool(1, name, {"field": None, "path": "!!bad!!",
                                   "keyword": None, "n": "x", "limit": None})
        assert isinstance(out, str)


# ── 摘要 ────────────────────────────────────────────────────────
def test_digest_reports_count_and_fields():
    e = _put(1, "r.json", NESTED)
    d = D.digest(e)
    assert "r.json" in d and "结构轮廓" in d or "记录" in d


def test_digest_of_plain_text():
    e = _put(1, "a.log", "line1\nline2\nline3\n")
    assert "纯文本" in D.digest(e)


def test_tools_schema_matches_dispatch():
    """schema 里声明的工具名必须都能执行，否则模型调了会得到「未知工具」。"""
    declared = {t["function"]["name"] for t in D.TOOLS}
    assert declared == D.TOOL_NAMES
