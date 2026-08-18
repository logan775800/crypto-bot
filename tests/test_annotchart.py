"""标注图表的数值层测试（不画图，只验算要标在图上的位）。
画错线比不画更糟——用户会照着一条错的止损线下单。"""
import pytest

from handlers.annotchart import (
    _ma_series, ma_align, levels, caption, _ascii_structure, _STRUCT_ASCII,
    MA_PERIODS,
)


def _rows(closes, highs=None, lows=None, vols=None):
    """伪造 Bybit kline 行：[start, open, high, low, close, volume, turnover]，旧→新。"""
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    vols = vols or [100.0] * n
    return [[str(i * 60000), str(closes[i]), str(highs[i]), str(lows[i]),
             str(closes[i]), str(vols[i]), "0"] for i in range(n)]


class TestMaSeries:
    """均线是 MA3/13/23（简单均线），和破位扫描同一套——
    图上画的和信号用的必须是同一个东西，否则看到的和报的对不上。"""

    def test_length_always_matches_input(self):
        # 长度对不上，mplfinance 的 addplot 会直接抛 —— 图整个发不出来
        for n in MA_PERIODS:
            assert len(_ma_series([1.0] * 300, n)) == 300

    def test_leading_values_are_none_until_seeded(self):
        s = _ma_series([1.0] * 100, 23)
        assert s[:22] == [None] * 22
        assert s[22] is not None

    def test_too_short_gives_all_none(self):
        assert _ma_series([1.0] * 10, 23) == [None] * 10

    def test_constant_series_equals_the_constant(self):
        assert _ma_series([5.0] * 100, 13)[-1] == pytest.approx(5.0)

    def test_is_a_simple_average(self):
        closes = [float(i) for i in range(1, 101)]
        assert _ma_series(closes, 3)[-1] == pytest.approx((98 + 99 + 100) / 3)

    def test_rolling_sum_matches_bruteforce(self):
        """滚动求和是增量算的，累积误差会悄悄漂——和暴力平均对一遍。"""
        closes = [float((i * 7919) % 101) + 1 for i in range(300)]
        ser = _ma_series(closes, 13)
        for i in (13, 100, 299):
            assert ser[i] == pytest.approx(sum(closes[i - 12:i + 1]) / 13)


class TestMaAlign:
    def test_rising_series_is_bullish(self):
        assert ma_align([float(i) for i in range(1, 60)]) == 1

    def test_falling_series_is_bearish(self):
        assert ma_align([float(i) for i in range(60, 1, -1)]) == -1

    def test_tight_chop_is_neither(self):
        """三根粘在 0.05% 以内 = 还没选边，这时候的"突破"最容易被立刻打回来。"""
        closes = [100.0 + (0.01 if i % 2 else -0.01) for i in range(60)]
        assert ma_align(closes) == 0

    def test_alignment_needs_separation_not_just_order(self):
        """光看大小关系不够——等幅震荡也能排出单调顺序。

        注意这条只挡得住"贴在一起"的情况：大幅等幅震荡算出来的均线间距
        （实测 0.28%）和真趋势（ETH 那例 0.32%）是一个量级，靠间距分不开，
        真正把它挡在门外的是破位扫描那边的**箱体**条件。
        """
        flat = [100.0] * 40 + [100.02] * 20
        assert ma_align(flat) == 0

    def test_too_short_is_neither(self):
        assert ma_align([1.0, 2.0]) == 0


class TestLevels:
    def test_basic_keys_present(self):
        lv = levels(_rows([float(i) for i in range(1, 301)]))
        for k in ("last", "atr", "structure", "prior_high", "prior_low",
                  "vwap", "ma", "ma_align", "rsi"):
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

    def test_unavailable_mas_are_omitted_not_shown_as_none(self):
        # 根数不够时长周期均线算不出来，图上也不画，说明里就不该列
        cap = caption("BTC", "1h", levels(_rows([float(i) for i in range(1, 101)])))
        assert "MA3" in cap and "MA13" in cap


class TestAsciiStructure:
    """结构标签：**装了中文字体就直接用中文**（镜像里有 fonts-noto-cjk），
    没装才退回 ASCII 对照表——中文硬画出来是一排豆腐块。
    下面这几条统一把字体探测钉死，免得跟着跑测试的机器有没有中文字体飘。"""

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

    def test_unknown_label_degrades_to_empty_without_font(self, monkeypatch):
        """没字体时遇到没映射的标签宁可空着，也别把中文画成豆腐块。"""
        from handlers import annotchart as A
        monkeypatch.setitem(A._CJK, "checked", True)
        monkeypatch.setitem(A._CJK, "name", None)
        assert _ascii_structure({"structure": "某个新标签"}) == ""

    def test_lookup_works_without_font(self, monkeypatch):
        from handlers import annotchart as A
        monkeypatch.setitem(A._CJK, "checked", True)
        monkeypatch.setitem(A._CJK, "name", None)
        assert _ascii_structure({"structure": "上升结构(HH+HL)"}) == "Uptrend HH+HL"

    def test_chinese_is_used_when_the_font_exists(self, monkeypatch):
        from handlers import annotchart as A
        monkeypatch.setitem(A._CJK, "checked", True)
        monkeypatch.setitem(A._CJK, "name", "Noto Sans CJK SC")
        assert _ascii_structure({"structure": "上升结构(HH+HL)"}) == "上升结构(HH+HL)"


class TestMaConsistency:
    """三处均线必须同一套口径。

    以前：日线图 MA7/25/99、标注图 EMA20/50/200、破位扫描 MA3/13/23——
    同一个币在三个地方给出三种"排列"，用户没法知道该信哪个。
    """

    def test_detail_chart_uses_the_same_periods(self):
        import inspect
        from handlers import detail
        src = inspect.getsource(detail)
        assert "MA_PERIODS" in src
        assert "mav=(7, 25, 99)" not in src

    def test_marketdata_analysis_uses_the_same_periods(self):
        import inspect
        from handlers import marketdata
        src = inspect.getsource(marketdata.klines_analysis)
        assert "MA_PERIODS" in src
        assert "ema(c, 20)" not in src

    def test_scan_trend_uses_the_same_periods(self):
        import inspect
        from handlers import scan
        src = inspect.getsource(scan._tf_snapshot)
        assert "MA_PERIODS" in src
        assert "md.ema(c, 20)" not in src

    def test_breakout_shares_the_same_align(self):
        import inspect
        from handlers import breakout
        assert "ma_align" in inspect.getsource(breakout.detect)

    # ── 下面这批是 v1.24.2 补的护栏 ──────────────────────────────
    # v1.24.0 声称"全局统一"，实际漏了 /analyze、/ai、/chartanalyze、驾驶舱、
    # 指标告警、BTC/ETH 联动六处，而当时的护栏只盖了上面四个模块，所以漏网无人发现。
    # 判据一律用「算出来的值」而不是 grep 源码——源码里写没写 MA_PERIODS 不代表算对了。

    def test_analyze_returns_the_shared_periods(self):
        from indicators import analyze
        from handlers.annotchart import MA_PERIODS, _ma_series
        closes = [100 + i * 0.7 for i in range(60)]      # 稳定上行
        r = analyze(closes)
        assert r["ma_periods"] == MA_PERIODS
        for key, n in zip(("ma_fast", "ma_mid", "ma_slow"), MA_PERIODS):
            assert r[key] == pytest.approx(_ma_series(closes, n)[-1])
        assert "多头排列" in r["ma_signal"]
        assert f"MA{MA_PERIODS[-1]}" in r["price_signal"]
        assert "ma7" not in r and "ma30" not in r        # 旧键必须彻底消失

    def test_analyze_calls_a_tangle_a_tangle(self):
        """缠绕不能被算成看空——analysis.py 的计分以前用 else 兜底，凭空多一票空。"""
        from indicators import analyze
        closes = [100.0] * 60
        assert "缠绕" in analyze(closes)["ma_signal"]

    def test_cockpit_trend_uses_the_lifeline(self):
        import inspect
        from handlers import cockpit
        from handlers.annotchart import MA_PERIODS
        src = inspect.getsource(cockpit._analyze_one)
        assert "MA_PERIODS" in src and "ema(c, 20)" not in src
        st, danger = cockpit.trend_state(
            "long", {"last": 90.0, "ma_ref": 95.0, "ma_ref_period": MA_PERIODS[-1],
                     "structure": "震荡/不明确"})
        assert f"MA{MA_PERIODS[-1]}" in st and danger is True

    @staticmethod
    def _code_only(fn):
        """剥掉注释再判——注释里写「以前是 MA7/MA30」是解释，不是残留口径。
        （这条护栏第一次就是被自己的注释绊倒的。）"""
        import inspect
        return "\n".join(ln.split("#")[0] for ln in inspect.getsource(fn).splitlines())

    def test_chart_and_market_context_dropped_the_old_periods(self):
        from handlers import chart, marketdata
        csrc = self._code_only(chart.analyze_chart)
        assert "MA_PERIODS" in csrc
        assert "sma(window, 7)" not in csrc and "MA30" not in csrc
        msrc = self._code_only(marketdata.market_context)
        assert "MA_PERIODS" in msrc and "EMA20" not in msrc

    def test_indicator_alert_cross_uses_the_same_periods(self):
        import inspect
        from handlers import indicator_alert
        src = inspect.getsource(indicator_alert)
        assert "MA_PERIODS" in src
        assert "sma(prices, 7)" not in src and "sma(prices, 30)" not in src
        # 换口径后旧状态键必须弃用，否则换版当天会凭空推一次金叉
        assert "ma_state_ma3" in src

    def test_help_text_does_not_promise_the_old_periods(self):
        """给用户看的说明也算口径的一部分：菜单里写着「图上标 🟡EMA20 🔵EMA50
        🟣EMA200」，而图上画的其实是 MA3/13/23——颜色对得上、名字全错。
        这类过期文案和错的代码一样会误导人，而且更难发现（没人会去测文案）。

        regime.py 的 4h EMA20/50/200 是**故意保留**的：它判的是几十天尺度的
        市场环境，换成 MA23（4h × 23 ≈ 4 天）意思就变了。它自己写明了口径，
        不算对不上。backtest 的策略定义同理。
        """
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        for name in ("bot.py", "handlers/menu.py"):
            src = (root / name).read_text(encoding="utf-8")
            for bad in ("EMA20", "EMA50", "EMA200"):
                assert bad not in src, f"{name} 里还写着 {bad}，图上画的是 MA3/13/23"

    def test_ai_tool_descriptions_match_the_data_they_return(self):
        """给 AI 的工具描述必须和实际数值口径一致——说 EMA20 却给 MA3 的值，
        模型不会报错，只会把短均线当长均线解读。这是最难发现的一类不一致。"""
        from handlers import chat
        blob = repr(chat.TOOLS) + chat.SYSTEM
        assert "EMA" not in blob.upper().replace("MA3/13/23", "")
        assert "MA3/13/23" in blob and "MA23" in blob


class TestCjkFont:
    """图上的中文：装了字体就用真名，没装才退回 ASCII。
    没字体时硬画中文会渲染成一排豆腐块，比显示英文/地址还糟。"""

    def test_probe_is_cached(self):
        from handlers.annotchart import cjk_font, _CJK
        cjk_font()
        assert _CJK["checked"] is True

    def test_apply_cjk_sets_font_when_available(self, monkeypatch):
        from handlers import annotchart as A
        monkeypatch.setitem(A._CJK, "checked", True)
        monkeypatch.setitem(A._CJK, "name", "Noto Sans CJK SC")
        out = A.apply_cjk({"base_mpf_style": "charles"})
        assert out["rc"]["font.family"] == "Noto Sans CJK SC"
        assert out["rc"]["axes.unicode_minus"] is False   # 否则负号变方块

    def test_apply_cjk_is_a_noop_without_font(self, monkeypatch):
        from handlers import annotchart as A
        monkeypatch.setitem(A._CJK, "checked", True)
        monkeypatch.setitem(A._CJK, "name", None)
        assert A.apply_cjk({"x": 1}) == {"x": 1}

    def test_onchain_title_falls_back_without_font(self, monkeypatch):
        from handlers import annotchart as A, onchain as OC
        monkeypatch.setitem(A._CJK, "checked", True)
        monkeypatch.setitem(A._CJK, "name", None)
        t = {"symbol": "牛来", "address": "0xBEEA1D618e533a387D941F58a7d4c9b7bD377777",
             "chain": "bsc"}
        title = OC._ascii_title(t, "1h")
        assert title.isascii() and "0xBEEA1D61" in title

    def test_onchain_title_uses_chinese_with_font(self, monkeypatch):
        from handlers import annotchart as A, onchain as OC
        monkeypatch.setitem(A._CJK, "checked", True)
        monkeypatch.setitem(A._CJK, "name", "Noto Sans CJK SC")
        t = {"symbol": "牛来", "address": "0xa", "chain": "bsc"}
        assert "牛来" in OC._ascii_title(t, "1h")

    def test_dockerfile_installs_the_font(self):
        import io as _io
        import os
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "Dockerfile")
        assert "fonts-noto-cjk" in _io.open(p, encoding="utf-8").read()
