"""长轮询偶发断线不该推「机器人异常」。

线上 6 周 113 条「🐞 机器人异常」全是 getUpdates 的 Bad Gateway / ReadError，
PTB 会自己重试、消息不丢，推出来只是噪音。只有连续断线撑过 3 分钟才告警。
"""
import asyncio
import datetime

import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

import bot as botmod


class _Ctx:
    def __init__(self, err, job=None):
        self.error = err
        self.bot_data = {}
        self.bot = None
        self.job = job


@pytest.fixture
def notified(monkeypatch):
    sent = []

    async def fake(_ctx, text):
        sent.append(text)
    import handlers.monitor as mon
    monkeypatch.setattr(mon, "notify_admin", fake)
    return sent


@pytest.mark.parametrize("err", [NetworkError("Bad Gateway"),
                                 NetworkError("httpx.ReadError: "),
                                 TimedOut()])
def test_single_poll_blip_is_silent(notified, err):
    asyncio.run(botmod.on_error(None, _Ctx(err)))
    assert notified == []


def _run_at(monkeypatch, ctx, t):
    class _DT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime.fromtimestamp(t)
    monkeypatch.setattr(botmod.datetime, "datetime", _DT)
    asyncio.run(botmod.on_error(None, ctx))


def test_sustained_outage_alerts_once(notified, monkeypatch):
    """每 30 秒失败一次（PTB 退避上限）、持续 4 分钟 → 告警一次，不刷屏。"""
    ctx = _Ctx(NetworkError("Bad Gateway"))
    t0 = 1_800_000_000
    for i in range(9):                       # 0..240 秒
        _run_at(monkeypatch, ctx, t0 + i * 30)
    assert len(notified) == 1
    assert "连不上 Telegram" in notified[0] and "3 分钟" in notified[0]   # 第 180 秒那次触发


def test_separate_blips_do_not_add_up(notified, monkeypatch):
    """间隔超过一分钟的零星断线各算各的，攒不成告警（历史上一天最多 6 次）。"""
    ctx = _Ctx(NetworkError("Bad Gateway"))
    t0 = 1_800_000_000
    for i in range(10):
        _run_at(monkeypatch, ctx, t0 + i * 120)
    assert notified == []


def test_bad_request_still_reported(notified):
    """BadRequest 是 NetworkError 的子类，但它是代码问题，必须照常上报。"""
    asyncio.run(botmod.on_error(None, _Ctx(BadRequest("Message is too long"))))
    assert notified and "机器人异常" in notified[0]


def test_job_network_error_still_reported(notified):
    """后台任务里的网络错误不是长轮询，不能被这条分支吞掉（静默失败最贵）。"""
    asyncio.run(botmod.on_error(None, _Ctx(NetworkError("Bad Gateway"), job=object())))
    assert notified and "机器人异常" in notified[0]
