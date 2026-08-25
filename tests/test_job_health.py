"""后台任务心跳 + 数据源探测。

**告警任务挂掉的表现是「什么都没发生」**——和「这段时间市场没异动」在屏幕上
完全一样。这个项目最贵的一类 bug 就长这样：不报错、日志干净、只是某件事
永远不发生。数据源探测管不到这一层（源好好的，任务自己抛异常也一样静默）。

另一条：数据源探测原来只看 CoinGecko 和 OKX，而 v1.46.0 之后**币安才是主源**
（急涨急跌、清算地图、K线、多日涨跌榜都币安优先）。最要紧的那家反而没人看着。
"""
import asyncio
import types

import pytest

from handlers import monitor as M


@pytest.fixture(autouse=True)
def _clean():
    M._BEATS.clear()
    M._job_alerted.clear()
    yield
    M._BEATS.clear()
    M._job_alerted.clear()


class FakeBot:
    """conftest 配了两个管理员（111,222），所以每条告警会发两遍——
    这是对的行为。测试按**去重后的内容**数，别数发送次数。"""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)

    @property
    def msgs(self):
        out = []
        for t in self.sent:
            if t not in out:
                out.append(t)
        return out


def _ctx():
    return types.SimpleNamespace(bot=FakeBot())


def _run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


# ── 探测覆盖 ──────────────────────────────────────────────────
def test_binance_is_probed():
    """v1.46.0 起币安是主源，它挂了大半功能会哑。补上之前根本没探。"""
    import inspect
    src = inspect.getsource(M.health_check)
    assert "api.binance.com" in src, "主源没被探测"
    assert "api.bybit.com" in src


def test_every_probed_source_has_an_impact_note():
    """「部分功能可能受影响」等于没说——他既不知道该停手还是照用，
    也不知道该验证什么。每个源都要写清具体影响哪些功能。"""
    for key in M._health:
        assert key in M.IMPACT, f"{key} 挂了之后影响什么，没人写"
        assert len(M.IMPACT[key]) > 10


# ── 心跳登记 ──────────────────────────────────────────────────
def test_tracked_records_success():
    async def _ok(ctx):
        return None
    _run(M.tracked(_ok, "测试任务", 60)(_ctx()))
    assert M._BEATS["测试任务"]["fails"] == 0
    assert M._BEATS["测试任务"]["last_ok"] > 0


def test_tracked_swallows_the_exception():
    """异常不能往上抛：抛出去只是框架记一条日志，任务下一轮照跑，
    而没有任何人知道出过事。既然登记进心跳了，watchdog 会报。"""
    async def _boom(ctx):
        raise RuntimeError("接口挂了")
    _run(M.tracked(_boom, "坏任务", 60)(_ctx()))       # 不该抛出来
    r = M._BEATS["坏任务"]
    assert r["fails"] == 1
    assert "接口挂了" in r["err"]
    assert r["last_ok"] == 0


def test_success_resets_the_failure_streak():
    async def _boom(ctx):
        raise RuntimeError("x")

    async def _ok(ctx):
        return None
    job_bad = M.tracked(_boom, "任务", 60)
    _run(job_bad(_ctx()))
    _run(job_bad(_ctx()))
    assert M._BEATS["任务"]["fails"] == 2
    _run(M.tracked(_ok, "任务", 60)(_ctx()))
    assert M._BEATS["任务"]["fails"] == 0


# ── 判定 ──────────────────────────────────────────────────────
def test_one_failure_is_not_worth_alerting():
    """一次网络抖动就报的话，人很快会开始忽略告警——那比不报还糟。"""
    M.beat("任务", False, 60, "抖了一下")
    bad, _ = M.job_health()
    assert bad == []


def test_a_failure_streak_is_reported():
    for _ in range(M.JOB_FAIL_STREAK):
        M.beat("任务", False, 60, "接口 500")
    bad, _ = M.job_health()
    assert len(bad) == 1
    assert "接口 500" in bad[0][1]


def test_a_job_that_stopped_running_is_reported():
    """连续失败之外的另一种死法：压根没被调度到。
    这种更隐蔽——没有异常、没有日志，就是不跑了。"""
    import time
    M.beat("任务", True, 60)
    M._BEATS["任务"]["last_ok"] = time.time() - 60 * 10   # 10 分钟没成功过
    bad, _ = M.job_health()
    assert len(bad) == 1
    assert "没成功跑完" in bad[0][1]


def test_a_healthy_job_is_quiet():
    M.beat("任务", True, 60)
    assert M.job_health()[0] == []


def test_a_never_run_job_is_not_reported_as_stalled():
    """刚启动、还没轮到它跑的任务不算故障（first= 有几分钟延迟是正常的）。"""
    M.beat("任务", False, 60, "第一次就失败")
    bad, _ = M.job_health()
    assert bad == [], "只失败一次不该报，也不该被当成停摆"


# ── 推送 ──────────────────────────────────────────────────────
def test_watchdog_alerts_once_not_every_round():
    """15 分钟一轮，不去重就是每 15 分钟同一条——告警刷屏比不告警更糟。"""
    for _ in range(M.JOB_FAIL_STREAK):
        M.beat("急涨急跌", False, 60, "boom")
    ctx = _ctx()
    _run(M.job_watchdog(ctx))
    _run(M.job_watchdog(ctx))
    assert len(ctx.bot.msgs) == 1


def test_watchdog_says_what_this_means():
    """光说"任务异常"没用，要点破它的表现形式，否则他不会当回事。"""
    for _ in range(M.JOB_FAIL_STREAK):
        M.beat("急涨急跌", False, 60, "boom")
    ctx = _ctx()
    _run(M.job_watchdog(ctx))
    assert "静默" in ctx.bot.msgs[0] or "什么都没发生" in ctx.bot.msgs[0]
    assert "急涨急跌" in ctx.bot.msgs[0]


def test_recovery_is_reported_too():
    """只报坏不报好，他会一直不确定现在到底恢复没有。"""
    for _ in range(M.JOB_FAIL_STREAK):
        M.beat("急涨急跌", False, 60, "boom")
    ctx = _ctx()
    _run(M.job_watchdog(ctx))
    M.beat("急涨急跌", True, 60)
    _run(M.job_watchdog(ctx))
    assert len(ctx.bot.msgs) == 2
    assert "恢复" in ctx.bot.msgs[1]


# ── 接线 ──────────────────────────────────────────────────────
def test_the_jobs_that_matter_are_tracked():
    """**沉默=直接损失**的那批必须挂心跳。

    收集范围决定了谁会被看着——漏掉一个，那条告警链就还是"静默失效"。
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    for name in ("价格预警", "波动监控", "急涨急跌", "合约异动", "市场异动",
                 "条件提醒", "风险守护", "实盘爆仓预警", "交易计划", "LP撤出告警"):
        assert f'"{name}"' in src, f"{name} 这条告警链没挂心跳"
    assert "monitor.job_watchdog" in src, "巡检任务没注册，心跳记了也没人看"
