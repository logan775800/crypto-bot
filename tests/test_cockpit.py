"""持仓驾驶舱的判断逻辑。趋势状态和建议动作误判会给用户错误的操作提示。"""
import pytest

from handlers.cockpit import trend_state, next_levels, suggest, _pos_block, account_flags


def _a(**kw):
    base = {"last": 100.0, "ema20": 95.0, "ema50": 90.0,
            "structure": "上升结构(HH+HL)",
            "swing_high": 110.0, "swing_low": 92.0,
            "prior_high": 115.0, "prior_low": 88.0}
    base.update(kw)
    return base


class TestTrendState:
    def test_long_with_trend(self):
        st, danger = trend_state("long", _a(last=100, ema20=95, structure="上升结构(HH+HL)"))
        assert "顺势" in st and danger is False

    def test_long_counter_trend_is_dangerous(self):
        st, danger = trend_state("long", _a(last=90, ema20=95, structure="下降结构(LH+LL)"))
        assert "逆势" in st and danger is True

    def test_long_below_ema_is_flagged(self):
        st, danger = trend_state("long", _a(last=90, ema20=95, structure="震荡/不明确"))
        assert danger is True and "跌破" in st

    def test_short_with_trend(self):
        st, danger = trend_state("short", _a(last=90, ema20=95, structure="下降结构(LH+LL)"))
        assert "顺势" in st and danger is False

    def test_short_counter_trend_is_dangerous(self):
        st, danger = trend_state("short", _a(last=100, ema20=95, structure="上升结构(HH+HL)"))
        assert "逆势" in st and danger is True

    def test_short_above_ema_is_flagged(self):
        st, danger = trend_state("short", _a(last=100, ema20=95, structure="震荡/不明确"))
        assert danger is True and "站上" in st

    def test_missing_data_is_not_dangerous(self):
        st, danger = trend_state("long", {"last": None, "ema20": None})
        assert st == "数据不足" and danger is False


class TestNextLevels:
    def test_support_is_nearest_below(self):
        sup, res = next_levels("long", _a(last=100, swing_low=92, prior_low=88))
        assert sup == 92        # 最近的下方（92 比 88 更近现价）

    def test_resistance_is_nearest_above(self):
        sup, res = next_levels("long", _a(last=100, swing_high=110, prior_high=115))
        assert res == 110

    def test_levels_on_wrong_side_are_ignored(self):
        # swing_low 若高于现价就不是支撑
        sup, res = next_levels("long", _a(last=100, swing_low=105, prior_low=88,
                                          swing_high=110, prior_high=115))
        assert sup == 88

    def test_no_data(self):
        assert next_levels("long", {"last": None}) == (None, None)

    def test_ref_overrides_kline_last(self):
        # 传持仓 markPrice 当参照时，用它而不是 K线 last 来分支撑/阻力
        a = _a(last=200, swing_low=92, prior_low=88, swing_high=110, prior_high=115)
        sup, res = next_levels("long", a, ref=100)
        assert sup == 92 and res == 110      # 相对 ref=100，不是相对 last=200

    def test_falls_back_to_last_without_ref(self):
        a = _a(last=100, swing_low=92, swing_high=110)
        assert next_levels("long", a)[0] == 92


def _pos(side="Buy", sl="0", upnl="10", value="1000", liq="50", mark="100"):
    return {"symbol": "BTCUSDT", "side": side, "stopLoss": sl,
            "unrealisedPnl": upnl, "positionValue": value,
            "liqPrice": liq, "markPrice": mark, "avgPrice": "95", "leverage": "10"}


class TestSuggest:
    def test_no_stop_loss_is_first_priority(self):
        acts = suggest("long", _a(), _pos(sl="0"), dist_liq=50, has_sl=False,
                       state_danger=False)
        assert acts[0] == "先设止损"

    def test_near_liquidation_warns(self):
        acts = suggest("long", _a(), _pos(), dist_liq=10, has_sl=True,
                       state_danger=False)
        assert any("距爆仓近" in x for x in acts)

    def test_counter_trend_and_losing_says_dont_average_down(self):
        acts = suggest("long", _a(), _pos(upnl="-50"), dist_liq=50, has_sl=True,
                       state_danger=True)
        assert any("别补仓" in x for x in acts)

    def test_winning_with_trend_suggests_trailing_stop(self):
        acts = suggest("long", _a(), _pos(upnl="100"), dist_liq=50, has_sl=True,
                       state_danger=False)
        assert any("止损上移保本" in x for x in acts)

    def test_default_is_hold(self):
        acts = suggest("long", _a(), _pos(upnl="0"), dist_liq=50, has_sl=True,
                       state_danger=False)
        assert acts == ["持有观察"]


class TestPosBlock:
    def test_marks_naked_stop(self):
        block = _pos_block({"symbol": "BTCUSDT", "pos": _pos(sl="0"), **_a()})
        assert "未设 ❗" in block

    def test_shows_distance_to_liq(self):
        # mark=100 liq=50 → 距爆仓 50%
        block = _pos_block({"symbol": "BTCUSDT", "pos": _pos(mark="100", liq="50"),
                            **_a()})
        assert "距 50.0%" in block

    def test_funding_direction_long_pays_on_positive(self):
        a = {"symbol": "BTCUSDT", "pos": _pos(side="Buy"), "funding": 0.05, **_a()}
        assert "你在付费" in _pos_block(a)

    def test_funding_direction_long_earns_on_negative(self):
        a = {"symbol": "BTCUSDT", "pos": _pos(side="Buy"), "funding": -0.05, **_a()}
        assert "你在收费" in _pos_block(a)

    def test_small_price_coin_is_not_shown_as_zero(self):
        # PEPE 之类：avgPrice 0.000012 用固定2位小数会变成 0.00
        pos = _pos(mark="0.0000118")
        pos["avgPrice"] = "0.000012"
        block = _pos_block({"symbol": "PEPEUSDT", "pos": pos,
                            "last": 0.0000118, "ema20": 0.0000115,
                            "structure": "震荡/不明确"})
        assert "0.00 →" not in block          # 均价不能塌成 0.00
        assert "0.000012" in block


class TestAccountFlags:
    def _mk(self, sym, side="Buy", sl="0", value="1000"):
        return {"symbol": sym, "pos": {"symbol": sym, "side": side, "stopLoss": sl,
                                       "positionValue": value}}

    def test_flags_naked_stops(self):
        flags = account_flags([self._mk("BTCUSDT", sl="0")], 10000)
        assert any("没设止损" in f for f in flags)

    def test_flags_multiple_alt_longs(self):
        analyses = [self._mk("PEPEUSDT", "Buy"), self._mk("WIFUSDT", "Buy")]
        flags = account_flags(analyses, 10000)
        assert any("山寨多单" in f and "一起走" in f for f in flags)

    def test_majors_do_not_trigger_alt_flag(self):
        analyses = [self._mk("BTCUSDT", "Buy", sl="100"),
                    self._mk("ETHUSDT", "Buy", sl="100")]
        flags = account_flags(analyses, 10000)
        assert not any("山寨多单" in f for f in flags)

    def test_no_flags_for_a_clean_book(self):
        flags = account_flags([self._mk("BTCUSDT", "Buy", sl="60000")], 10000)
        assert flags == []
