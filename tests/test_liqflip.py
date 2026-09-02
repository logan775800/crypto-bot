"""爆仓一边倒告警：判据是回测出来的，测试要锁住"别把结论说过头"。

他要的是「5 分钟内一边爆仓 ≥80% → 摸顶抄底」。回测（26 币 / 7 天 /
5 万多个 5 分钟窗口）确认信号成立，但**两个方向的时间尺度不一样**，
而且摸顶那侧尾部风险大。这些限定条件一旦从卡片上掉了，
这个功能就从"有统计依据的提示"退化成"看着像"。
"""
import time

import pytest

from handlers import liqflip as L


@pytest.fixture(autouse=True)
def clean():
    import storage
    storage.data["liqflip"] = {}
    L._base.clear()
    yield
    L._base.clear()


# ── 判据 ────────────────────────────────────────────────────
def test_one_sided_short_liquidation_is_the_top_side():
    """命名按**被爆的是谁**，不是按建议做什么——后者会在代码里读反。"""
    assert L.classify(10, 95, cut=50) == ("short", pytest.approx(95 / 105))
    assert L.classify(95, 10, cut=50)[0] == "long"


def test_balanced_liquidation_is_not_a_signal():
    """两边都在爆 = 双向绞杀，不是一边倒。"""
    assert L.classify(50, 50, cut=10) is None
    assert L.classify(75, 25, cut=10) is None      # 75% 不到 80%


def test_below_the_size_gate_is_ignored():
    """占比再极端，金额太小也是噪音——小币随便一单就 100%。"""
    assert L.classify(0, 100, cut=1000) is None
    assert L.classify(0, 100, cut=50)[0] == "short"


def test_no_gate_no_call():
    """样本不足算不出分位时，这个币这轮直接跳过，不能当成 0 门槛全放行。"""
    assert L.classify(0, 100, cut=None) is None


def test_threshold_is_per_coin_not_a_fixed_dollar_amount():
    """$1 万爆仓对 BTC 是零头、对小币是天量。绝对金额换个币就得重调，
    而且永远调不对。"""
    vals = [i * 100.0 for i in range(1, 201)]
    assert L.percentile(vals, 0.95) == pytest.approx(19100, rel=0.05)
    assert L.percentile([1.0, 2.0], 0.95) is None, "样本太少还给分位数"


def test_levels_tighten_on_both_gates():
    """档位管两个数：相对分位 + 绝对下限，越严的档两个都更高。

    ⚠️ 这条原来断言的是「门槛越高信号越强」。**加了绝对闸之后那句话不成立了**
    ——实测三档胜率都在 65~68%，分位再往上提只降频率不提胜率。
    所以档位的语义从"提高准确度"变成了"控频率"，断言也跟着改成
    只锁单调性，不再暗示胜率。
    """
    qs = [L.LEVELS[k][0] for k in ("宽", "标准", "严")]
    fs = [L.LEVELS[k][1] for k in ("宽", "标准", "严")]
    assert qs[0] < qs[1] < qs[2]
    assert fs[0] < fs[1] < fs[2]
    assert L.LEVELS[L.DEFAULT_LEVEL] == (0.95, 30_000)


def test_absolute_floor_kills_the_zero_edge_zone():
    """他实际收到「爆仓 5106 U，100% 是空头」之后骂的那件事。
    实测 **$1 万以下抄底胜率 49.5%，基线 49.6%——零边际**，
    而 32 个币里 27 个的 90 分位低于 2 万美元，绝大多数命中落在那里。
    所以最宽的那档也不能让 $1 万以下的进来。"""
    assert min(f for _q, f in L.LEVELS.values()) >= 10_000


# ── 卡片：限定条件不能掉 ────────────────────────────────────
@pytest.mark.parametrize("q", sorted(v[0] for v in L.LEVELS.values()))
def test_every_level_has_backtested_numbers(q):
    """卡片上印的胜率必须每档都有实测值。缺一档就会退回默认值 0，
    卡片上会出现「上涨概率 0%」这种明显错误的话。"""
    assert q in L.STATS["long"], f"{q} 档缺多头侧回测数据"
    assert q in L.STATS["short"], f"{q} 档缺空头侧回测数据"


def test_long_side_card_says_one_hour():
    """抄底的边际**只在 1 小时尺度上成立**，4 小时就没了。
    不写时间尺度，人会拿它当趋势信号拿几天。"""
    t = L.format_hit("SOL", "long", 0.93, 412_000, 180_000, 128.4, 0.95)
    assert "1 小时" in t
    assert "4 小时就没了" in t
    assert "66%" in t and "基线" in t


def test_short_side_card_says_four_hours_and_the_tail_risk():
    """摸顶要等 4 小时才显著；而且中位是跌的、均值却经常是涨的——
    大多数时候回落，偶尔被轧到天上。**胜率高不等于赔率好**，
    这句不能省，省了就是在鼓励人按胜率上仓位。"""
    t = L.format_hit("BTR", "short", 0.87, 96_000, 41_000, 0.2091, 0.95)
    assert "4 小时" in t
    assert "尾部风险" in t and "胜率高不等于赔率好" in t
    assert "15 分钟和 1 小时都很弱" in t


def test_card_shows_the_sample_size_and_the_baseline():
    """只给"68%"没有基线，读的人不知道这算不算高（基线就有 49.6%）。"""
    t = L.format_hit("SOL", "long", 0.93, 412_000, 180_000, 128.4, 0.95)
    assert "样本 125" in t
    assert "基线" in t


def test_card_never_promises():
    t = L.format_hit("SOL", "long", 0.93, 412_000, 180_000, 128.4, 0.95)
    assert "统计边际不是保证" in t


def test_panel_admits_leverage_cannot_be_filtered():
    """他要的是「5 倍以上杠杆清算」。**爆仓数据里没有杠杆倍数**，
    筛不了——这件事必须直说，不能假装筛了。"""
    t = L.panel_text(-100)
    assert "没有杠杆倍数" in t
    assert "筛不了" in t


def test_panel_shows_the_backtest_table():
    t = L.panel_text(-100)
    assert "68.0%" in t and "33.3%" in t   # 宽档抄底 / 标准档摸顶
    assert "基线" in t
    assert "绝对下限" in t, "没写清第二道闸"


# ── 去重与闸门 ──────────────────────────────────────────────
def test_same_bar_is_only_judged_once():
    """扫描 5 分钟一轮、K 线也是 5 分钟，错位时会连着两轮看到同一根。"""
    assert not L.seen_bar("BTC:long", 1000)
    assert L.seen_bar("BTC:long", 1000)
    assert not L.seen_bar("BTC:long", 1300)


def test_cooldown_is_per_coin_and_per_side():
    """同一个币的多头侧和空头侧是两个事件，冷却不能互相压。"""
    L.mark("BTC", "long")
    assert L.cooled("BTC", "long")
    assert not L.cooled("BTC", "short")
    assert not L.cooled("ETH", "long")


def test_cooldown_expires():
    t = time.time()
    L.mark("BTC", "long", now=t - L.COOLDOWN - 10)
    assert not L.cooled("BTC", "long", now=t)


def test_quota_does_not_reset_itself_after_counting():
    """hot 和梗爆发都踩过的同一个坑：记数不滚小时的话，
    先记后查会把刚记的抹掉，兜底闸等于没有。"""
    L._used(L.PER_HOUR)
    assert L.quota_left() == 0


def test_quota_rolls_over():
    import storage
    L._used(1)
    assert L.quota_left() == L.PER_HOUR - 1
    storage.data["liqflip"]["hour"] = 0
    assert L.quota_left() == L.PER_HOUR


# ── 取数口径 ────────────────────────────────────────────────
def test_uses_the_closed_bar_not_the_forming_one():
    """最后那根多半还没收盘，爆仓额只累积了一部分，
    拿它比门槛会系统性偏低（v1.33.1 量比那次同一个坑）。"""
    import inspect
    src = inspect.getsource(L.scan_once)
    assert "rows[-2]" in src
    assert "还没收盘" in src


def test_universe_is_ranked_the_same_way_the_backtest_was():
    """**验的池子和跑的池子必须是同一批。** 第一版按 Gate 自己的成交额取，
    而回测用的是币安成交额前 30——Gate 的量高度集中，构成完全不同，
    那份 68%/34% 的胜率就不能直接往上套。"""
    import inspect
    src = inspect.getsource(L.universe)
    assert "fapi.binance.com" in src
    assert "quoteVolume" in src


def test_universe_excludes_tokenized_stocks():
    """同一个坑第四次了（微市值榜、涨跌榜、多空比榜各踩过一次）。"""
    import inspect
    src = inspect.getsource(L.universe)
    assert "noncrypto_bases" in src and "_BSTOCK" in src


def test_universe_skips_coins_gate_does_not_have():
    """爆仓数据只有 Gate 给，它没有的币扫了也白扫。"""
    import inspect
    assert "b not in gate" in inspect.getsource(L.universe)


# ── 接线 ────────────────────────────────────────────────────
def test_toggle_and_level():
    assert not L.is_on(-100)
    L.toggle(-100, True)
    assert L.is_on(-100)
    assert L.set_level("严")[1:] == L.LEVELS["严"]
    assert L.set_level("没这个档") is None
    L.toggle(-100, False)
    assert not L.is_on(-100)


def test_button_entry_exists():
    from handlers import menu
    cbs = [b.callback_data for row in menu.notify_kb(-100).inline_keyboard
           for b in row]
    assert "lf:panel" in cbs
    import inspect
    assert 'd.startswith("lf:")' in inspect.getsource(menu._dispatch)


def test_command_and_job_registered():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("liqflip", liqflip.liqflip_cmd)' in src
    assert "liqflip.scan" in src
    assert 'BotCommand("liqflip"' in src


def test_job_interval_matches_the_bar_it_judges():
    """判的是 5 分钟 K 线，扫描间隔比它长就会漏根。"""
    import pathlib
    import re
    src = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "bot.py").read_text(encoding="utf-8")
    seg = src.split("liqflip.scan")[1][:200]
    m = re.search(r"interval=(\d+)", seg)
    assert m and int(m.group(1)) <= 300, "扫描比 5 分钟慢，会漏掉整根"
