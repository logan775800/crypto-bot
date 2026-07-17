"""条件提醒的解析与求值测试。
这功能最坏的失败模式是**静默**：条件解析错或求值错，规则永远不触发，
用户以为在盯盘、实际什么都没有。所以解析必须要么正确要么明确报错，绝不含糊放过。
"""
import pytest

from handlers.condalert import parse_cond, eval_rule, cond_text, rule_text


class TestParsePrice:
    @pytest.mark.parametrize("tok,op,val", [
        ("<60000", "<", 60000.0),
        (">70000", ">", 70000.0),
        ("price<60000", "<", 60000.0),
        ("<0.0035", "<", 0.0035),
        ("<60,000", "<", 60000.0),      # 手打逗号很常见
        ("＜60000", "<", 60000.0),      # 中文输入法全角
    ])
    def test_price_forms(self, tok, op, val):
        c = parse_cond(tok)
        assert c == {"kind": "price", "op": op, "val": val}


class TestParseRsi:
    def test_defaults_to_15m(self):
        assert parse_cond("rsi<30") == {"kind": "rsi", "iv": "15m", "op": "<", "val": 30.0}

    def test_explicit_interval(self):
        assert parse_cond("rsi1h>70") == {"kind": "rsi", "iv": "1h", "op": ">", "val": 70.0}

    def test_case_insensitive(self):
        assert parse_cond("RSI4H<25")["iv"] == "4h"

    def test_unsupported_interval_is_rejected_not_silently_defaulted(self):
        # 悄悄退回 15m 会让用户以为在盯 3m —— 宁可报错
        assert parse_cond("rsi3m<30") is None


class TestParseOthers:
    def test_chg_negative_value(self):
        assert parse_cond("chg1h<-3") == {"kind": "chg", "iv": "1h", "op": "<", "val": -3.0}

    def test_ema_has_no_value(self):
        assert parse_cond("ema20>") == {"kind": "ema", "n": 20, "op": ">", "iv": "15m"}

    def test_ema_only_known_periods(self):
        assert parse_cond("ema33>") is None

    def test_vol(self):
        assert parse_cond("vol>2") == {"kind": "vol", "iv": "15m", "op": ">", "val": 2.0}


class TestParseRejects:
    @pytest.mark.parametrize("tok", ["", "btc", "rsi", "<", "rsi<", "<<30",
                                     "rsi<=30", "foo>1", "60000"])
    def test_garbage_returns_none(self, tok):
        assert parse_cond(tok) is None


def _snap(**kw):
    base = {"close": 100.0, "rsi": 50.0, "ema20": 90.0, "ema50": 95.0,
            "ema200": 80.0, "chg": 0.0, "vol": 1.0}
    base.update(kw)
    return {"15m": base}


def _rule(*toks):
    return {"symbol": "BTC", "conds": [parse_cond(t) for t in toks]}


class TestEval:
    def test_single_condition_hit(self):
        hit, detail = eval_rule(_rule("<200"), _snap(close=100))
        assert hit and "价格" in detail

    def test_single_condition_miss(self):
        hit, _ = eval_rule(_rule("<50"), _snap(close=100))
        assert not hit

    def test_all_conditions_must_hold(self):
        # 价格满足但 RSI 不满足 → 不叫。这正是 /cond 存在的意义
        hit, _ = eval_rule(_rule("<200", "rsi<30"), _snap(close=100, rsi=50))
        assert not hit

    def test_all_hold_fires_with_every_condition_in_detail(self):
        hit, detail = eval_rule(_rule("<200", "rsi<30"), _snap(close=100, rsi=25))
        assert hit
        assert detail.count("✓") == 2

    def test_ema_above(self):
        hit, detail = eval_rule(_rule("ema20>"), _snap(close=100, ema20=90))
        assert hit and "EMA20" in detail

    def test_ema_below_when_price_is_above_does_not_fire(self):
        hit, _ = eval_rule(_rule("ema20<"), _snap(close=100, ema20=90))
        assert not hit

    def test_negative_chg(self):
        assert eval_rule(_rule("chg1h<-3"), {"1h": {"close": 1, "chg": -5.0}})[0]
        assert not eval_rule(_rule("chg1h<-3"), {"1h": {"close": 1, "chg": -1.0}})[0]

    def test_missing_interval_does_not_fire(self):
        # 该周期取数失败 → 宁可漏报也不误报
        hit, _ = eval_rule(_rule("rsi1h<30"), _snap(rsi=10))
        assert not hit

    def test_none_indicator_does_not_fire(self):
        # K线不够长时 rsi/ema 会是 None，绝不能被当成 0 而误判「<30 成立」
        hit, _ = eval_rule(_rule("rsi<30"), _snap(rsi=None))
        assert not hit

    def test_none_ema_does_not_fire(self):
        hit, _ = eval_rule(_rule("ema200>"), _snap(ema200=None))
        assert not hit

    def test_mixed_intervals(self):
        snap = {"15m": {"close": 100, "rsi": 20}, "1h": {"close": 100, "chg": -6.0}}
        assert eval_rule(_rule("rsi15m<30", "chg1h<-5"), snap)[0]


class TestRender:
    def test_cond_text_is_human_readable(self):
        assert cond_text(parse_cond("<60000")) == "价格 < 60000"
        assert cond_text(parse_cond("rsi1h>70")) == "1h RSI > 70"
        assert "%" in cond_text(parse_cond("chg15m>2"))

    def test_rule_text_joins_with_and(self):
        t = rule_text(_rule("<60000", "rsi<30"))
        assert "BTC" in t and "且" in t
