"""风险反推仓位。

这里算错 = 用户直接按错的数字下单亏钱，所以核心恒等式必须钉死：
    名义 × 止损距离% == 计划风险 USDT
以及最容易搞反的一点：**杠杆不改变名义仓位**，只改变保证金占用。
"""
import pytest

from handlers.sizing import plan_size, exposure, parse_args, build_text, TIERS


class TestPlanSizeIdentity:
    def test_the_core_identity_holds(self):
        # 名义 × 止损距离% 必须正好等于计划风险金额，这是整个功能的定义
        s = plan_size(equity=10000, entry=100, stop=98, risk_pct=0.5)
        assert s["notional"] * s["dist_pct"] / 100 == pytest.approx(s["risk_usdt"])

    def test_identity_holds_across_wide_inputs(self):
        for eq in (500, 10_000, 250_000):
            for entry, stop in ((100, 98), (0.081, 0.0828), (65000, 63000)):
                for risk in (0.25, 0.5, 1.0, 2.0):
                    s = plan_size(eq, entry, stop, risk)
                    assert s["notional"] * s["dist_pct"] / 100 == pytest.approx(s["risk_usdt"])
                    assert s["risk_usdt"] == pytest.approx(eq * risk / 100)

    def test_worked_example(self):
        # 权益 10000，风险 0.5% = 50U；入场 100 止损 98 → 距离 2%
        # 名义 = 50 / 2% = 2500U
        s = plan_size(10000, 100, 98, 0.5)
        assert s["risk_usdt"] == pytest.approx(50)
        assert s["dist_pct"] == pytest.approx(2.0)
        assert s["notional"] == pytest.approx(2500)
        assert s["qty"] == pytest.approx(25)

    def test_tighter_stop_allows_bigger_position(self):
        # 这是这个功能最反直觉也最有价值的一点
        tight = plan_size(10000, 100, 99, 0.5)
        wide = plan_size(10000, 100, 95, 0.5)
        assert tight["notional"] > wide["notional"]
        # 但两者的最大亏损相同——这才是「风险固定」的意思
        assert tight["risk_usdt"] == pytest.approx(wide["risk_usdt"])


class TestLeverageDoesNotChangeRisk:
    def test_margins_scale_inversely_with_leverage(self):
        s = plan_size(10000, 100, 98, 0.5)
        assert s["margins"][10] == pytest.approx(s["notional"] / 10)
        assert s["margins"][20] == pytest.approx(s["notional"] / 20)

    def test_notional_and_risk_are_independent_of_leverage(self):
        # 名义和风险里根本没有杠杆这个变量——杠杆只决定保证金占用
        s = plan_size(10000, 100, 98, 0.5)
        for lev, m in s["margins"].items():
            assert m * lev == pytest.approx(s["notional"])
        assert s["risk_usdt"] == pytest.approx(50)


class TestDirection:
    def test_stop_below_entry_is_long(self):
        assert plan_size(10000, 100, 98, 0.5)["side"] == "long"

    def test_stop_above_entry_is_short(self):
        assert plan_size(10000, 100, 102, 0.5)["side"] == "short"

    def test_short_math_is_symmetric(self):
        lo = plan_size(10000, 100, 98, 0.5)
        sh = plan_size(10000, 100, 102, 0.5)
        assert lo["notional"] == pytest.approx(sh["notional"])


class TestGuards:
    @pytest.mark.parametrize("eq,entry,stop,risk", [
        (10000, 100, 100, 0.5),     # 入场=止损 → 距离0，会除零
        (0, 100, 98, 0.5),          # 没权益
        (10000, 0, 98, 0.5),
        (10000, 100, 0, 0.5),
        (10000, 100, 98, 0),        # 风险0
        (-100, 100, 98, 0.5),
    ])
    def test_invalid_inputs_return_none_not_crash(self, eq, entry, stop, risk):
        assert plan_size(eq, entry, stop, risk) is None

    def test_drawdown_equals_risk_pct(self):
        # 触及止损后的账户回撤就等于计划风险%，这个等式是功能的自洽性检查
        s = plan_size(10000, 100, 98, 0.75)
        assert s["dd_pct"] == pytest.approx(0.75)
        assert s["risk_usdt"] / s["equity"] * 100 == pytest.approx(s["dd_pct"])

    def test_tiny_prices_keep_precision(self):
        s = plan_size(10000, 0.000081, 0.0000828, 0.5)
        assert s["notional"] * s["dist_pct"] / 100 == pytest.approx(s["risk_usdt"])


def _pos(sym="BTCUSDT", side="Buy", value="1000"):
    return {"symbol": sym, "side": side, "positionValue": value}


class TestExposure:
    def test_totals(self):
        e = exposure([_pos(value="1000"), _pos(value="500")])
        assert e["total"] == 1500

    def test_same_side_filter(self):
        ps = [_pos("AUSDT", "Buy", "1000"), _pos("BUSDT", "Sell", "500")]
        assert exposure(ps, "long")["same_side"] == 1000
        assert exposure(ps, "short")["same_side"] == 500

    def test_alt_share_excludes_majors(self):
        ps = [_pos("BTCUSDT", "Buy", "1000"), _pos("PEPEUSDT", "Buy", "500")]
        e = exposure(ps, "long")
        assert e["same_side"] == 1500
        assert e["same_side_alt"] == 500

    def test_empty_and_none(self):
        assert exposure([])["total"] == 0
        assert exposure(None)["total"] == 0

    def test_garbage_value_is_skipped_not_fatal(self):
        assert exposure([{"symbol": "X", "side": "Buy", "positionValue": "n/a"}])["total"] == 0


class TestParseArgs:
    def test_entry_stop_only_defaults_to_half_percent(self):
        assert parse_args(["0.081", "0.0828"]) == (None, 0.081, 0.0828, 0.5)

    def test_with_risk(self):
        assert parse_args(["0.081", "0.0828", "0.5%"]) == (None, 0.081, 0.0828, 0.5)

    def test_risk_without_percent_sign(self):
        assert parse_args(["100", "98", "1"]) == (None, 100.0, 98.0, 1.0)

    def test_with_symbol(self):
        assert parse_args(["BANK", "0.081", "0.0828", "0.5%"]) == ("BANK", 0.081, 0.0828, 0.5)

    def test_symbol_usdt_suffix_stripped(self):
        assert parse_args(["BANKUSDT", "1", "2"])[0] == "BANK"

    def test_commas_in_numbers(self):
        assert parse_args(["65,000", "63,000"])[1] == 65000.0

    @pytest.mark.parametrize("args", [
        [], ["0.081"], ["BANK"], ["BANK", "0.081"],
        ["abc", "def"], ["100", "98", "abc"], ["100", "98", "0"], ["100", "98", "150%"],
    ])
    def test_bad_input_returns_none(self, args):
        assert parse_args(args) is None


class TestBuildText:
    def test_none_gives_friendly_error(self):
        assert "算不了" in build_text(None)

    def test_headline_numbers_present(self):
        s = plan_size(10000, 100, 98, 0.5)
        t = build_text(s)
        assert "止损距离" in t and "2.00%" in t
        assert "建议最大名义" in t
        assert "各杠杆所需保证金" in t

    def test_flags_margin_exceeding_equity(self):
        # 极窄止损 → 名义巨大 → 低杠杆下保证金超过权益，必须说清做不了
        s = plan_size(10000, 100, 99.99, 0.5)
        assert "超过总权益" in build_text(s)

    def test_exposure_section_when_positions_given(self):
        s = plan_size(10000, 100, 98, 0.5)
        exp = exposure([_pos("PEPEUSDT", "Buy", "5000")], "long")
        t = build_text(s, exp)
        assert "同向暴露检查" in t
        assert "山寨" in t

    def test_warns_when_total_exposure_is_extreme(self):
        s = plan_size(10000, 100, 98, 0.5)
        exp = exposure([_pos("AUSDT", "Buy", "40000")], "long")
        assert "3 倍" in build_text(s, exp)

    def test_no_exposure_section_without_positions(self):
        assert "同向暴露检查" not in build_text(plan_size(10000, 100, 98, 0.5))

    def test_has_disclaimer(self):
        assert "不构成投资建议" in build_text(plan_size(10000, 100, 98, 0.5))


class TestTiers:
    def test_three_tiers_ascending(self):
        pcts = [p for _, p in TIERS]
        assert pcts == sorted(pcts)
        assert pcts == [0.25, 0.5, 1.0]
