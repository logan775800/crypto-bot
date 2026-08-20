"""实盘引导开仓：保证金改成按钮，提示必须写明是实盘。

真机（2026-08-18 14:18）：他在实盘引导流程里被要求「请发「保证金 价格」」，
那句提示**一个字都没提这是真钱**——而他账户里有 198 USDT 实盘。
他试了 `0 0.0097`，然后问「保证金一定要吗？」。

两个问题：
  1. 保证金只能打字。要，它决定仓位大小，但不该靠打字。
  2. `await_ropen` 当时是直接写 `context.user_data[...]`，绕过了 guided ——
     **永不过期、跨会话生效**，正是 v1.23.1 修掉的坑，这个调用点漏了。
"""
import inspect
import time
import types

import pytest

from handlers import rtrade as R
from handlers import guided as G


def test_margin_step_exists_with_buttons():
    assert hasattr(R, "guided_margin") and hasattr(R, "guided_price")
    src = inspect.getsource(R.guided_margin)
    assert "topm:" in src, "保证金要做成按钮回调"
    assert "topx:" in src, "留一个「自己输」的出口"


def test_lev_step_leads_to_margin_buttons():
    """选完杠杆应该进保证金按钮页，而不是直接让他打字。"""
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert "guided_margin" in src
    assert "topm:" in src and "topx:" in src


def test_margin_choices_are_filtered_by_balance():
    """列出他根本下不起的数字只会浪费一次点击。"""
    src = inspect.getsource(R.guided_margin)
    assert "equity is None or m <= equity" in src


def test_state_is_armed_through_guided_not_raw():
    """绕过 guided 就是永不过期 + 跨会话，那个坑不能再踩。"""
    for fn in (R.guided_price, R.guided_amount):
        src = inspect.getsource(fn)
        assert "arm_chat" in src
        assert 'context.user_data["await_ropen"] =' not in src


def test_armed_state_expires_and_binds_to_chat():
    ctx = types.SimpleNamespace(user_data={})
    G.arm_chat(ctx, "await_ropen", 555, value={"symbol": "BTCUSDT"})
    upd_same = types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=555, type="private"))
    upd_other = types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=999, type="private"))
    assert G.pending(ctx, "await_ropen", upd_same)
    assert G.pending(ctx, "await_ropen", upd_other) is None, "别的会话不该生效"
    ctx.user_data["await_ropen"]["ts"] = time.time() - G.TTL - 1
    assert G.pending(ctx, "await_ropen", upd_same) is None, "过期要自动失效"


# ── 只发价格 ────────────────────────────────────────────────
def _run_quickprice(ro, text):
    """跑 quickprice 里 await_ropen 那一段，收集回复和最终下单参数。"""
    import asyncio
    from handlers import quickprice as Q
    said, prepared = [], []

    class Msg:
        chat_id = 1

        def __init__(self, t):
            self.text = t

        async def reply_text(self, t, **kw):
            said.append(t)

    async def fake_prepare(message, context, symbol, side, margin, lev, price,
                           tp=None, sl=None):
        prepared.append((symbol, side, margin, lev, price, tp, sl))

    ctx = types.SimpleNamespace(user_data={})
    G.arm_chat(ctx, "await_ropen", 1, value=ro)
    upd = types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=1, type="private"),
        effective_user=types.SimpleNamespace(id=1),
        message=Msg(text))
    orig = R.prepare_open
    R.prepare_open = fake_prepare
    try:
        asyncio.run(Q.quick_price(upd, ctx))
    finally:
        R.prepare_open = orig
    return said, prepared


def test_preset_margin_only_needs_a_price():
    """保证金按钮选过了，就只要一个价格。"""
    said, prepared = _run_quickprice(
        {"symbol": "BTCUSDT", "side": "long", "lev": 10.0, "margin": 500.0},
        "62000")
    assert prepared and prepared[0][2] == 500.0 and prepared[0][4] == 62000.0


def test_zero_margin_explains_what_margin_is():
    """他问的就是这个：保证金一定要吗。答案要讲清为什么。"""
    said, prepared = _run_quickprice(
        {"symbol": "BTCUSDT", "side": "long", "lev": 10.0}, "0 0.0097")
    assert not prepared
    assert said and "保证金 × 杠杆" in said[0]
    assert "按钮" in said[0]


def test_prompts_carry_the_environment_tag():
    """这套引导和虚拟盘长得几乎一样，而这边一确认就是真钱。"""
    said, _ = _run_quickprice(
        {"symbol": "BTCUSDT", "side": "long", "lev": 10.0}, "只有一个数")
    # 重点是**说了是哪个账户**：交易所名 + 环境，两个都要有
    assert said and any(t in said[0] for t in ("实盘", "模拟", "测试网", "测试站"))
    assert said and any(t in said[0] for t in ("Bybit", "币安"))
