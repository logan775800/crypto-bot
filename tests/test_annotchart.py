"""标注图表的数值层测试（不画图，只验算要标在图上的位）。
画错线比不画更糟——用户会照着一条错的止损线下单。"""
import pytest

from handlers.annotchart import (
    _ema_series, levels, caption, _ascii_structure, _STRUCT_ASCII,
)


def _rows(closes, highs=None, lows=None, vols=None):
    """伪造 Bybit kline 行：[start, open, high, low, close, volume, turnover]，旧→新。"""
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    vols = vols or [100.0] * n
    return [[str(i * 60000), str(closes[i]), str(highs[i]), str(lows[i]),
             str(closes[i]), str(vols[i]), "0"] for i in range(n)]


class TestEmaSeries:
    def test_length_always_matches_input(self):
        # 长度对不上，mplfinance 的 addplot 会直接抛 —— 图整个发不出来
        for n in (5, 20, 50, 200):
            assert len(_ema_series([1.0] * 300, n)) == 300

    def test_leading_values_are_none_until_seeded(self):
        s = _ema_series([1.0] * 100, 20)
        assert s[:19] == [None] * 19
        assert s[19] is not None

    def test_too_short_gives_all_none(self):
        # EMA200 在只有 100 根时必须整条为 None，而不是崩掉或给假值
        assert _ema_series([1.0] * 100, 200) == [None] * 100

    def test_constant_series_ema_equals_the_constant(self):
        assert _ema_series([5.0] * 100, 20)[-1] == pytest.approx(5.0)

    def test_matches_marketdata_ema(self):
        # 图上的线必须和 AI 读到的数字同源，否则两边说的不是一回事
        from handlers.marketdata import ema
        closes = [float(i) for i in range(1, 301)]
        assert _ema_series(closes, 20)[-1] == pytest.approx(ema(closes, 20))


class TestLevels:
    def test_basic_keys_present(self):
        lv = levels(_rows([float(i) for i in range(1, 301)]))
        for k in ("last", "atr", "structure", "prior_high", "prior_low",
                  "vwap", "ema20", "ema50", "ema200", "rsi"):
            assert k in lv

    def test_last_is_final_close(self):
        assert levels(_rows([1.0] * 99 + [42.0]))["last"] == 42.0

    def test_prior_high_low_use_last_50_bars(self):
        closes = [10.0] * 100 + [50.0] * 30   # 老的高点在 50 根之外
        rows = _rows(closes, highs=[999.0] + [c * 1.01 for c in closes[1:]])
        lv = levels(rows)
        assert lv["prior_high"] != 999.0      # 早于近50根的极值不该被当成前高

    def test_stop_bands_straddle_price(self):
        lv = levels(_rows([float(i) for i in range(1, 301)]))
        assert lv["stop_long"] < lv["last"] < lv["stop_short"]

    def test_stop_distance_is_1_5_atr(self):
        lv = levels(_rows([float(i) for i in range(1, 301)]))
        assert lv["last"] - lv["stop_long"] == pytest.approx(1.5 * lv["atr"])
        assert lv["stop_short"] - lv["last"] == pytest.approx(1.5 * lv["atr"])

    def test_no_atr_means_no_stop_bands(self):
        # 数据太短算不出 ATR 时不能给出编造的止损位
        lv = levels(_rows([1.0] * 10))
        assert lv["atr"] is None
        assert "stop_long" not in lv

    def test_vwap_of_flat_series_is_that_price(self):
        lv = levels(_rows([10.0] * 100, highs=[10.0] * 100, lows=[10.0] * 100))
        assert lv["vwap"] == pytest.approx(10.0)

    def test_vwap_uses_only_the_plotted_window(self):
        # 前 200 根在 10 附近、后 20 根在 100 附近。只画 20 根时 VWAP 必须≈100，
        # 若按全量算会得到 ~19 —— 那条线落在画布外，图上根本看不到
        closes = [10.0] * 200 + [100.0] * 20
        lv = levels(_rows(closes), plot_bars=20)
        assert lv["vwap"] == pytest.approx(100.0, rel=0.02)

    def test_view_range_reflects_plotted_window_only(self):
        closes = [10.0] * 200 + [100.0] * 20
        lv = levels(_rows(closes), plot_bars=20)
        assert lv["view_low"] > 50      # 老的 10 块区间不在可见窗口里

    def test_every_annotated_level_fits_inside_the_drawn_axis(self):
        # caption 声称「图上的线」，那它们就必须都落在 y 轴范围内。
        # 这里复刻 build_chart 的 ylim 算法，锁住这个契约。
        closes = [60000 + i * 20 for i in range(400)]
        lv = levels(_rows(closes))
        drawn = [lv[k] for k in ("swing_high", "swing_low", "prior_high", "prior_low",
                                 "vwap", "stop_long", "stop_short") if lv.get(k) is not None]
        top = max([lv["view_high"]] + drawn)
        bot = min([lv["view_low"]] + drawn)
        pad = (top - bot) * 0.04
        for v in drawn:
            assert bot - pad <= v <= top + pad


class TestCaption:
    def test_uptrend_reads_as_bullish_stack(self):
        lv = levels(_rows([float(i) for i in range(1, 301)]))
        cap = caption("BTC", "1h", lv)
        assert "多头排列" in cap
        assert "BTC" in cap and "1h" in cap

    def test_downtrend_reads_as_bearish_stack(self):
        lv = levels(_rows([float(i) for i in range(300, 0, -1)]))
        assert "空头排列" in caption("BTC", "1h", lv)

    def test_stop_section_shown_when_atr_available(self):
        lv = levels(_rows([float(i) for i in range(1, 301)]))
        cap = caption("BTC", "1h", lv)
        assert "ATR 止损距离" in cap and "仓位" in cap

    def test_short_data_caption_still_renders(self):
        # 没 ATR(<15根)/没 EMA200 时不能崩，图还是要能发出去
        cap = caption("BTC", "1h", levels(_rows([1.0] * 10)))
        assert "BTC" in cap and "ATR 止损距离" not in cap

    def test_has_disclaimer(self):
        lv = levels(_rows([float(i) for i in range(1, 301)]))
        assert "不构成投资建议" in caption("BTC", "1h", lv)

    def test_symbol_is_normalized(self):
        lv = levels(_rows([float(i) for i in range(1, 301)]))
        assert "BTCUSDT" not in caption("btcusdt", "1h", lv)

    def test_never_prints_the_literal_string_none(self):
        # md.f(None) 返回 "None"，直接拼进去就会印给用户看
        for n in (10, 30, 100, 300):
            cap = caption("BTC", "1h", levels(_rows([float(i) for i in range(1, n + 1)])))
            assert "None" not in cap, f"{n} 根时 caption 里漏出了 None：{cap}"

    def test_unavailable_emas_are_omitted_not_shown_as_none(self):
        # 只有 100 根 → EMA200 算不出来，图上也不画，说明里就不该列
        cap = caption("BTC", "1h", levels(_rows([float(i) for i in range(1, 101)])))
        assert "EMA20" in cap and "EMA50" in cap
        assert "EMA200" not in cap


class TestAsciiStructure:
    """图标题必须纯 ASCII：镜像 python:3.11-slim 无 CJK 字体，中文会渲染成豆腐块。"""

    def test_every_structure_label_has_an_ascii_mapping(self):
        # marketdata.structure 新增标签而这里没跟上 → 标题会静默变空
        from handlers.marketdata import structure
        produced = set()
        for closes in ([float(i) for i in range(1, 101)],
                       [float(i) for i in range(100, 0, -1)],
                       [50.0 + (i % 7) for i in range(100)]):
            h = [c * 1.01 for c in closes]
            l = [c * 0.99 for c in closes]
            produced.add(structure(h, l)[0])
        assert produced <= set(_STRUCT_ASCII), f"未映射的结构标签: {produced - set(_STRUCT_ASCII)}"

    def test_mapped_titles_are_pure_ascii(self):
        for v in _STRUCT_ASCII.values():
            assert v.isascii(), v

    def test_unknown_label_degrades_to_empty_not_chinese(self):
        assert _ascii_structure({"structure": "某个新标签"}) == ""

    def test_lookup_works(self):
        assert _ascii_structure({"structure": "上升结构(HH+HL)"}) == "Uptrend HH+HL"
