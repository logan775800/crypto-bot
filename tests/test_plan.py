"""交易计划：状态机 + AI 输出校验。

状态机判错 = 用户按一份已经失效的计划下单——而「防止这件事」正是这个功能存在的理由，
所以这里的用例比功能本身还重要。两条硬约束：
  1) 失效判定优先于一切（已证伪的计划不该再谈触发/止盈）
  2) 一律用**收盘价**判定，不用影线（插针假触发是这类计划最大的坑）
"""
import time

import pytest

from handlers.plan import (
    evaluate, _from_ai, card, STATUS, LIVE, rr, _clean_tps, MIN_TP_GAP_PCT,
    prefill_params,
)


def _plan(side="short", status="waiting", **kw):
    p = {
        "id": "p1", "chat_id": 1, "uid": 1,
        "symbol": "BANKUSDT", "side": side, "status": status,
        "created": time.time(), "updated": time.time(),
        "expires": time.time() + 3600,
        "risk_temp": 9, "note": "逆势空，轻仓",
        "trigger": {"desc": "5m 有效跌破 0.0806", "price": 0.0806, "mode": "breakdown"},
        "entry": [0.0806, 0.0812],
        "stop": 0.0828,
        "tps": [{"price": 0.0783, "pct": 40, "note": "止损保本"},
                {"price": 0.0755, "pct": 60}],
        "invalid": {"desc": "BANK 重回 0.0815 上方", "price": 0.0815, "dir": "above"},
        "hit_tps": [],
    }
    p.update(kw)
    return p


class TestInvalidation:
    def test_close_above_invalid_price_kills_the_plan(self):
        st, msg = evaluate(_plan(), close=0.0839, high=0.0840, low=0.0820)
        assert st == "invalid"
        assert "请勿继续按旧计划挂单" in msg

    def test_wick_above_invalid_price_does_not_kill_it(self):
        # 影线穿了但收盘没站上 → 计划还活着。用影线判会被插针洗掉
        st, _ = evaluate(_plan(), close=0.0810, high=0.0840, low=0.0805)
        assert st != "invalid"

    def test_long_plan_is_invalidated_by_breaking_down(self):
        p = _plan(side="long", invalid={"desc": "跌破 0.0790", "price": 0.0790,
                                        "dir": "below"})
        st, _ = evaluate(p, close=0.0785, high=0.0800, low=0.0780)
        assert st == "invalid"

    def test_invalidation_beats_take_profit(self):
        # 同一根K线里既碰到止盈又破了失效位 → 必须判失效。
        # 反过来会给用户一个「赚钱了」的错觉，而逻辑其实已经废了
        p = _plan(status="triggered")
        st, _ = evaluate(p, close=0.0839, high=0.0840, low=0.0700)
        assert st == "invalid"

    def test_invalidation_beats_trigger(self):
        st, _ = evaluate(_plan(status="waiting"), close=0.0839, high=0.0840, low=0.0805)
        assert st == "invalid"

    def test_no_invalid_price_means_no_invalidation(self):
        p = _plan(invalid={"desc": "看感觉", "price": None, "dir": "above"})
        st, _ = evaluate(p, close=0.9999, high=1.0, low=0.9)
        assert st != "invalid"


class TestExpiry:
    def test_expired_plan_is_flagged(self):
        p = _plan(expires=time.time() - 1)
        st, msg = evaluate(p, close=0.0810, high=0.0812, low=0.0808)
        assert st == "expired" and "已过期" in msg

    def test_invalidation_beats_expiry(self):
        p = _plan(expires=time.time() - 1)
        st, _ = evaluate(p, close=0.0839, high=0.0840, low=0.0820)
        assert st == "invalid"

    def test_not_yet_expired_is_untouched(self):
        st, _ = evaluate(_plan(expires=time.time() + 999), close=0.0810,
                         high=0.0812, low=0.0809)
        assert st is None


class TestTrigger:
    def test_breakdown_fires_on_close_below(self):
        st, msg = evaluate(_plan(), close=0.0804, high=0.0810, low=0.0803)
        assert st == "triggered" and "已触发" in msg

    def test_breakdown_does_not_fire_on_wick_only(self):
        st, _ = evaluate(_plan(), close=0.0810, high=0.0813, low=0.0800)
        assert st is None

    def test_breakout_fires_on_close_above(self):
        p = _plan(side="long", trigger={"desc": "站上", "price": 0.0850,
                                        "mode": "breakout"},
                  entry=[0.0850, 0.0860], stop=0.0830,
                  tps=[{"price": 0.0900}],
                  invalid={"desc": "跌回", "price": 0.0840, "dir": "below"})
        st, _ = evaluate(p, close=0.0855, high=0.0856, low=0.0851)
        assert st == "triggered"

    def test_zone_mode_fires_inside_entry_band(self):
        p = _plan(trigger={"desc": "进区间", "price": None, "mode": "zone"})
        st, _ = evaluate(p, close=0.0809, high=0.0810, low=0.0808)
        assert st == "triggered"

    def test_zone_mode_silent_outside_band(self):
        p = _plan(trigger={"desc": "进区间", "price": None, "mode": "zone"})
        st, _ = evaluate(p, close=0.0801, high=0.0803, low=0.0800)
        assert st is None

    def test_triggered_plan_does_not_retrigger(self):
        st, _ = evaluate(_plan(status="triggered"), close=0.0804,
                         high=0.0810, low=0.0803)
        assert st is None


class TestTakeProfit:
    def test_tp1_hit_moves_to_partial(self):
        p = _plan(status="triggered")
        st, msg = evaluate(p, close=0.0790, high=0.0795, low=0.0782)
        assert st == "partial"
        assert "TP1" in msg and "止损保本" in msg
        assert p["hit_tps"] == [0]

    def test_tp_uses_wick_not_close(self):
        # 止盈相反：挂的限价单被影线扫到就是成交了，这里用影线是对的
        p = _plan(status="triggered")
        st, _ = evaluate(p, close=0.0800, high=0.0805, low=0.0783)
        assert st == "partial"

    def test_last_tp_completes_the_plan(self):
        p = _plan(status="partial", hit_tps=[0])
        st, msg = evaluate(p, close=0.0760, high=0.0765, low=0.0754)
        assert st == "done"
        assert "全部止盈位已走完" in msg

    def test_same_tp_never_fires_twice(self):
        p = _plan(status="partial", hit_tps=[0])
        # 又回到 TP1 附近但没到 TP2 → 不该重复报
        st, _ = evaluate(p, close=0.0790, high=0.0795, low=0.0783)
        assert st is None

    def test_waiting_plan_ignores_tp(self):
        # 还没触发就不可能有仓位，止盈毫无意义
        st, _ = evaluate(_plan(status="waiting"), close=0.0783,
                         high=0.0790, low=0.0780)
        assert st != "partial"

    def test_long_tp_needs_high(self):
        # 只有一个 TP，命中即全部走完 → done（不是 partial）
        p = _plan(side="long", status="triggered", entry=[0.0800, 0.0810],
                  stop=0.0780, tps=[{"price": 0.0850}],
                  trigger={"desc": "x", "price": 0.0800, "mode": "breakout"},
                  invalid={"desc": "y", "price": 0.0770, "dir": "below"})
        assert evaluate(p, close=0.0840, high=0.0851, low=0.0835)[0] == "done"
        p["hit_tps"] = []
        # 高点没够到 TP → 不该报
        assert evaluate(p, close=0.0840, high=0.0845, low=0.0835)[0] is None


class TestDeadPlansAreLeftAlone:
    @pytest.mark.parametrize("st", ["invalid", "expired", "archived", "done"])
    def test_no_transitions_from_terminal_states(self, st):
        assert evaluate(_plan(status=st), 0.0839, 0.0840, 0.0700) == (None, None)

    def test_live_set_is_what_the_job_watches(self):
        assert set(LIVE) == {"waiting", "triggered", "partial"}
        for s in LIVE:
            assert s in STATUS


class TestFromAi:
    def _args(self, **kw):
        a = {
            "side": "short", "risk_temp": 9, "note": "逆势空",
            "trigger_desc": "5m 跌破 0.0806", "trigger_price": 0.0806,
            "trigger_mode": "breakdown",
            "entry_low": 0.0806, "entry_high": 0.0812, "stop": 0.0828,
            "tps": [{"price": 0.0783, "pct": 40}, {"price": 0.0755}],
            "invalid_desc": "站回 0.0815", "invalid_price": 0.0815,
            "reasoning": "理由",
        }
        a.update(kw)
        return a

    def test_happy_path(self):
        p = _from_ai(self._args(), "BANKUSDT", "short", 1, 1)
        assert p["side"] == "short" and p["stop"] == 0.0828
        assert p["entry"] == [0.0806, 0.0812]
        assert len(p["tps"]) == 2
        assert p["status"] == "waiting"

    def test_rejects_short_plan_whose_stop_is_below_entry(self):
        # 做空止损写到入场下方 = 止损变止盈，是会真亏钱的错误，必须拒绝而不是「修正」
        with pytest.raises(ValueError, match="止损"):
            _from_ai(self._args(stop=0.0790), "BANKUSDT", "short", 1, 1)

    def test_rejects_long_plan_whose_stop_is_above_entry(self):
        a = self._args(side="long", entry_low=0.0800, entry_high=0.0810,
                       stop=0.0850, tps=[{"price": 0.0900}],
                       invalid_price=0.0780)
        with pytest.raises(ValueError, match="止损"):
            _from_ai(a, "BANKUSDT", "long", 1, 1)

    def test_rejects_stop_that_is_too_tight(self):
        # 实测模型给过 0.11% 的止损，正常波动就会扫掉。
        # 这里做空、止损在入场上方（方向对），但只高出 ~0.09%：应被「太窄」拦下
        a = self._args(entry_low=63890, entry_high=63940, stop=63998,
                       tps=[{"price": 63000}], invalid_price=64100)
        with pytest.raises(ValueError, match="太窄"):
            _from_ai(a, "BTCUSDT", "short", 1, 1)

    def test_accepts_a_reasonable_stop(self):
        a = self._args(entry_low=63890, entry_high=63940, stop=64500,
                       tps=[{"price": 63000}], invalid_price=64100)
        p = _from_ai(a, "BTCUSDT", "short", 1, 1)
        assert p["stop"] == 64500

    def test_swapped_entry_bounds_are_normalized(self):
        p = _from_ai(self._args(entry_low=0.0812, entry_high=0.0806),
                     "BANKUSDT", "short", 1, 1)
        assert p["entry"] == [0.0806, 0.0812]

    def test_drops_take_profits_on_the_wrong_side(self):
        # 做空的止盈却在入场之上 = 反的，丢掉
        p = _from_ai(self._args(tps=[{"price": 0.0900}, {"price": 0.0783}]),
                     "BANKUSDT", "short", 1, 1)
        assert [t["price"] for t in p["tps"]] == [0.0783]

    def test_rejects_when_no_valid_take_profit_remains(self):
        with pytest.raises(ValueError, match="止盈"):
            _from_ai(self._args(tps=[{"price": 0.0900}]), "BANKUSDT", "short", 1, 1)

    def test_stop_pct_is_computed_from_entry_mid(self):
        p = _from_ai(self._args(), "BANKUSDT", "short", 1, 1)
        mid = (0.0806 + 0.0812) / 2
        assert p["stop_pct"] == pytest.approx(abs(0.0828 - mid) / mid * 100)

    def test_invalid_direction_follows_side(self):
        p = _from_ai(self._args(), "BANKUSDT", "short", 1, 1)
        assert p["invalid"]["dir"] == "above"
        a = self._args(side="long", entry_low=0.0800, entry_high=0.0810,
                       stop=0.0780, tps=[{"price": 0.0900}], invalid_price=0.0770)
        assert _from_ai(a, "BANKUSDT", "long", 1, 1)["invalid"]["dir"] == "below"

    def test_data_meta_snapshot_is_kept(self):
        class R:
            completeness = 72.7
            missing = ["清算数据"]
        p = _from_ai(self._args(), "BANKUSDT", "short", 1, 1, R())
        assert p["data_meta"]["completeness"] == 72.7


class TestRiskReward:
    """盈亏比。这两组用例来自一次真实生成：模型给出 R:R 0.32 的计划
    （冒 896 赚 285），而卡片当时根本没显示盈亏比 —— 用户看不出这单数学上就是亏的。"""

    def test_basic(self):
        assert rr(100, 98, 104) == pytest.approx(2.0)     # 赚4 亏2
        assert rr(100, 98, 101) == pytest.approx(0.5)

    def test_works_for_shorts(self):
        assert rr(100, 102, 96) == pytest.approx(2.0)

    def test_zero_risk_is_none_not_infinity(self):
        assert rr(100, 100, 104) is None

    def test_card_shows_rr_for_every_tp(self):
        c = card(_plan())
        assert c.count("R:R") == 2

    def test_bad_rr_is_impossible_to_miss(self):
        # 复刻实测那份计划的形状：TP 离入场太近
        p = _plan(entry=[0.0806, 0.0812], stop=0.0828,
                  tps=[{"price": 0.0806}], rr_final=0.32)
        c = card(p)
        assert "末段盈亏比只有 0.32" in c
        assert "期望也是负的" in c

    def test_good_rr_adds_no_warning(self):
        assert "期望也是负的" not in card(_plan(rr_final=2.4))

    def test_from_ai_computes_both_rr(self):
        a = {"side": "short", "risk_temp": 9, "trigger_desc": "x",
             "trigger_price": 0.0806, "trigger_mode": "breakdown",
             "entry_low": 0.0806, "entry_high": 0.0812, "stop": 0.0828,
             "tps": [{"price": 0.0783}, {"price": 0.0755}],
             "invalid_desc": "y", "invalid_price": 0.0815, "reasoning": "z"}
        p = _from_ai(a, "BANKUSDT", "short", 1, 1)
        mid = (0.0806 + 0.0812) / 2
        assert p["rr_first"] == pytest.approx(rr(mid, 0.0828, 0.0783))
        assert p["rr_final"] == pytest.approx(rr(mid, 0.0828, 0.0755))
        assert p["rr_final"] > p["rr_first"]


class TestCleanTps:
    """实测发现模型会给 63,683 和 63,698 这种只差 0.02% 的两个「分段」止盈。"""

    def test_merges_take_profits_that_are_practically_identical(self):
        tps = _clean_tps([{"price": 63683}, {"price": 63698}], "long",
                         63331, 63466, 62502, 63398)
        assert len(tps) == 1, "只差 0.02% 的两个 TP 是同一个位置，分段止盈成了摆设"

    def test_keeps_meaningfully_spaced_take_profits(self):
        tps = _clean_tps([{"price": 63683}, {"price": 64500}], "long",
                         63331, 63466, 62502, 63398)
        assert len(tps) == 2

    def test_gap_threshold_boundary(self):
        base = 10000.0
        near = base * (1 + (MIN_TP_GAP_PCT - 0.01) / 100)
        far = base * (1 + (MIN_TP_GAP_PCT + 0.05) / 100)
        assert len(_clean_tps([{"price": base}, {"price": near}], "long",
                              9000, 9500, 8000, 9250)) == 1
        assert len(_clean_tps([{"price": base}, {"price": far}], "long",
                              9000, 9500, 8000, 9250)) == 2

    def test_sorted_by_exit_order_for_shorts(self):
        # 做空先到的是价格高的
        tps = _clean_tps([{"price": 0.0755}, {"price": 0.0783}], "short",
                         0.0806, 0.0812, 0.0828, 0.0809)
        assert [t["price"] for t in tps] == [0.0783, 0.0755]

    def test_sorted_by_exit_order_for_longs(self):
        tps = _clean_tps([{"price": 0.0900}, {"price": 0.0850}], "long",
                         0.0800, 0.0810, 0.0780, 0.0805)
        assert [t["price"] for t in tps] == [0.0850, 0.0900]

    def test_wrong_side_dropped(self):
        tps = _clean_tps([{"price": 0.0900}, {"price": 0.0783}], "short",
                         0.0806, 0.0812, 0.0828, 0.0809)
        assert [t["price"] for t in tps] == [0.0783]

    def test_capped_at_three(self):
        raw = [{"price": p} for p in (0.079, 0.077, 0.075, 0.073, 0.071)]
        assert len(_clean_tps(raw, "short", 0.0806, 0.0812, 0.0828, 0.0809)) == 3

    def test_garbage_entries_skipped(self):
        tps = _clean_tps([{"price": "abc"}, {}, {"price": 0.0783}], "short",
                         0.0806, 0.0812, 0.0828, 0.0809)
        assert [t["price"] for t in tps] == [0.0783]

    def test_empty(self):
        assert _clean_tps(None, "short", 1, 2, 3, 1.5) == []


class TestPrefill:
    """计划 → 预填下单参数。这条路径最终会真下单，参数错=真金白银错，钉死。"""

    def test_uses_entry_midpoint(self):
        fp = prefill_params(_plan(entry=[0.0806, 0.0812]), 10000, 10)
        assert fp["entry"] == pytest.approx(0.0809)

    def test_margin_is_notional_over_leverage(self):
        # 风险 0.5%、权益 10000 → 风险 50U；入场中值 0.0809 止损 0.0828
        p = _plan(entry=[0.0806, 0.0812], stop=0.0828)
        f10 = prefill_params(p, 10000, 10)
        f20 = prefill_params(p, 10000, 20)
        # 名义不随杠杆变，保证金 = 名义/杠杆
        assert f10["notional"] == pytest.approx(f20["notional"])
        assert f10["margin"] == pytest.approx(f10["notional"] / 10)
        assert f20["margin"] == pytest.approx(f10["margin"] / 2)

    def test_risk_is_half_percent_of_equity(self):
        # 名义 × 止损距离 == 权益 × 0.5%
        p = _plan(entry=[0.0806, 0.0812], stop=0.0828)
        fp = prefill_params(p, 10000, 10)
        mid = 0.0809
        dist_pct = abs(0.0828 - mid) / mid * 100
        assert fp["notional"] * dist_pct / 100 == pytest.approx(10000 * 0.5 / 100)

    def test_carries_plan_stop_and_first_tp(self):
        fp = prefill_params(_plan(stop=0.0828,
                                  tps=[{"price": 0.0783}, {"price": 0.0755}]),
                            10000, 10)
        assert fp["sl"] == 0.0828
        assert fp["tp"] == 0.0783        # TP1，不是最后一个

    def test_side_and_symbol_passthrough(self):
        fp = prefill_params(_plan(side="short"), 10000, 10)
        assert fp["side"] == "short" and fp["symbol"] == "BANK"

    def test_no_equity_returns_none(self):
        assert prefill_params(_plan(), 0, 10) is None
        assert prefill_params(_plan(), None, 10) is None

    def test_zero_leverage_returns_none(self):
        assert prefill_params(_plan(), 10000, 0) is None


class TestCard:
    def test_one_screen_has_everything_needed_to_execute(self):
        c = card(_plan())
        for must in ("触发", "入场", "止损", "TP1", "TP2", "失效", "风险温度"):
            assert must in c, must

    def test_invalid_plan_screams_do_not_use(self):
        c = card(_plan(status="invalid", invalid_reason="站稳 0.0839"))
        assert "已失效" in c and "不要再按这份计划挂单" in c

    def test_expired_plan_says_replan(self):
        assert "/replan" in card(_plan(status="expired"))

    def test_degraded_data_is_disclosed_on_the_card(self):
        c = card(_plan(data_meta={"completeness": 72.0, "missing": ["清算数据"]}))
        assert "完整度 72%" in c and "精确度已打折" in c

    def test_full_data_adds_no_warning(self):
        assert "打折" not in card(_plan(data_meta={"completeness": 100.0, "missing": []}))

    def test_has_disclaimer(self):
        assert "不构成投资建议" in card(_plan())
