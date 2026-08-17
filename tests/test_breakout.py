"""5 分钟箱体破位扫描。

这个功能最容易做废的方式不是"漏报"，而是**刷屏**：条件一松，价格出箱之后
每根都命中，一天几十条推到群里，第 10 条的时候那个入场点早没了，
再往后整个功能就被无视。所以这里的测试有一半在守"什么时候**不**该报"。

阈值都是量出来的（23 个热门币 × 3.3 天 5m 历史逐根回放）：
  箱高≤4% 穿轴≥3 不要求放量 → 55.8 次/天
  箱高≤2.5% 穿轴≥5 必须放量 止损≤2.5% → 17.5 次/天，止损中位 1.33%
"""
import time

import pytest

from handlers import breakout as B


def bars(seq, vol=100.0, start=1_700_000_000_000, step=300_000):
    """[(o,h,l,c)] → K线行。"""
    return [[start + i * step, o, h, lo, c, vol]
            for i, (o, h, lo, c) in enumerate(seq)]


def box_bars(n=30, mid=100.0, half=0.5, vol=100.0):
    """造一段来回穿中轴的箱体。"""
    out = []
    for i in range(n):
        up = i % 2 == 0
        c = mid + (half if up else -half)
        o = mid - (half if up else -half)
        out.append((o, max(o, c) + 0.05, min(o, c) - 0.05, c))
    return bars(out, vol=vol)


def trend_bars(n=30, start=100.0, step=0.3):
    """单边斜坡——不该被当成箱体。"""
    out = []
    p = start
    for _ in range(n):
        o, c = p, p + step
        out.append((o, c + 0.05, o - 0.05, c))
        p = c
    return bars(out)


# ── 箱体识别 ─────────────────────────────────────────────────────
def test_oscillating_range_is_a_box():
    box = B.box_of(box_bars(30))
    assert box and box["crosses"] >= B.MIN_CROSSES
    assert B.BOX_MIN_PCT <= box["height_pct"] <= B.BOX_MAX_PCT


def test_a_slope_is_not_a_box():
    """BTC/ETH 那种缓慢漂移只穿中轴 0~2 次——是斜坡不是箱体，
    在斜坡上谈"突破"没有意义，也没有止损位可放。"""
    assert B.box_of(trend_bars(30)) is None


def test_too_wide_is_not_a_box():
    wide = box_bars(30, mid=100.0, half=5.0)      # 高度 10%，早就不是整理了
    assert B.box_of(wide) is None


def test_too_narrow_is_not_a_box():
    """窄到 0.1% 多半是没人交易的僵尸盘。"""
    assert B.box_of(box_bars(30, mid=100.0, half=0.02)) is None


# ── 破位判定 ─────────────────────────────────────────────────────
def _breakout_rows(direction=1, vol_x=2.0, gap=1.3):
    """箱体 + 一根破位K线；均线也顺势（靠箱体后半段推上去）。

    gap 的窗口很窄，两边都卡着真实的过滤条件：
      太小 → 收盘价够不到 BREAK_BUFFER 那条线，不算破位；
      太大 → 到箱体另一侧的止损距离超过 MAX_STOP_PCT(2.5%)，被赔率过滤拒掉。
    这个夹具里可用区间约 1.11~1.40，取中间的 1.3。
    """
    rows = box_bars(70, mid=100.0, half=0.5)
    # 让均线排好队：最后 12 根缓缓走出方向
    p = 100.0
    for i in range(12):
        p += 0.12 * direction
        c = p
        o = p - 0.1 * direction
        rows.append([rows[-1][0] + 300_000, o, max(o, c) + 0.02,
                     min(o, c) - 0.02, c, 100.0])
    last_close = (100.5 + gap) if direction > 0 else (99.5 - gap)
    o = rows[-1][4]
    rows.append([rows[-1][0] + 300_000, o, max(o, last_close),
                 min(o, last_close), last_close, 100.0 * vol_x])
    return rows


def test_upward_breakout_is_detected():
    sig = B.detect(_breakout_rows(1))
    assert sig and sig["direction"] == 1
    assert sig["stop"] < sig["close"]          # 多头止损在下方
    assert sig["volume_ok"]


def test_downward_breakdown_is_detected():
    sig = B.detect(_breakout_rows(-1))
    assert sig and sig["direction"] == -1
    assert sig["stop"] > sig["close"]


def test_inside_the_box_is_not_a_signal():
    rows = box_bars(70)
    assert B.detect(rows) is None


def test_touching_the_edge_is_not_a_breakout():
    """贴着边界收盘不算破位——差半个身位就报，假信号会成倍增加。"""
    sig = B.detect(_breakout_rows(1, gap=0.0))
    assert sig is None


# ── 三个条件缺一不可 ─────────────────────────────────────────────
def test_ma_must_agree_with_the_break():
    """均线还缠着的时候，破上破下都可能立刻被打回来——这是要滤掉的一半。"""
    rows = box_bars(70, mid=100.0, half=0.5)     # 全程震荡，均线必然缠绕
    rows.append([rows[-1][0] + 300_000, 100.0, 103.0, 100.0, 102.5, 300.0])
    assert B.detect(rows) is None


def test_low_volume_breakout_is_rejected():
    """缩量突破是假突破的高发区。"""
    assert B.detect(_breakout_rows(1, vol_x=0.5)) is None


def test_far_stop_is_rejected():
    """止损到箱体另一侧超过 2.5%，5 分钟级别赔率不够。"""
    sig = B.detect(_breakout_rows(1, gap=6.0))
    assert sig is None


# ── 只报"破出去那一根" ───────────────────────────────────────────
def test_only_the_breaking_bar_fires():
    """价格出箱后往往连着好几根都满足条件。不去重的话实测 62 次/天——
    刷屏，而且第 10 条提醒时入场点早没了。"""
    rows = _breakout_rows(1)
    assert B.detect(rows) is not None
    # 再加一根"还在外面"的
    c = rows[-1][4] + 0.1
    rows.append([rows[-1][0] + 300_000, rows[-1][4], c + 0.05, rows[-1][4] - 0.05,
                 c, 300.0])
    assert B.detect(rows) is None, "出箱后的第二根不该再报一次"


def test_fresh_check_is_not_tautological():
    """第一版判"上一根收在箱体内"——而箱体上下沿本来就是含上一根算出来的，
    永远成立，等于没判。改成"上一根还没触发"才有效。"""
    import inspect
    src = inspect.getsource(B.detect)
    assert "fresh_only" in src
    assert "同义反复" in (B.detect.__doc__ or "")


# ── 阈值不能被悄悄放松 ───────────────────────────────────────────
def test_thresholds_stay_selective():
    """这几个数是量出来的（见模块注释），放松任何一个都会让推送量翻倍。"""
    assert B.BOX_MAX_PCT <= 2.5
    assert B.MIN_CROSSES >= 5
    assert B.VOL_OK >= 1.2
    assert B.MAX_STOP_PCT <= 2.5


# ── 输出 ─────────────────────────────────────────────────────────
def test_render_gives_a_stop_not_a_prediction():
    """这功能报的是"能画出止损的形态"，不是"会涨"。"""
    sig = B.detect(_breakout_rows(1))
    txt = B.render("ETH", sig, "Bybit永续")
    assert "止损参考" in txt
    assert "箱体" in txt and "MA3" in txt
    assert "不构成投资建议" in txt


def test_render_marks_direction():
    up = B.render("X", B.detect(_breakout_rows(1)))
    dn = B.render("X", B.detect(_breakout_rows(-1)))
    assert "向上突破" in up and "向下跌破" in dn


def test_empty_result_explains_itself():
    """扫不出来是常态，要说清这不是故障。"""
    txt = B.render_list([], "Bybit永续")
    assert "没有符合" in txt and "正常" in txt


# ── 订阅：默认订阅不能变成"关不掉" ───────────────────────────────
@pytest.fixture(autouse=True)
def _clean():
    import storage
    for k in ("breakout_subs", "breakout_seeded", "breakout_seen"):
        storage.data[k] = {}
    yield
    for k in ("breakout_subs", "breakout_seeded", "breakout_seen"):
        storage.data[k] = {}


def test_seed_turns_it_on_by_default():
    assert B.seed_default(123) is True
    assert B.is_on(123)


def test_unsubscribing_survives_restart():
    """默认订阅和"关不掉"之间只隔着这个标记：种过一次就不再种。"""
    B.seed_default(123)
    B.toggle(123, False)
    assert B.seed_default(123) is False      # 重启再来一次
    assert not B.is_on(123)


def test_toggle_round_trip():
    B.toggle(7, True)
    assert B.is_on(7)
    B.toggle(7, False)
    assert not B.is_on(7)


def test_dedupe_blocks_repeat_within_cooldown():
    now = time.time()
    assert B._dedupe_ok(1, "ETHUSDT", 1, now)
    assert not B._dedupe_ok(1, "ETHUSDT", 1, now + 60)
    # 反方向是另一个事件
    assert B._dedupe_ok(1, "ETHUSDT", -1, now + 60)


def test_dedupe_expires():
    now = time.time()
    B._dedupe_ok(1, "ETHUSDT", 1, now)
    assert B._dedupe_ok(1, "ETHUSDT", 1, now + B.COOLDOWN + 1)


# ── 入口 ─────────────────────────────────────────────────────────
def test_command_registered():
    import inspect
    import bot
    assert 'CommandHandler("breakout"' in inspect.getsource(bot.main)


def test_job_scheduled():
    import inspect
    import bot
    assert "breakout.job" in inspect.getsource(bot.main)


def test_button_is_routed():
    import inspect
    from handlers import menu
    assert 'startswith("bo:")' in inspect.getsource(menu._dispatch)


def test_menu_entry_exists():
    from handlers import menu
    cbs = [b.callback_data for row in menu.CATS["cat_scan"][1] for b in row]
    assert any(c.startswith("bo:") for c in cbs), "新功能必须有按钮入口"


def test_seeded_at_startup():
    import inspect
    import bot
    assert "seed_all" in inspect.getsource(bot.post_init)
