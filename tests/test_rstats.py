"""复盘的纯函数测试——统计口径错了整个功能就是误导，这些必须锁住。

v1.70.0 起数据源只有虚拟盘；实盘 closed-pnl 规整/成交还原那几组测试随实盘一起删了，
统计和渲染的口径测试保留（虚拟盘 load_virtual 产出的是同一结构）。
"""
import pytest

from handlers.rstats import (
    compute_stats, _agg, _dur_bucket, build_stats_text, build_ai_digest,
)


def _trade(pnl="10", ts=1000, symbol="BTCUSDT", side="long"):
    """伪造一笔已平仓交易（load_virtual 的输出结构）。"""
    return {"symbol": symbol, "side": side, "pnl": float(pnl), "entry": 100.0,
            "exit": 110.0, "qty": 1.0, "value": 100.0, "lev": 10.0, "ts": int(ts)}


class TestComputeStats:
    def test_empty_returns_none(self):
        assert compute_stats([]) is None

    def test_core_metrics(self):
        trades = [_trade(pnl=p, ts=i)
                  for i, p in enumerate(["10", "-5", "20", "-5"])]
        s = compute_stats(trades)
        assert s["n"] == 4 and s["wins"] == 2 and s["losses"] == 2
        assert s["win_rate"] == 50.0
        assert s["total"] == 20.0
        assert s["avg_win"] == 15.0 and s["avg_loss"] == 5.0
        assert s["rr"] == 3.0
        assert s["expectancy"] == 5.0

    def test_max_drawdown_is_peak_to_trough(self):
        # 曲线 100 → 60 → 90：峰值100，谷底60 → 回撤40
        trades = [_trade(pnl=p, ts=i)
                  for i, p in enumerate(["100", "-40", "30"])]
        assert compute_stats(trades)["max_dd"] == 40.0

    def test_loss_streaks(self):
        trades = [_trade(pnl=p, ts=i)
                  for i, p in enumerate(["-1", "-1", "-1", "5", "-2", "-2"])]
        s = compute_stats(trades)
        assert s["max_loss_streak"] == 3
        assert s["cur_loss_streak"] == 2      # 尾部正在连亏

    def test_never_divides_by_zero_when_no_losses(self):
        trades = [_trade(pnl="10", ts=i) for i in range(3)]
        s = compute_stats(trades)
        assert s["rr"] == float("inf")
        assert s["max_dd"] == 0.0

    def test_breakeven_trade_counts_as_neither(self):
        trades = [_trade(pnl="0", ts=1)]
        s = compute_stats(trades)
        assert s["wins"] == 0 and s["losses"] == 0


class TestAgg:
    def test_sorted_worst_first(self):
        trades = [
            _trade(symbol="AUSDT", pnl="5", ts=1),
            _trade(symbol="BUSDT", pnl="-30", ts=2),
            _trade(symbol="BUSDT", pnl="10", ts=3),
        ]
        rows = _agg(trades, lambda t: t["symbol"])
        assert rows[0][0] == "BUSDT" and rows[0][1] == 2 and rows[0][2] == -20.0
        assert rows[0][3] == 50.0        # 2笔1胜

    def test_none_keys_are_skipped(self):
        trades = [_trade(ts=1)]
        assert _agg(trades, lambda t: None) == []


class TestDurBucket:
    @pytest.mark.parametrize("sec,expect", [
        (10, "<5分钟"), (299, "<5分钟"), (300, "5~30分钟"),
        (3600, "30分钟~2小时"), (100000, ">24小时"),
    ])
    def test_buckets(self, sec, expect):
        assert _dur_bucket({"dur": sec}) == expect

    def test_unknown_duration_is_none(self):
        assert _dur_bucket({"dur": None}) is None


class TestRender:
    def test_empty_text_is_friendly_not_a_crash(self):
        out = build_stats_text([], 30)
        assert "没有已平仓记录" in out

    def test_text_contains_headline_numbers(self):
        trades = [_trade(pnl=p, ts=i * 1000)
                  for i, p in enumerate(["10", "-5"])]
        for t in trades:
            t["dur"] = t["ts"] / 1000
        out = build_stats_text(trades, 30, {"BTCUSDT": 2.0}, "🧪模拟盘")
        assert "胜率 50.0%" in out
        assert "期望值" in out
        assert "资金费净支出" in out

    def test_infinite_rr_renders_without_blowing_up_markdown(self):
        trades = [_trade(pnl="10", ts=1)]
        assert "∞" in build_stats_text(trades, 7)

    def test_ai_digest_is_compact_and_has_no_raw_rows(self):
        trades = [_trade(pnl=p, ts=i * 1000)
                  for i, p in enumerate(["10", "-5", "3"])]
        d = build_ai_digest(trades, 30)
        assert "胜率" in d and "期望值" in d
        assert len(d) < 2000

    def test_ai_digest_empty_is_none(self):
        assert build_ai_digest([], 30) is None
