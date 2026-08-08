"""双实例抢 token 的自动告警。

这个症状（有的命令回、有的不回）前后靠人工猜过两次才定位，
而它其实是可检测的：Telegram 对第二个 getUpdates 返回 409 Conflict。
"""
import asyncio
import datetime

import pytest
from telegram.error import Conflict

import bot as botmod


class _Ctx:
    def __init__(self, err):
        self.error = err
        self.bot_data = {}
        self.bot = None


@pytest.fixture
def notified(monkeypatch):
    sent = []

    async def fake(_ctx, text):
        sent.append(text)
    import handlers.monitor as mon
    monkeypatch.setattr(mon, "notify_admin", fake)
    return sent


def test_conflict_triggers_a_specific_alert(notified):
    ctx = _Ctx(Conflict("terminated by other getUpdates request"))
    asyncio.run(botmod.on_error(None, ctx))
    assert notified and "双实例" in notified[0]
    assert "docker compose down" in notified[0]      # 要能直接照做


def test_conflict_is_throttled(notified):
    """409 会持续刷，半小时内只报一次，否则告警本身变成刷屏。"""
    ctx = _Ctx(Conflict("x"))
    asyncio.run(botmod.on_error(None, ctx))
    asyncio.run(botmod.on_error(None, ctx))
    asyncio.run(botmod.on_error(None, ctx))
    assert len(notified) == 1


def test_throttle_expires(notified):
    ctx = _Ctx(Conflict("x"))
    asyncio.run(botmod.on_error(None, ctx))
    ctx.bot_data["conflict_notified"] = (
        datetime.datetime.now().timestamp() - 3600)
    asyncio.run(botmod.on_error(None, ctx))
    assert len(notified) == 2


def test_other_errors_do_not_use_the_conflict_path(notified):
    """普通异常仍然走通用上报，不能被这条分支吃掉。"""
    ctx = _Ctx(RuntimeError("普通错误"))
    asyncio.run(botmod.on_error(None, ctx))
    assert not any("双实例" in t for t in notified)


def test_alert_failure_does_not_raise(monkeypatch):
    """告警发不出去也不能让错误处理器自己炸掉。"""
    async def boom(_ctx, _text):
        raise RuntimeError("发送失败")
    import handlers.monitor as mon
    monkeypatch.setattr(mon, "notify_admin", boom)
    asyncio.run(botmod.on_error(None, _Ctx(Conflict("x"))))
