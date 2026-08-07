"""会话交易上下文 —— 用户是连着说话的：
「分析AKE」→「回踩能不能多」→「我已经在0.00405做多」→「现在要不要减仓」。
每句重新猜币种，后三句就全答偏了。
"""
import time

import pytest

from handlers import chat


class _Ctx:
    """够用的 chat_data 替身。"""
    def __init__(self):
        self.chat_data = {}


def test_symbol_is_remembered():
    c = _Ctx()
    chat.update_ctx(c, "", "AKEUSDT")
    assert "AKEUSDT" in chat.ctx_hint(c)


def test_entry_price_parsed_from_natural_speech():
    c = _Ctx()
    chat.update_ctx(c, "我已经在0.00405做多了", "AKEUSDT")
    assert c.chat_data["trade_ctx"]["entry"] == pytest.approx(0.00405)
    assert c.chat_data["trade_ctx"]["side"] == "long"


def test_side_parsed_without_price():
    c = _Ctx()
    chat.update_ctx(c, "这个位置我想做空", "AKEUSDT")
    assert c.chat_data["trade_ctx"]["side"] == "short"


def test_later_message_without_symbol_keeps_it():
    """「现在要不要减仓」没提币种，必须还知道是 AKE。"""
    c = _Ctx()
    chat.update_ctx(c, "分析AKE", "AKEUSDT")
    chat.update_ctx(c, "现在要不要减仓")
    assert c.chat_data["trade_ctx"]["symbol"] == "AKEUSDT"


def test_switching_symbol_drops_stale_entry():
    """换币了，上一个币的入场价必须作废——沿用过去会算出完全错误的盈亏。"""
    c = _Ctx()
    chat.update_ctx(c, "我在0.00405做多", "AKEUSDT")
    chat.update_ctx(c, "看看BTC", "BTCUSDT")
    ctx = c.chat_data["trade_ctx"]
    assert ctx["symbol"] == "BTCUSDT" and "entry" not in ctx


def test_context_expires():
    """隔夜的入场价不该被当成现在的。"""
    c = _Ctx()
    chat.update_ctx(c, "我在0.00405做多", "AKEUSDT")
    c.chat_data["trade_ctx"]["ts"] = time.time() - chat._CTX_TTL - 10
    assert chat.ctx_hint(c) == ""


def test_empty_context_produces_no_hint():
    """没有上下文时绝不能凭空造一个币出来。"""
    assert chat.ctx_hint(_Ctx()) == ""


def test_hint_marks_entry_as_user_claimed():
    """入场价是用户自己说的，不是取数得到的——必须标明来源，
    否则模型会把它当成实际成交价写进结论。"""
    c = _Ctx()
    chat.update_ctx(c, "我在0.00405做多", "AKEUSDT")
    h = chat.ctx_hint(c)
    assert "用户自述" in h and "不是行情数据" in h


def test_hint_tells_model_not_to_fall_back_to_btc():
    c = _Ctx()
    chat.update_ctx(c, "", "AKEUSDT")
    assert "不要换成 BTC" in chat.ctx_hint(c)
