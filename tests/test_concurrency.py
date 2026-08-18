"""一个慢操作不能把所有人堵住。

真机（2026-08-18 18:02→18:05）：他点了动量轮动回测，随后发 /menu **石沉大海**，
三分钟后再发一次还是没反应——看上去就是"机器人卡死了"。

真相不是卡死：PTB 默认串行处理更新，动量回测要逐个拉 24 个币的日线，
CoinGecko 强制 2 秒间隔还会 429 退避，整轮几分钟。这期间任何人发的任何消息
都在队列里排着。同一个根因也制造过「回调应答过期」那批报错。
"""
import asyncio
import inspect
import time

import pytest

from handlers import busy


def test_concurrent_updates_is_enabled():
    """没有这一条，任何一个慢 handler 都能让整个机器人看起来死掉。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert "concurrent_updates(" in src
    assert "concurrent_updates(32)" in src, "给个上限，别让狂点把任务数堆爆"


# ── 重活闸 ──────────────────────────────────────────────────
def test_second_run_is_refused_not_queued():
    """排队的表现和卡死一模一样——要直接告诉他上一次还在跑。"""
    async def go():
        async with busy.guard(1, "momentum") as first:
            assert first is True
            async with busy.guard(1, "momentum") as second:
                assert second is False
    asyncio.run(go())


def test_lock_is_released_after_the_run():
    async def go():
        async with busy.guard(1, "momentum"):
            pass
        async with busy.guard(1, "momentum") as again:
            assert again is True
    asyncio.run(go())


def test_lock_is_released_even_when_the_task_blows_up():
    """异常路径不释放的话，他这辈子都跑不了第二次。"""
    async def go():
        with pytest.raises(RuntimeError):
            async with busy.guard(1, "momentum"):
                raise RuntimeError("boom")
        async with busy.guard(1, "momentum") as again:
            assert again is True
    asyncio.run(go())


def test_different_users_do_not_block_each_other():
    async def go():
        async with busy.guard(1, "momentum"):
            async with busy.guard(2, "momentum") as other:
                assert other is True
    asyncio.run(go())


def test_different_tasks_do_not_block_each_other():
    async def go():
        async with busy.guard(1, "momentum"):
            async with busy.guard(1, "scan") as other:
                assert other is True
    asyncio.run(go())


def test_stale_lock_expires():
    """异常路径没走 finally 的话，别让它永远卡住这个用户。"""
    busy._running[("9", "momentum")] = time.time() - busy.STALE - 1
    assert busy.is_busy(9, "momentum") is False


def test_busy_text_says_how_long_and_not_to_repeat():
    busy._running[("8", "momentum")] = time.time() - 42
    t = busy.busy_text(8, "momentum", "动量回测")
    busy._running.pop(("8", "momentum"), None)
    assert "42" in t and "不用重复点" in t


# ── 接线 ────────────────────────────────────────────────────
def test_momentum_command_is_guarded():
    from handlers import strategy
    assert "busy.guard" in inspect.getsource(strategy.momentum)


def test_momentum_button_is_guarded():
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert "busy.guard" in src


def test_progress_message_tells_the_truth_about_the_wait():
    """原来写「约 30~60 秒」，实际要 1~3 分钟——低估等待时间会让他以为卡死了。"""
    from handlers import strategy
    src = inspect.getsource(strategy.momentum)
    assert "1~3 分钟" in src
    assert "其他功能照常能用" in src
