"""多空比极值榜 /lsr —— 全市场最被看多/最被看空各 3 个。

需求来自群里：「多空比　数据最大和最小的 top3 列出来」。

三条护栏，都是探针实测或真机第一跑抓到的：
  1. **三家的数字对不上**（同一时刻 ETH：币安 2.69 / OKX 1.33 / Bybit 1.86），
     所以是**换所不是合并**——合并出来的"最看多 Top3"其实是"哪家用户最偏多"；
  2. **以 1 为界切开**，不是取头 3 + 取尾 3。池子里没有真正偏空的币时，
     取尾会把 `ZRO 1.01`（多头略多）列进「最被看空」——真机第一跑 Bybit 就这样；
  3. **代币化美股/商品要剔**。第一版漏了，「最被看多」榜首是 SOXL
     （3 倍做多半导体 ETF），第二档还有 CL（原油）。
"""
import pytest

from handlers import lsratio as L


def row(sym, ratio, turnover=1e8):
    return {"sym": sym, "inst": sym + "USDT", "ratio": ratio,
            "long_share": ratio / (1 + ratio), "turnover": turnover}


# ── 读数：0.43 要说成"空头是多头的 2.3 倍" ────────────────────
def test_ratio_below_one_is_explained_as_a_multiple():
    """小于 1 的比值人脑读不出量级——他截图里问的就是这件事。"""
    assert L.read(0.43) == "空头是多头的 2.3 倍"
    assert L.read(2.5) == "多头是空头的 2.5 倍"
    assert L.read(1.0) == "多头是空头的 1.0 倍"


@pytest.mark.parametrize("ratio,word", [
    (3.0, "多头极度拥挤"), (1.8, "多头拥挤"), (1.0, "两边接近"),
    (0.6, "空头拥挤"), (0.3, "空头极度拥挤"),
])
def test_tag(ratio, word):
    assert L.tag(ratio) == word


# ── 分组：以 1 为界，不是取头尾 ───────────────────────────────
def test_groups_split_at_one_not_head_and_tail():
    """全都偏多时，「最被看空」必须是空的，而不是把 1.01 塞进去。"""
    rows = sorted([row("A", 3.0), row("B", 2.0), row("C", 1.5),
                   row("D", 1.2), row("ZRO", 1.01)],
                  key=lambda r: -r["ratio"])
    txt = L.build_text(rows, "bybit", {"ok": 5})
    assert "最被看空*（0）" in txt
    assert "ZRO" not in txt, "1.01 是多头略占优，不能列进最被看空"


def test_groups_split_when_all_bearish():
    rows = sorted([row("A", 0.3), row("B", 0.5), row("C", 0.9)],
                  key=lambda r: -r["ratio"])
    txt = L.build_text(rows, "binance", {"ok": 3})
    assert "最被看多*（0）" in txt
    assert "最被看空 Top3" in txt


def test_bearish_group_is_worst_first():
    rows = sorted([row("MILD", 0.9), row("WORST", 0.2), row("MID", 0.5),
                   row("LONG", 2.0)], key=lambda r: -r["ratio"])
    txt = L.build_text(rows, "binance", {"ok": 4})
    body = txt.split("最被看空")[1]
    assert body.index("WORST") < body.index("MID") < body.index("MILD")


def test_only_three_per_side():
    rows = sorted([row(f"L{i}", 2 + i * 0.1) for i in range(8)]
                  + [row(f"S{i}", 0.9 - i * 0.1) for i in range(8)],
                  key=lambda r: -r["ratio"])
    txt = L.build_text(rows, "binance", {"ok": 16})
    assert "Top3" in txt
    assert txt.count("L") >= 3


def test_btc_is_shown_as_a_reference():
    """极值好看，但没有参照系读不出"这算不算极端"。"""
    rows = sorted([row("A", 3.0), row("BTC", 1.07), row("B", 0.4)],
                  key=lambda r: -r["ratio"])
    txt = L.build_text(rows, "binance", {"ok": 3})
    assert "参照：BTC 1.07" in txt


# ── Bybit 的 buyRatio 不是多空比 ─────────────────────────────
def test_bybit_ratio_is_computed_not_taken_raw():
    """Bybit 给的是两个占比（和为 1）。直接把 buyRatio 当多空比的话，
    0.53 和 1.13 是两个完全不同的数——榜会整个错位。"""
    import asyncio

    class _R:
        @staticmethod
        def json():
            return {"retCode": 0, "result": {"list": [
                {"buyRatio": "0.5316", "sellRatio": "0.4684"}]}}

    class _C:
        async def get(self, *a, **k):
            return _R()

    ratio, long_share = asyncio.run(L._lsr_bybit(_C(), "BTCUSDT"))
    assert ratio == pytest.approx(0.5316 / 0.4684, rel=1e-6)
    assert ratio > 1.13 and long_share == pytest.approx(0.5316)


def test_binance_ratio_is_taken_from_the_right_field():
    import asyncio

    class _R:
        @staticmethod
        def json():
            return [{"longAccount": "0.5173", "longShortRatio": "1.0717",
                     "shortAccount": "0.4827"}]

    class _C:
        async def get(self, *a, **k):
            return _R()

    ratio, long_share = asyncio.run(L._lsr_binance(_C(), "BTCUSDT"))
    assert ratio == pytest.approx(1.0717) and long_share == pytest.approx(0.5173)


# ── 扫描：过滤（打桩，不联网）────────────────────────────────
def _stub(monkeypatch, pairs, skip=frozenset(), ratios=None):
    async def _uni(client, market):
        return pairs

    async def _fetch(client, inst):
        return (ratios or {}).get(inst, (1.5, 0.6))

    monkeypatch.setitem(L._UNI, "binance", _uni)
    monkeypatch.setitem(L._FETCH, "binance", _fetch)

    async def _skip():
        return set(skip)
    import handlers.klines as K
    monkeypatch.setattr(K, "noncrypto_bases", _skip)


def test_tokenised_stocks_are_dropped(monkeypatch):
    """真机第一跑「最被看多」榜首是 SOXL（3 倍做多半导体 ETF）。
    复用了涨跌榜的 _keep 却漏了品类过滤——同一个坑第三次。"""
    import asyncio
    _stub(monkeypatch, [("BTCUSDT", "BTC", 9e8), ("SOXLUSDT", "SOXL", 5e8)],
          skip={"SOXL"})
    rows, stats = asyncio.run(L.scan("binance"))
    assert [r["sym"] for r in rows] == ["BTC"]
    assert stats["stock"] == 1 and stats["skip_ok"] is True


def test_thin_books_are_dropped(monkeypatch):
    """多空比是账户数统计，池子太小时一个人开仓就能把比值打飞。"""
    import asyncio
    _stub(monkeypatch, [("BTCUSDT", "BTC", 9e8),
                        ("DEADUSDT", "DEAD", 1e6)])
    rows, stats = asyncio.run(L.scan("binance"))
    assert [r["sym"] for r in rows] == ["BTC"] and stats["thin"] == 1
    assert L.MIN_TURNOVER > 1e7, "门槛要比涨跌榜高，账户数统计比价格更怕小池子"


def test_stablecoins_and_leverage_tokens_are_dropped(monkeypatch):
    import asyncio
    _stub(monkeypatch, [("BTCUSDT", "BTC", 9e8), ("USDCUSDT", "USDC", 9e8),
                        ("BTC3LUSDT", "BTC3L", 9e8)])
    rows, _s = asyncio.run(L.scan("binance"))
    assert [r["sym"] for r in rows] == ["BTC"]


def test_rows_come_back_sorted_by_ratio(monkeypatch):
    import asyncio
    _stub(monkeypatch,
          [("AUSDT", "A", 9e8), ("BUSDT", "B", 8e8), ("CUSDT", "C", 7e8)],
          ratios={"AUSDT": (0.5, 0.33), "BUSDT": (3.0, 0.75), "CUSDT": (1.2, 0.55)})
    rows, _s = asyncio.run(L.scan("binance"))
    assert [r["sym"] for r in rows] == ["B", "C", "A"]


def test_failed_fetches_are_counted(monkeypatch):
    """静默丢数据是这个项目最贵的 bug 类型，扫全市场的榜都要报这个数。"""
    import asyncio

    async def _fetch(client, inst):
        if inst == "BUSDT":
            raise RuntimeError("429")
        return (1.5, 0.6)
    _stub(monkeypatch, [("AUSDT", "A", 9e8), ("BUSDT", "B", 8e8)])
    monkeypatch.setitem(L._FETCH, "binance", _fetch)
    rows, stats = asyncio.run(L.scan("binance"))
    assert len(rows) == 1 and stats["failed"] == 1
    assert "1 个没取到" in L.build_text(rows, "binance", stats)


# ── 口径卡 ──────────────────────────────────────────────────
def test_detail_explains_the_reading_and_why_venues_are_not_merged():
    txt = L.build_detail([row("BTC", 1.07)], "binance",
                         {"pool": 90, "asked": 80, "ok": 80, "failed": 0,
                          "thin": 430, "stock": 173, "skip_ok": True})
    for must in ("账户数", "反向参考", "为什么不合并三家", "2.69", "为什么没有 OKX",
                 "173", "不构成投资建议"):
        assert must in txt, f"口径卡缺了：{must}"


def test_card_is_short_enough_for_the_buttons_to_be_reachable():
    """Telegram 按钮永远在消息末尾，卡片一长按钮就被挤出屏幕。"""
    rows = sorted([row(f"L{i}", 3 - i * 0.2) for i in range(10)]
                  + [row(f"S{i}", 0.9 - i * 0.05) for i in range(10)],
                  key=lambda r: -r["ratio"])
    txt = L.build_text(rows, "binance", {"ok": 20})
    assert len(txt.splitlines()) <= 20, "卡片太长，按钮会被挤出屏幕"
    assert "👇" in txt


# ── 入口 ────────────────────────────────────────────────────
def test_command_is_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("lsr"' in src and 'BotCommand("lsr"' in src


def test_button_is_two_taps_from_home():
    """/menu → 📊 行情 → ⚖️ 多空比极值榜。埋三层等于没有入口。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    seg = src.split('elif d == "cat_market":')[1].split("elif d ==")[0]
    assert "ls:v:binance" in seg


def test_buttons_round_trip_through_the_dispatcher():
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert 'd.startswith("ls:")' in src
    for r in L.kb("binance").inline_keyboard:
        for b in r:
            d = b.callback_data
            if not d.startswith("ls:"):
                continue
            bits = d.split(":")
            assert len(bits) == 3 and bits[1] in ("v", "r", "i")
            assert bits[2] in L.V_LABEL


def test_both_venues_have_a_button():
    labels = " ".join(b.text for r in L.kb("binance").inline_keyboard for b in r)
    assert "币安" in labels and "Bybit" in labels


def test_command_is_categorised_in_the_panel():
    from handlers import cmdpanel
    assert cmdpanel.MODULE_CN.get("handlers.lsratio")


def test_heavy_scan_is_gated():
    import inspect
    assert "busy.guard" in inspect.getsource(L.lsr_cmd)


def test_single_coin_ratio_command_is_untouched():
    """/ratio BTC 走 OKX 的单币查询，不该被这次改动碰到。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("ratio", okx.long_short)' in src
