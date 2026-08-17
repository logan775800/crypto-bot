"""引导式输入状态：绑会话、会过期、群里不拿闲聊当输入。

这是真机上暴露的问题：有人点过【👁 持续波动监控】没填完，之后他在群里说的
**每一句话**都被当成"在回答提示"，机器人逐条回「请发「币 百分比」，例如 DOGE 5」。
别人正常聊天被刷屏，而且没人知道怎么关掉。

三个原因叠在一起，缺一条都不至于这么糟：
  1. user_data 按用户存、跨会话共享——私聊点的按钮，群里发消息照样被拦；
  2. 永不过期，而且被持久化，重启都还在；
  3. 群里把闲聊当输入去纠正。
"""
import time

import pytest

from handlers import guided


class Chat:
    def __init__(self, cid, ctype="private"):
        self.id = cid
        self.type = ctype


class Update:
    def __init__(self, cid, ctype="private"):
        self.effective_chat = Chat(cid, ctype)


class Ctx:
    def __init__(self):
        self.user_data = {}


# ── 绑会话 ───────────────────────────────────────────────────────
def test_state_only_fires_in_the_chat_that_armed_it():
    """在私聊点的按钮，不该拦住这个人在群里的发言——这是刷屏的第一个原因。"""
    ctx = Ctx()
    guided.arm(ctx, "await_watchpct", Update(111))          # 私聊里点的
    assert guided.pending(ctx, "await_watchpct", Update(111)) is True
    assert guided.pending(ctx, "await_watchpct", Update(-1002)) is None


def test_other_chat_does_not_consume_the_state():
    """别的会话里不生效，但也不能把状态清掉——回到原会话还得能用。"""
    ctx = Ctx()
    guided.arm(ctx, "await_watchpct", Update(111))
    guided.pending(ctx, "await_watchpct", Update(-1002))
    assert guided.pending(ctx, "await_watchpct", Update(111)) is True


def test_arm_chat_binds_too():
    ctx = Ctx()
    guided.arm_chat(ctx, "await_alert", -1002, {"symbol": "BTC"})
    assert guided.pending(ctx, "await_alert", Update(-1002)) == {"symbol": "BTC"}
    assert guided.pending(ctx, "await_alert", Update(111)) is None


# ── 过期 ─────────────────────────────────────────────────────────
def test_state_expires():
    """点完按钮就走开了，5 分钟后不该还在拦人。"""
    ctx = Ctx()
    guided.arm(ctx, "await_watchpct", Update(1))
    ctx.user_data["await_watchpct"]["ts"] = time.time() - guided.TTL - 1
    assert guided.pending(ctx, "await_watchpct", Update(1)) is None


def test_expired_state_is_cleaned_up():
    ctx = Ctx()
    guided.arm(ctx, "await_watchpct", Update(1))
    ctx.user_data["await_watchpct"]["ts"] = time.time() - guided.TTL - 1
    guided.pending(ctx, "await_watchpct", Update(1))
    assert "await_watchpct" not in ctx.user_data


def test_fresh_state_survives():
    ctx = Ctx()
    guided.arm(ctx, "await_watchpct", Update(1))
    assert guided.pending(ctx, "await_watchpct", Update(1)) is True


# ── 老数据兼容 ───────────────────────────────────────────────────
def test_legacy_raw_value_still_works():
    """升级前存的是裸值（True / dict），不能因为格式变了就把人卡住。"""
    ctx = Ctx()
    ctx.user_data["await_watchpct"] = True
    assert guided.pending(ctx, "await_watchpct", Update(1)) is True
    ctx.user_data["await_alert"] = {"symbol": "BTC", "direction": "above"}
    assert guided.pending(ctx, "await_alert", Update(1))["symbol"] == "BTC"


# ── 群里不拿闲聊当输入 ───────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "DOGE 5", "BTC 3 合约gate".replace("合约gate", "gate"), "0x1234", "65000",
    "ARB", "1000 62000",
])
def test_parameter_lines_look_like_input(text):
    assert guided.looks_like_input(text)


@pytest.mark.parametrize("text", [
    "又不能做网格", "还是 牛牛强", "搞不了", "这个币怎么样？",
    "a" * 60, "", "   ",
])
def test_chat_does_not_look_like_input(text):
    """真机上就是这两句把机器人惹出来的：「又不能做网格」「还是 牛牛强」。"""
    assert not guided.looks_like_input(text)


def test_group_chat_is_skipped_silently():
    """群里的闲聊：静默放行，不回"格式不对"——那才是刷屏的直接原因。"""
    ctx = Ctx()
    guided.arm_chat(ctx, "await_watchpct", -1002)
    val, skip = guided.should_handle(ctx, "await_watchpct",
                                     Update(-1002, "supergroup"), "又不能做网格")
    assert val is None and skip is True


def test_group_state_survives_the_chatter():
    """闲聊不该把状态清掉——他后面真发「DOGE 5」还得能接住。"""
    ctx = Ctx()
    guided.arm_chat(ctx, "await_watchpct", -1002)
    guided.should_handle(ctx, "await_watchpct", Update(-1002, "supergroup"), "随便聊聊")
    val, skip = guided.should_handle(ctx, "await_watchpct",
                                     Update(-1002, "supergroup"), "DOGE 5")
    assert val is True and skip is False


def test_private_chat_still_gets_corrections():
    """私聊是一对一，回一句"格式不对"是帮忙不是打扰。"""
    ctx = Ctx()
    guided.arm(ctx, "await_watchpct", Update(111))
    val, skip = guided.should_handle(ctx, "await_watchpct", Update(111), "写错了啊")
    assert val is True and skip is False


def test_no_state_means_nothing_to_do():
    ctx = Ctx()
    val, skip = guided.should_handle(ctx, "await_watchpct",
                                     Update(-1002, "supergroup"), "DOGE 5")
    assert val is None and skip is False


# ── 接线 ─────────────────────────────────────────────────────────
def test_quickprice_uses_the_guard_everywhere():
    """任何一个引导分支漏掉这道闸，那条路就还会在群里刷屏。"""
    import inspect
    from handlers import quickprice
    src = inspect.getsource(quickprice.quick_price)
    for key in ("await_cmd", "await_track_addr", "await_ropen", "await_ropen_coin",
                "await_rsl", "await_watchpct", "await_alert_coin", "await_alert",
                "await_onchain"):
        assert f'should_handle(context, "{key}"' in src, f"{key} 没走引导闸"


def test_setters_bind_the_chat():
    import inspect
    from handlers import menu, onchain
    for mod in (menu, onchain):
        src = inspect.getsource(mod)
        assert 'user_data["await_' not in src, f"{mod.__name__} 还在裸写 await 状态"
