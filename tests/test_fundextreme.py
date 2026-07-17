"""资金费极端榜测试。核心是**归一化**：1h 结算的币抽血是常规 8h 的 8 倍，
只按每期费率排序会把最危险的币排在榜外——那正是用户被埋的地方。"""
import pytest

from handlers.fundextreme import _row, _hi, build_text


class TestNormalizeToDaily:
    def test_standard_8h_interval(self):
        r = _row("Bybit", "BTC", 0.01, 480, 1e9, 60000)
        assert r["daily"] == 0.03          # 一天 3 期

    def test_1h_interval_is_eight_times_worse(self):
        a = _row("Bybit", "STX", 0.01, 60, 1e8, 1)
        b = _row("Bybit", "BTC", 0.01, 480, 1e9, 1)
        # 同样的「每期 0.01%」，1h 结算的实际日化是 8h 的 8 倍
        assert a["daily"] == 0.24
        assert abs(a["daily"] / b["daily"] - 8) < 1e-9

    def test_4h_interval(self):
        assert _row("Bybit", "X", 0.01, 240, 1e8, 1)["daily"] == 0.06

    def test_missing_interval_defaults_to_8h(self):
        assert _row("Bybit", "X", 0.01, None, 1e8, 1)["daily"] == 0.03

    def test_negative_rate_stays_negative(self):
        assert _row("Bybit", "X", -0.05, 60, 1e8, 1)["daily"] == pytest.approx(-1.2)


class TestHighFrequencyTag:
    def test_8h_gets_no_tag(self):
        assert _hi({"mins": 480}) == ""

    def test_1h_is_flagged(self):
        assert "1h结算" in _hi({"mins": 60})

    def test_4h_is_flagged(self):
        assert "4h结算" in _hi({"mins": 240})


class TestBuildText:
    def test_empty_is_graceful(self):
        assert "取不到" in build_text([])

    def test_sorted_by_daily_not_by_raw_rate(self):
        rows = [
            _row("Bybit", "SLOW", 0.05, 480, 1e9, 1),    # 每期最高，日化 0.15%
            _row("Bybit", "FAST", 0.02, 60, 1e8, 1),     # 每期较低，日化 0.48% ← 才是真正的坑
        ]
        out = build_text(rows)
        # FAST 必须排在 SLOW 前面（多头付费榜按日化降序）
        assert out.index("FAST") < out.index("SLOW")

    def test_high_frequency_warning_appears(self):
        rows = [_row("Bybit", "FAST", 0.02, 60, 1e8, 1)]
        assert "高频结算" in build_text(rows)

    def test_no_warning_when_all_standard(self):
        rows = [_row("Bybit", "BTC", 0.01, 480, 1e9, 1)]
        assert "高频结算" not in build_text(rows)

    def test_positive_and_negative_are_split(self):
        rows = [_row("Bybit", "POS", 0.05, 480, 1e9, 1),
                _row("Bybit", "NEG", -0.05, 480, 1e9, 1)]
        out = build_text(rows)
        assert "空头付费最多" in out and "多头付费最多" in out
        assert out.index("NEG") < out.index("POS")

    def test_exchange_name_is_shown(self):
        assert "Binance" in build_text([_row("Binance", "X", 0.05, 480, 1e9, 1)])
