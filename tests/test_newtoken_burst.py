"""梗爆发：判据是「同名被抄的速度」，不是「池子够大」。

这一路和另外两路的区别就在这里——它对流动性完全不设限，
一个 500 美元的池子照样算一次"有人在抄"。所以护栏必须换一套：
时间跨度、垃圾名、配额。这个文件逐条验。
"""
import time

import pytest

from handlers import newtoken as N


@pytest.fixture(autouse=True)
def clean():
    import storage
    storage.data["newtoken"] = {}
    N._burst.clear()
    yield
    N._burst.clear()


def pool(name, addr, liq=5000):
    return {"name": f"{name} / USDT 0.25%", "pool_address": addr,
            "reserve_in_usd": str(liq),
            "transactions": {"h1": {"buys": 10, "sells": 5}}}


def feed(name, n, addr_prefix="p", start=0.0, step=60.0, liq=5000, now=None):
    """同一个名字喂 n 个池子，彼此隔 step 秒。"""
    base = now or time.time()
    for i in range(n):
        N.burst_track([pool(name, f"{addr_prefix}{i}", liq)], now=base + start + i * step)
    return base


# ── 核心判据 ────────────────────────────────────────────────
def test_a_meme_copied_enough_times_fires():
    """实测「甜甜币」8 分钟被抄 7 次，「牛来」只有 1 次——
    前者此刻在热，后者热度是几天前的。这就是要抓的那个差别。"""
    t = time.time()
    feed("甜甜币", 5, now=t)
    hits = N.burst_hits(now=t + 300)
    assert hits and hits[0][0] == "甜甜币"
    assert hits[0][1] == 5


def test_one_pool_is_not_a_trend():
    feed("牛来", 1)
    assert N.burst_hits() == []


def test_liquidity_is_deliberately_ignored():
    """和另外两路最大的区别：抄的次数才是信号，池子大小不是。
    一个 500 美元的池子照样算一次「有人愿意花钱抄」——
    这正是它能比「等池子做大」早的原因。"""
    t = time.time()
    feed("馍馍", 5, liq=500, now=t)
    assert N.burst_hits(now=t + 300)


# ── 护栏一：同一秒批量建池不算热度 ─────────────────────────────
def test_instant_spam_is_not_a_burst():
    """实测抓到过 `joe 3 次 / 0.0 分钟`——一个人同一秒建三个池，
    那是垃圾不是热度。"""
    t = time.time()
    feed("刷子币", 8, step=1.0, now=t)          # 8 个池子挤在 8 秒里
    assert N.burst_hits(now=t + 60) == [], "同一秒批量建池被当成了爆发"


def test_spread_out_copies_do_count():
    t = time.time()
    feed("慢慢抄", 5, step=120.0, now=t)
    assert N.burst_hits(now=t + 700)


# ── 护栏二：垃圾名 ──────────────────────────────────────────
@pytest.mark.parametrize("junk", ["Solana", "solana", "SOL", "usdt", "BNB", "x", "ab", ""])
def test_chain_and_ticker_names_are_not_memes(junk):
    """实测一轮里「Solana」和「solana」各被用了 5 次。
    链名币名天天有人拿来当代币名，那不是梗。"""
    assert N.is_junk_name(junk)


# 单个汉字是正常梗币名，单个字母不是——这两条一起验，防止再被一刀切
@pytest.mark.parametrize("real", ["甜甜币", "馍馍", "我的女友景甜", "MIRA",
                                  "Owl Byte", "猫", "牛"])
def test_real_names_pass(real):
    assert not N.is_junk_name(real)


def test_junk_never_enters_the_window():
    feed("solana", 10)
    assert "solana" not in N._burst
    assert N.burst_hits() == []


# ── 中英文两套门槛 ──────────────────────────────────────────
def test_english_needs_a_much_bigger_burst():
    """实测：同名≥5 次时全部名字 24.6 个/小时，中文名只有 4.1 个/小时——
    英文那一列基本全是噪音。所以英文要 level×3 才算数。
    这样极端爆发（那一轮 MIRA 被抄 23 次）仍然进得来。"""
    t = time.time()
    _lv, need = N.burst_level()
    feed("EnglishMeme", need, addr_prefix="e", now=t)
    feed("中文梗", need, addr_prefix="c", now=t)
    names = [h[0] for h in N.burst_hits(now=t + need * 60)]
    assert "中文梗" in names
    assert "EnglishMeme" not in names, "英文名用了中文的门槛"

    N._burst.clear()
    feed("MIRA", need * N.BURST_EN_FACTOR, addr_prefix="m", now=t)
    assert "MIRA" in [h[0] for h in N.burst_hits(now=t + 3000)]


# ── 滑动窗口 ────────────────────────────────────────────────
def test_old_copies_fall_out_of_the_window():
    """30 分钟前的抄袭不该算进「正在热」——不然一个梗热过一次就永远在榜上。"""
    t = time.time()
    feed("过气梗", 6, step=30.0, now=t - N.BURST_WINDOW - 600)
    N.burst_track([], now=t)            # 触发过期清理
    assert N.burst_hits(now=t) == []
    assert "过气梗" not in N._burst


def test_the_same_pool_is_only_counted_once():
    """同一个池子每轮扫描都会再看到一次。按地址去重，否则
    一个池子挂在那儿五轮就自己变成「被抄 5 次」。"""
    t = time.time()
    for i in range(6):
        N.burst_track([pool("单个池", "same-addr")], now=t + i * 60)
    assert len(N._burst.get("单个池", {})) == 1
    assert N.burst_hits(now=t + 400) == []


def test_window_is_not_persisted():
    """滑动窗口每 5 分钟糊几百条进 data.json 不值当。
    重启后 30 分钟自愈，期间只会漏报不会误报。"""
    import storage
    feed("内存梗", 5)
    assert "burst_window" not in (storage.data.get("newtoken") or {})


# ── 配额和冷却 ──────────────────────────────────────────────
def test_quota_is_separate_from_hot():
    """两类告警共用一个配额的话，池子多的那类会把另一类饿死。"""
    N._hot_used(N.HOT_PER_HOUR)
    assert N.hot_quota_left() == 0
    assert N.burst_quota_left() == N.BURST_PER_HOUR


def test_quota_counts_down_and_rolls_over():
    assert N.burst_quota_left() == N.BURST_PER_HOUR
    N._burst_used(1)
    assert N.burst_quota_left() == N.BURST_PER_HOUR - 1
    import storage
    storage.data["newtoken"]["burst_hour"] = 0     # 假装到了下一小时
    assert N.burst_quota_left() == N.BURST_PER_HOUR


def test_used_then_checked_does_not_reset_itself():
    """hot 那边踩过的同一个坑：记数不滚小时的话，
    先记后查会把刚记的抹掉，兜底闸等于没有。"""
    N._burst_used(N.BURST_PER_HOUR)
    assert N.burst_quota_left() == 0


def test_cooldown_survives_restart():
    """冷却**要落盘**：重启后不该把半小时前报过的梗再报一遍。
    （窗口不落盘、冷却落盘，是两个相反的决定，各有理由。）"""
    import storage
    N.burst_mark("甜甜币")
    assert N.burst_cooled("甜甜币")
    assert "甜甜币" in storage.data["newtoken"]["burst_sent_at"]
    assert not N.burst_cooled("没报过的")


def test_cooldown_expires():
    t = time.time()
    N.burst_mark("旧梗", now=t - N.BURST_COOLDOWN - 10)
    assert not N.burst_cooled("旧梗", now=t)


# ── 卡片 ────────────────────────────────────────────────────
def test_card_leads_with_the_speed_not_the_pool():
    """推的是**梗**不是池子。第一眼要看到"多久被抄了几次"。"""
    t = time.time()
    feed("甜甜币", 7, step=70.0, now=t)
    nm, n, span, pools = N.burst_hits(now=t + 600)[0]
    txt = N.format_burst(nm, n, span, pools, "BSC", "")
    assert "甜甜币" in txt
    assert "被抄了 7 次" in txt
    assert "分钟内" in txt


def test_card_says_which_pool_it_is_showing_and_lists_the_others():
    """同名一堆，看的人必须知道手里这条是哪一个。"""
    t = time.time()
    for i, liq in enumerate((900, 50000, 3000, 1200, 800)):
        N.burst_track([pool("馍馍", f"a{i}", liq)], now=t + i * 100)
    nm, n, span, pools = N.burst_hits(now=t + 500)[0]
    txt = N.format_burst(nm, n, span, pools, "BSC", "")
    assert "50,000" in txt, "没把流动性最大的那个排在前面"
    assert "其余的" in txt


def test_card_refuses_to_claim_potential():
    """他问过「你是怎么筛选有潜力的池子」。答案是：不筛。
    这里给的是"有人在抄"，不是"会不会涨"——这句必须印在卡片上。"""
    t = time.time()
    feed("梗", 5, now=t)
    nm, n, span, pools = N.burst_hits(now=t + 400)[0]
    txt = N.format_burst(nm, n, span, pools, "BSC", "")
    assert "合约地址" in txt
    assert "涨" in txt


def test_card_reports_what_the_quota_dropped():
    """"这一小时只有一个梗"和"还有三个没推给你"在屏幕上长得一模一样。
    挡掉的必须报数——这是那份工程教训里"静默失败最贵"的同一条。"""
    t = time.time()
    feed("梗", 5, now=t)
    nm, n, span, pools = N.burst_hits(now=t + 400)[0]
    txt = N.format_burst(nm, n, span, pools, "BSC", "", dropped=3)
    assert "还有 3 个" in txt


# ── 接线 ────────────────────────────────────────────────────
def test_scan_feeds_the_window_regardless_of_the_other_filters():
    """窗口要吃**全部**新池，不能被普通模式那些门槛先筛一道——
    梗爆发对流动性不设限，被筛过的数据算出来的速度是错的。"""
    import inspect
    src = inspect.getsource(N.scan)
    i_track = src.index("burst_track(pools)")
    i_pass = src.index("passes(a,")
    assert i_track < i_pass, "burst_track 排在筛选之后了，计数会偏低"


def test_scan_pushes_bursts():
    import inspect
    src = inspect.getsource(N.scan)
    assert "format_burst" in src and "burst_hits()" in src


def test_toggle_and_level():
    assert not N.burst_enabled(-100)
    N.toggle_burst(-100, True)
    assert N.burst_enabled(-100)
    assert N.set_burst_level("严")[1] == N.BURST_LEVELS["严"]
    assert N.set_burst_level("不存在的档") is None
    N.toggle_burst(-100, False)
    assert not N.burst_enabled(-100)


def test_off_kills_all_three_paths():
    """`/newtoken off` 说的是"关闭"，那就得真的全关。
    漏掉一路的话，他关了还在收，会以为开关坏了。"""
    import inspect
    src = inspect.getsource(N.newtoken_cmd)
    seg = src.split('"off"')[1].split("if args")[0]
    assert "toggle_hot(chat_id, False)" in seg
    assert "toggle_burst(chat_id, False)" in seg


def test_button_entry_exists():
    """他的规矩：功能必须有按钮入口。"""
    from handlers import menu
    cbs = [b.callback_data for row in menu.notify_kb(-100).inline_keyboard
           for b in row]
    assert "nt:burst" in cbs
    import inspect
    assert 'd == "nt:burst"' in inspect.getsource(menu._dispatch)


# ── 自检 ────────────────────────────────────────────────────
def test_selftest_distinguishes_empty_window_from_broken_scan():
    """低频告警不配自检 = 让人靠猜。「没有梗在爆」和「扫描挂了」
    在屏幕上一模一样，所以空窗口时必须给出下一步怎么查。"""
    import asyncio
    txt = asyncio.new_event_loop().run_until_complete(N.burst_selftest())
    assert "datacheck" in txt or "心跳" in txt


def test_selftest_shows_how_long_the_window_has_been_filling():
    """窗口在内存里，重启后要攒 30 分钟。不印这个，
    刚重启看到空的会以为坏了。"""
    import asyncio
    t = time.time()
    feed("攒着的", 2, now=t - 600)
    txt = asyncio.new_event_loop().run_until_complete(N.burst_selftest())
    assert "攒了" in txt


def test_selftest_shows_near_misses_with_their_thresholds():
    """没到线的时候要能看出"差多少"，否则调档全靠蒙。"""
    import asyncio
    t = time.time()
    feed("差一点", 2, now=t)
    txt = asyncio.new_event_loop().run_until_complete(N.burst_selftest())
    assert "差一点" in txt
    assert "次）" in txt


# ── 覆盖率：翻页够不够 ───────────────────────────────────────
def test_pages_cover_the_measured_pool_rate():
    """实测每 10 分钟：Solana 102 个新池、BSC 79、Base 72、以太坊 52。
    原来是「10 分钟一轮、每链翻 3 页(60 个)」——Solana 有四成从没被看见过。
    而这种漏是**静默的**：计数偏低，告警只是"没响"。

    这条锁的是「翻页量 × 轮次频率 ≥ 实测出池速度」这个不变式。
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("bot.py").read_text(
        encoding="utf-8")
    seg = src.split("newtoken.scan")[1][:200]
    import re
    m = re.search(r"interval=(\d+)", seg)
    assert m, "找不到扫描间隔"
    interval_min = int(m.group(1)) / 60
    fastest_per_10min = 102          # Solana，实测值
    need = fastest_per_10min / 10 * interval_min
    assert N.PAGES * 20 >= need * 1.3, (
        f"每轮翻 {N.PAGES * 20} 个，但 {interval_min:g} 分钟里最快的链会出 "
        f"{need:.0f} 个——会静默丢池子")


def test_span_uses_onchain_creation_time_not_first_sight():
    """刚重启时窗口里的存量池子会被打上同一个"首见时间"，跨度≈0，
    整批被"同一秒批量建池"那道护栏毙掉——真机第一跑就撞上了：
    自检显示「甜甜币被抄 16 次」，却一条都没到线。

    池子自带 `pool_created_at`，用它算跨度才是真的。
    """
    t = time.time()
    _lv, need = N.burst_level()
    from datetime import datetime, timezone
    for i in range(need):
        p = pool("甜甜币", f"z{i}")
        # 链上是隔 5 分钟陆续建的（都在 30 分钟窗口内），
        # 但我是重启后同一秒里一次性全看到的
        p["pool_created_at"] = datetime.fromtimestamp(
            t - 1500 + i * 300, timezone.utc).isoformat().replace("+00:00", "Z")
        N.burst_track([p], now=t)
    hits = N.burst_hits(now=t)
    assert hits, "存量池子被首见时间戳压成了 0 跨度"
    assert hits[0][2] >= N.BURST_MIN_SPAN


def test_falls_back_to_first_sight_when_no_creation_time():
    """字段缺了不能炸，退回首见时间即可（代价见上一条，所以只是兜底）。"""
    t = time.time()
    p = pool("无时间", "nt1")
    p.pop("pool_created_at", None)
    N.burst_track([p], now=t)
    assert abs(N._burst["无时间"]["nt1"][0] - t) < 2
