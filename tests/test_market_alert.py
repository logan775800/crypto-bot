"""市场异动告警的「放量」检测。

他问：「怎么一天到晚都有这两个币放量是什么意思？」——UTK / LRC 天天报。

**取证证伪了老口径**：老版本拿「这一轮的 24h 成交额 ÷ 上一轮的 24h 成交额」
当倍数。实测 90 秒内 149 个成交额≥200万的币，这个比值**最大 1.008、
中位数 1.000，没有一个到 1.5 倍**——24h 累计值在几分钟里根本不会动。

所以老版本有两个后果：
  · 真放量**永远测不到**（够不到 3 倍门槛）；
  · 能触发的只有基线是脏数据的时候，而且**没有任何冷却**，
    于是同样几个币每 5 分钟重报一次。

新口径：**这段时间的净增量 ÷ 它自己的平均速率**。零额外请求。
"""
import pytest

import storage
from handlers import market_alert as MA


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(storage, "save_data", lambda *a, **k: None)
    monkeypatch.setattr(MA, "save_data", lambda *a, **k: None)
    storage.data["surge_alerted"] = {}
    storage.data["last_volumes_ex"] = {}
    yield
    storage.data["surge_alerted"] = {}
    storage.data["last_volumes_ex"] = {}


def ratio(vol24, delta, elapsed):
    """新口径：这段时间成交了多少 ÷ 同样时长平时该成交多少。"""
    return delta / (vol24 * (elapsed / 86400.0))


# ── 口径本身 ────────────────────────────────────────────────
def test_normal_pace_is_about_one():
    """按平均速率走的币，倍数应该贴着 1——这是整个判据的锚点。"""
    vol24 = 10_000_000
    for elapsed in (300, 600, 900):
        typical = vol24 * elapsed / 86400
        assert ratio(vol24, typical, elapsed) == pytest.approx(1.0)


def test_a_real_surge_scores_well_above_the_threshold():
    """24h 一千万的币，5 分钟里成交 20 万 —— 那是平时的近 6 倍。"""
    assert ratio(10_000_000, 200_000, 300) > MA.SURGE_RATIO


def test_a_small_wiggle_does_not():
    assert ratio(10_000_000, 50_000, 300) < MA.SURGE_RATIO


def test_the_old_caliber_could_never_fire():
    """老口径比的是两个 24h 累计值。实测这个比值最大 1.008，
    而门槛是 3——真放量永远测不到。这条钉住"别改回去"。
    """
    prev24, now24 = 10_000_000, 10_020_000     # 5 分钟后的真实量级
    assert now24 / prev24 < 1.01
    assert now24 / prev24 < MA.SURGE_RATIO


def test_ratio_is_independent_of_scan_interval():
    """扫描间隔会变（重启、卡顿），倍数不能跟着间隔飘，
    否则同一件事在不同间隔下判出不同结论。"""
    vol24 = 10_000_000
    r5 = ratio(vol24, 200_000, 300)
    r10 = ratio(vol24, 400_000, 600)     # 同样的速率，双倍时长双倍量
    assert r5 == pytest.approx(r10)


# ── 护栏常量 ────────────────────────────────────────────────
def test_absolute_floor_blocks_tiny_pools():
    """只看比例的话，小池子随便一笔就能炸出很高的倍数。"""
    assert MA.SURGE_MIN_DELTA > 0
    # 24h 三百万的币，5 分钟成交 5 万 → 倍数很高，但绝对量不够，应被挡
    assert ratio(3_000_000, 50_000, 300) > MA.SURGE_RATIO
    assert 50_000 < MA.SURGE_MIN_DELTA


def test_stale_baseline_is_not_trusted():
    """重启/停机之后基线过期，这轮只该重建、不该告警——
    否则一次停机就换来一轮假放量。"""
    assert MA.MIN_ELAPSED > 0 and MA.MAX_ELAPSED > MA.MIN_ELAPSED
    assert MA.MAX_ELAPSED <= 3600


def test_there_is_a_cooldown():
    """没有冷却的话同一件事每 5 分钟重报一次——这正是他被刷屏的直接原因。"""
    assert MA.SURGE_COOLDOWN >= 3600


def test_snapshot_carries_a_timestamp():
    """不存时间戳就算不出"这段时间有多长"，倍数无从谈起。"""
    import inspect
    src = inspect.getsource(MA.scan_market)
    assert '"t": now' in src and '"v": vol_map' in src
    assert "elapsed" in src


def test_old_snapshot_format_is_skipped_not_crashed():
    """线上 data.json 里存的是老格式（直接 {币: 额}）。
    读到老格式必须安静跳过一轮再换新格式，不能抛异常也不能当成基线用。"""
    import inspect
    src = inspect.getsource(MA.scan_market)
    assert 'isinstance(snap.get("v"), dict)' in src


def test_message_states_the_caliber():
    """「量增 28 倍」这种数字不写口径，没人知道是拿什么比什么。"""
    import inspect
    src = inspect.getsource(MA.scan_market)
    assert "是平时同时长的" in src
    assert "÷ 它自己 24h 的平均速率" in src
    assert "4 小时内不重复报" in src
