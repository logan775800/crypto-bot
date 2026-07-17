"""风险守护的判定逻辑测试。这些函数决定「叫不叫」，误报会训练用户忽略告警，漏报更糟。"""
import pytest

from handlers import riskguard as rg


@pytest.fixture(autouse=True)
def _clean_cfg():
    """每个用例都从干净配置开始，别让上一个用例的阈值/冷却串味。"""
    from storage import data
    data["riskguard"] = {}
    yield
    data["riskguard"] = {}


def _pos(symbol="BTCUSDT", side="Buy", value="1000", sl="0", upnl="0", lev="10"):
    return {"symbol": symbol, "side": side, "positionValue": value,
            "stopLoss": sl, "unrealisedPnl": upnl, "leverage": lev,
            "markPrice": "100", "liqPrice": "50"}


class TestMMR:
    def test_fires_above_threshold(self):
        out = rg.check_mmr({"accountMMRate": "0.55", "totalEquity": "1000"})
        assert out and "55.0%" in out

    def test_silent_below_threshold(self):
        assert rg.check_mmr({"accountMMRate": "0.1", "totalEquity": "1000"}) is None

    def test_missing_field_is_silent_not_a_crash(self):
        # 非统一账户/接口没返回时不能崩，也不能当成 0 风险去叫
        assert rg.check_mmr({"totalEquity": "1000"}) is None
        assert rg.check_mmr({"accountMMRate": ""}) is None

    def test_garbage_value_is_silent(self):
        assert rg.check_mmr({"accountMMRate": "n/a"}) is None

    def test_respects_custom_threshold(self):
        from storage import data
        data["riskguard"]["mmr"] = 80.0
        assert rg.check_mmr({"accountMMRate": "0.55"}) is None


class TestConcentration:
    def test_needs_at_least_three_positions(self):
        # 两个同向仓不算「集中」，那是正常交易
        assert rg.check_concentration([_pos("AUSDT"), _pos("BUSDT")]) is None

    def test_fires_when_all_same_direction(self):
        ps = [_pos("AUSDT"), _pos("BUSDT"), _pos("CUSDT")]
        out = rg.check_concentration(ps)
        assert out and "做多" in out and "3 个仓" in out

    def test_flags_altcoin_share(self):
        ps = [_pos("AUSDT"), _pos("BUSDT"), _pos("CUSDT")]
        assert "山寨" in rg.check_concentration(ps)

    def test_majors_only_does_not_get_alt_warning(self):
        ps = [_pos("BTCUSDT"), _pos("ETHUSDT"), _pos("BTCUSDT")]
        out = rg.check_concentration(ps)
        assert out and "山寨" not in out

    def test_balanced_book_is_silent(self):
        ps = [_pos("AUSDT", side="Buy", value="1000"),
              _pos("BUSDT", side="Sell", value="1000"),
              _pos("CUSDT", side="Sell", value="800")]
        assert rg.check_concentration(ps) is None

    def test_short_side_concentration_also_fires(self):
        ps = [_pos("AUSDT", side="Sell"), _pos("BUSDT", side="Sell"),
              _pos("CUSDT", side="Sell")]
        assert "做空" in rg.check_concentration(ps)

    def test_zero_notional_does_not_divide_by_zero(self):
        ps = [_pos("AUSDT", value="0"), _pos("BUSDT", value="0"), _pos("CUSDT", value="0")]
        assert rg.check_concentration(ps) is None


class TestDaily:
    def test_first_call_sets_baseline_and_stays_silent(self):
        from storage import data
        assert rg.check_daily(1000.0) is None
        assert data["riskguard"]["day"]["start"] == 1000.0

    def test_fires_once_drawdown_exceeds_threshold(self):
        rg.check_daily(1000.0)
        assert rg.check_daily(980.0) is None       # -2%，没到 5%
        out = rg.check_daily(940.0)               # -6%
        assert out and "-6.00%" in out

    def test_never_fires_twice_in_one_day(self):
        rg.check_daily(1000.0)
        assert rg.check_daily(900.0) is not None
        assert rg.check_daily(800.0) is None      # 已熔断，当天闭嘴

    def test_new_day_resets_baseline_and_fired_flag(self):
        from storage import data
        rg.check_daily(1000.0)
        rg.check_daily(900.0)
        data["riskguard"]["day"]["date"] = "1999-01-01"   # 假装隔天
        assert rg.check_daily(900.0) is None              # 重设基准，不该叫
        assert data["riskguard"]["day"]["start"] == 900.0
        assert data["riskguard"]["day"]["fired"] is False

    def test_profit_never_fires(self):
        rg.check_daily(1000.0)
        assert rg.check_daily(1200.0) is None

    def test_custom_threshold(self):
        from storage import data
        data["riskguard"]["daily"] = 2.0
        rg.check_daily(1000.0)
        assert rg.check_daily(970.0) is not None    # -3% > 2%


class TestNoStopLoss:
    def test_fires_for_naked_position(self):
        out = rg.check_no_sl([_pos(sl="0")])
        assert out and "没设止损" in out

    @pytest.mark.parametrize("sl", ["0", "0.0", "", None])
    def test_all_empty_forms_count_as_naked(self, sl):
        assert rg.check_no_sl([_pos(sl=sl)]) is not None

    def test_position_with_stop_is_silent(self):
        assert rg.check_no_sl([_pos(sl="59000")]) is None

    def test_only_naked_ones_are_listed(self):
        out = rg.check_no_sl([_pos("AUSDT", sl="100"), _pos("BUSDT", sl="0")])
        assert "B" in out and "AUSDT" not in out


class TestChecksToggle:
    def test_checks_default_to_on(self):
        assert rg._on("mmr") is True

    def test_can_be_turned_off(self):
        from storage import data
        data["riskguard"]["checks"] = {"mmr": False}
        assert rg._on("mmr") is False
        assert rg._on("conc") is True     # 其它项不受影响


class TestCooldown:
    def test_first_call_passes_then_blocks(self):
        assert rg._cool_ok("mmr", 3600) is True
        assert rg._cool_ok("mmr", 3600) is False

    def test_keys_are_independent(self):
        assert rg._cool_ok("mmr", 3600) is True
        assert rg._cool_ok("conc", 3600) is True

    def test_zero_cooldown_always_passes(self):
        assert rg._cool_ok("x", 0) is True
        assert rg._cool_ok("x", 0) is True
