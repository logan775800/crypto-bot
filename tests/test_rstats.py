"""实盘复盘的纯函数测试——统计口径错了整个功能就是误导，这些必须锁住。"""
import pytest

from handlers.rstats import (
    norm_trade, open_times, attach_duration, funding_cost,
    compute_stats, _agg, _dur_bucket, build_stats_text, build_ai_digest,
)


def _closed(side="Sell", pnl="10", ts=1000, symbol="BTCUSDT", **kw):
    """伪造一行 Bybit closed-pnl。side 是**平仓单**方向。"""
    d = {"symbol": symbol, "side": side, "closedPnl": pnl, "updatedTime": str(ts),
         "avgEntryPrice": "100", "avgExitPrice": "110", "closedSize": "1",
         "cumEntryValue": "100", "leverage": "10", "orderId": f"o{ts}"}
    d.update(kw)
    return d


class TestNormTrade:
    def test_sell_close_means_it_was_a_long(self):
        # 这是最容易写反的一处：平仓单 Sell → 平的是多头
        assert norm_trade(_closed(side="Sell"))["side"] == "long"

    def test_buy_close_means_it_was_a_short(self):
        assert norm_trade(_closed(side="Buy"))["side"] == "short"

    def test_uses_exchange_pnl_verbatim(self):
        # 盈亏取交易所口径，不用入场/出场价自己乘
        t = norm_trade(_closed(pnl="-33.5", avgEntryPrice="100", avgExitPrice="110"))
        assert t["pnl"] == -33.5

    def test_missing_fields_do_not_explode(self):
        t = norm_trade({"symbol": "ETHUSDT", "side": "Buy"})
        assert t["pnl"] == 0 and t["side"] == "short" and t["ts"] == 0


class TestOpenTimes:
    def _ex(self, side, qty, ts, symbol="BTCUSDT", exec_type="Trade"):
        return {"symbol": symbol, "side": side, "execQty": str(qty),
                "execTime": str(ts), "execType": exec_type}

    def test_records_only_transition_from_flat(self):
        execs = [
            self._ex("Buy", 1, 100),     # 开仓 ← 记这个
            self._ex("Buy", 1, 200),     # 加仓，不是新开仓
            self._ex("Sell", 1, 300),    # 减仓
            self._ex("Sell", 1, 400),    # 平光
            self._ex("Buy", 2, 500),     # 再开仓 ← 记这个
        ]
        assert open_times(execs)["BTCUSDT"] == [100, 500]

    def test_funding_rows_never_count_as_position_changes(self):
        execs = [
            self._ex("Sell", 0, 50, exec_type="Funding"),
            self._ex("Buy", 1, 100),
        ]
        assert open_times(execs)["BTCUSDT"] == [100]

    def test_float_residue_still_counts_as_flat(self):
        # 分批平仓的浮点累计会留 1e-16 级残渣，不夹掉就永远认为还有仓、再也记不到开仓
        execs = [
            self._ex("Buy", 0.1, 100), self._ex("Buy", 0.2, 110),
            self._ex("Sell", 0.3, 200),
            self._ex("Buy", 1, 300),
        ]
        assert open_times(execs)["BTCUSDT"] == [100, 300]

    def test_symbols_tracked_independently(self):
        execs = [
            self._ex("Buy", 1, 100, symbol="BTCUSDT"),
            self._ex("Buy", 1, 150, symbol="ETHUSDT"),
            self._ex("Sell", 1, 200, symbol="BTCUSDT"),
        ]
        o = open_times(execs)
        assert o["BTCUSDT"] == [100] and o["ETHUSDT"] == [150]

    def test_short_position_open_is_detected(self):
        execs = [self._ex("Sell", 1, 100), self._ex("Buy", 1, 200)]
        assert open_times(execs)["BTCUSDT"] == [100]


class TestAttachDuration:
    def test_picks_last_open_before_close(self):
        trades = [norm_trade(_closed(ts=400)), norm_trade(_closed(ts=900))]
        attach_duration(trades, {"BTCUSDT": [100, 500]})
        assert trades[0]["dur"] == 0.3      # (400-100)/1000 秒
        assert trades[1]["dur"] == 0.4      # (900-500)/1000

    def test_no_open_record_yields_none_not_crash(self):
        # 执行明细拉失败/窗口外开的仓 → 时长未知，不能崩也不能瞎猜
        trades = [norm_trade(_closed(ts=400))]
        attach_duration(trades, {})
        assert trades[0]["dur"] is None

    def test_close_before_any_open_yields_none(self):
        trades = [norm_trade(_closed(ts=50))]
        attach_duration(trades, {"BTCUSDT": [100]})
        assert trades[0]["dur"] is None


class TestFundingCost:
    def test_sums_only_funding_rows(self):
        execs = [
            {"symbol": "BTCUSDT", "execType": "Funding", "execFee": "1.5"},
            {"symbol": "BTCUSDT", "execType": "Funding", "execFee": "-0.5"},
            {"symbol": "BTCUSDT", "execType": "Trade", "execFee": "99"},
        ]
        assert funding_cost(execs) == {"BTCUSDT": 1.0}


class TestComputeStats:
    def test_empty_returns_none(self):
        assert compute_stats([]) is None

    def test_core_metrics(self):
        trades = [norm_trade(_closed(pnl=p, ts=i))
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
        trades = [norm_trade(_closed(pnl=p, ts=i))
                  for i, p in enumerate(["100", "-40", "30"])]
        assert compute_stats(trades)["max_dd"] == 40.0

    def test_loss_streaks(self):
        trades = [norm_trade(_closed(pnl=p, ts=i))
                  for i, p in enumerate(["-1", "-1", "-1", "5", "-2", "-2"])]
        s = compute_stats(trades)
        assert s["max_loss_streak"] == 3
        assert s["cur_loss_streak"] == 2      # 尾部正在连亏

    def test_never_divides_by_zero_when_no_losses(self):
        trades = [norm_trade(_closed(pnl="10", ts=i)) for i in range(3)]
        s = compute_stats(trades)
        assert s["rr"] == float("inf")
        assert s["max_dd"] == 0.0

    def test_breakeven_trade_counts_as_neither(self):
        trades = [norm_trade(_closed(pnl="0", ts=1))]
        s = compute_stats(trades)
        assert s["wins"] == 0 and s["losses"] == 0


class TestAgg:
    def test_sorted_worst_first(self):
        trades = [
            norm_trade(_closed(symbol="AUSDT", pnl="5", ts=1)),
            norm_trade(_closed(symbol="BUSDT", pnl="-30", ts=2)),
            norm_trade(_closed(symbol="BUSDT", pnl="10", ts=3)),
        ]
        rows = _agg(trades, lambda t: t["symbol"])
        assert rows[0][0] == "BUSDT" and rows[0][1] == 2 and rows[0][2] == -20.0
        assert rows[0][3] == 50.0        # 2笔1胜

    def test_none_keys_are_skipped(self):
        trades = [norm_trade(_closed(ts=1))]
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
        trades = [norm_trade(_closed(pnl=p, ts=i * 1000))
                  for i, p in enumerate(["10", "-5"])]
        attach_duration(trades, {"BTCUSDT": [0]})
        out = build_stats_text(trades, 30, {"BTCUSDT": 2.0}, "🧪模拟盘")
        assert "胜率 50.0%" in out
        assert "期望值" in out
        assert "资金费净支出" in out

    def test_infinite_rr_renders_without_blowing_up_markdown(self):
        trades = [norm_trade(_closed(pnl="10", ts=1))]
        assert "∞" in build_stats_text(trades, 7)

    def test_ai_digest_is_compact_and_has_no_raw_rows(self):
        trades = [norm_trade(_closed(pnl=p, ts=i * 1000))
                  for i, p in enumerate(["10", "-5", "3"])]
        d = build_ai_digest(trades, 30)
        assert "胜率" in d and "期望值" in d
        assert len(d) < 2000

    def test_ai_digest_empty_is_none(self):
        assert build_ai_digest([], 30) is None
