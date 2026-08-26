"""LP 撤出告警：有人从池子里抽水就推。

这个模块只有一个真正的技术难点，测试也主要在钉它：

    池子的美元流动性**本来就跟着币价走**。恒定乘积做市 x·y=k 下 V ∝ √p，
    所以币价跌 75%、没有任何人撤资，美元流动性也会自然掉 50%。

直接拿"流动性掉了多少"当判据，等于每次砸盘都报一次跑路。而砸盘恰恰是最常发生的，
那样这个告警会立刻变成噪音 → 被无视 → 真跑路那次也一起被无视。
`test_price_crash_alone_is_not_a_rug` 就是钉这条的。
"""
import time

import pytest

import storage
from handlers import rugwatch as R


@pytest.fixture(autouse=True)
def _clean():
    # 碰全局 data 的测试必须**重置**用到的键（服务器上 pytest 是随机顺序跑的）
    storage.data["rugwatch"] = {}
    storage.data["watchpct"] = []      # 是 list 不是 dict，见下面收集范围那一节
    yield
    storage.data["rugwatch"] = {}
    storage.data["watchpct"] = []      # 是 list 不是 dict，见下面收集范围那一节


# ── 价格修正：这个模块的立身之本 ──────────────────────────────
def test_expected_liquidity_scales_with_square_root_of_price():
    """V ∝ √p。价格变成 1/4，池子该剩一半。"""
    assert R.expected_liq(100_000, 1.0, 0.25) == pytest.approx(50_000)
    assert R.expected_liq(100_000, 1.0, 4.0) == pytest.approx(200_000)
    assert R.expected_liq(100_000, 1.0, 1.0) == pytest.approx(100_000)


def test_price_crash_alone_is_not_a_rug():
    """**币价腰斩 75%、没有一个人撤资 → 抽水比例必须是 0。**

    不做价格修正的话这里会算出 -50%，于是每次砸盘都报一次跑路。
    """
    liq_now = 50_000          # 正是 √0.25 = 0.5 倍，价格解释得了全部
    assert R.drain_pct(100_000, 1.0, 0.25, liq_now) == pytest.approx(0.0, abs=1e-9)


def test_real_drain_is_caught_even_when_price_is_flat():
    """价格没动，池子少了一半 → 有人搬钱。"""
    assert R.drain_pct(100_000, 1.0, 1.0, 50_000) == pytest.approx(0.5)


def test_drain_on_top_of_a_price_crash_is_still_caught():
    """既砸盘又抽水：价格因素只解释掉一半，剩下的还是要算数。"""
    # 价格 -75% → 本该剩 50k；实际只剩 20k → 被抽走 60%
    assert R.drain_pct(100_000, 1.0, 0.25, 20_000) == pytest.approx(0.6)


def test_growing_pool_never_reports_negative_drain():
    """池子变大不是坏事，不该给个负数把人吓一跳。"""
    assert R.drain_pct(100_000, 1.0, 1.0, 180_000) == 0.0


def test_missing_price_falls_back_to_no_correction():
    """价格拿不到时不做修正——宁可少报，不能拿错基准误报。"""
    assert R.expected_liq(100_000, None, None) == 100_000
    assert R.expected_liq(100_000, 0, 5.0) == 100_000


def test_drain_needs_a_baseline():
    assert R.drain_pct(None, 1.0, 1.0, 100) is None
    assert R.drain_pct(0, 1.0, 1.0, 100) is None


# ── 判定与状态机 ──────────────────────────────────────────────
def test_first_sighting_only_builds_a_baseline():
    """第一次见这个币时没有基线，谈不上"少了多少"，只能先记下来。"""
    hit, d, _sev = R.assess("0xabc", price=1.0, liq=100_000)
    assert hit is False and d is None
    assert storage.data["rugwatch"]["0xabc"]["base_liq"] == 100_000


def test_a_real_drain_fires():
    R.assess("0xabc", 1.0, 100_000)                 # 建基线
    hit, d, severe = R.assess("0xabc", 1.0, 40_000)  # 少了 60%
    assert hit is True
    assert d == pytest.approx(0.6)
    assert severe is False                           # 60% 还没到"基本跑光"


def test_severe_drain_is_flagged_separately():
    """抽走 70% 以上和抽走 40% 不是一回事，措辞和紧急程度都该不同。"""
    R.assess("0xabc", 1.0, 100_000)
    hit, d, severe = R.assess("0xabc", 1.0, 20_000)
    assert hit and severe is True


def test_small_wobbles_do_not_fire():
    """做市商调仓、小额加减池是日常，报出来全是噪音。"""
    R.assess("0xabc", 1.0, 100_000)
    hit, _d, _s = R.assess("0xabc", 1.0, 80_000)     # 只少 20%
    assert hit is False


def test_price_crash_does_not_fire_end_to_end():
    """端到端再验一次那条核心判据：只砸盘，不该报。"""
    R.assess("0xabc", 1.0, 100_000)
    hit, d, _s = R.assess("0xabc", 0.25, 50_000)
    assert hit is False
    assert d == pytest.approx(0.0, abs=1e-9)


def test_cooldown_prevents_repeat_alerts():
    """抽水是个持续过程，不冷却会每 5 分钟报一次同一件事。"""
    R.assess("0xabc", 1.0, 100_000)
    assert R.assess("0xabc", 1.0, 30_000)[0] is True
    assert R.assess("0xabc", 1.0, 20_000)[0] is False, "冷却期内不该重复报"


def test_rearm_only_after_liquidity_comes_back():
    """报过之后要等回补才重新武装，否则会在门槛上抖动刷屏。"""
    now = time.time()
    R.assess("0xabc", 1.0, 100_000, now=now)
    assert R.assess("0xabc", 1.0, 30_000, now=now)[0] is True
    # 冷却过了，但流动性还趴在低位 → 仍然不重复报
    assert R.assess("0xabc", 1.0, 30_000, now=now + R.COOLDOWN + 1)[0] is False
    # 回补到接近预期 → 重新武装；再抽一次要能报出来
    R.assess("0xabc", 1.0, 99_000, now=now + R.COOLDOWN + 2)
    assert R.assess("0xabc", 1.0, 30_000, now=now + R.COOLDOWN + 3)[0] is True


def test_baseline_rises_when_liquidity_grows():
    """有人加池子要抬基线，否则后面拿老基线比会一直显得"多"。"""
    R.assess("0xabc", 1.0, 100_000)
    R.assess("0xabc", 1.0, 300_000)
    assert storage.data["rugwatch"]["0xabc"]["base_liq"] == pytest.approx(300_000)


def test_baseline_never_falls_on_its_own():
    """**基线只涨不跌。**

    降基线的话，分批慢慢抽水会被一路合理化成"新常态"——每次只掉一点、
    基线跟着降，永远触发不了。这是这类监控最容易被绕过的方式。
    """
    R.assess("0xabc", 1.0, 100_000)
    R.assess("0xabc", 1.0, 80_000)      # 少了但没到门槛
    assert storage.data["rugwatch"]["0xabc"]["base_liq"] == pytest.approx(100_000)


def test_dust_pools_are_ignored():
    """本来就只有几千美元的池子随时会归零，那是建监控时就该知道的事，
    不是一个"事件"。"""
    R.assess("0xdust", 1.0, 3_000)
    hit, _d, _s = R.assess("0xdust", 1.0, 500)
    assert hit is False


# ── 收集范围 ──────────────────────────────────────────────────
# ⚠️ **这几条的 fixture 必须用真正写数据的那个函数来造。**
#
# 第一版我凭印象手写成 `{chat_id: [...]}` 字典，而 `watchpct` 存的是**扁平 list**
# （每条自带 chat_id）。于是 `_onchain_watches` 里 `.items()` 在线上每 5 分钟抛一次
# AttributeError，**而单测一路绿灯——因为测试和代码错在同一个地方**。
# 用生产者造 fixture，形状变了两边一起红，才挡得住这类错。

def _add_watch(chat_id, symbol, market="onchain", **extra):
    """走 watchpct 真正的落盘函数，形状永远和线上一致。"""
    from handlers import watchpct as W
    ok, _msg = W._store(chat_id, symbol, 5, "tester", 1.0, "bsc", market,
                        name=extra.pop("name", None), extra=extra or None)
    assert ok


def test_watchpct_is_a_flat_list_not_a_dict():
    """把形状本身钉死：`_onchain_watches` 的写法完全取决于这一条。"""
    storage.data["watchpct"] = []
    _add_watch(1, "0xabc")
    assert isinstance(storage.data["watchpct"], list)
    assert storage.data["watchpct"][0]["chat_id"] == 1, "chat_id 在记录里，不是外层的键"


def test_only_onchain_watches_are_collected():
    """交易所的币没有"池子"这回事，混进来会白白打接口。"""
    storage.data["watchpct"] = []
    _add_watch(1, "0xabc", "onchain")
    _add_watch(1, "BTC", "spot")
    got = R._onchain_watches()
    assert [w["symbol"] for _cid, w in got] == ["0xabc"]


def test_same_token_watched_by_several_chats():
    """同一个币被多个会话盯着时，取一次数发多次——不能各取各的。"""
    storage.data["watchpct"] = []
    _add_watch(1, "0xabc")
    _add_watch(2, "0xabc")
    got = R._onchain_watches()
    assert len(got) == 2
    assert {cid for cid, _w in got} == {"1", "2"}


def test_malformed_records_do_not_break_the_scan():
    """data.json 被手改过/旧格式残留时，别让整个任务挂掉。"""
    storage.data["watchpct"] = ["坏数据", None, {"market": "onchain"}]
    assert R._onchain_watches() == []


# ── 文案 ──────────────────────────────────────────────────────
def test_alert_explains_the_price_correction():
    """不解释的话，看的人会拿卡片上的流动性数字去对，发现对不上。"""
    text = R.format_alert("PEPE", "0xabc", "bsc", 0.62, False, 38_000, 100_000)
    assert "扣掉" in text and "平方根" in text


def test_severe_alert_tells_you_what_to_do():
    text = R.format_alert("PEPE", "0xabc", "bsc", 0.85, True, 15_000, 100_000)
    assert "卖不出去" in text
    assert "别再补仓" in text


def test_alert_is_plain_text_safe():
    """合约地址带下划线时 Markdown 会把它吃掉，这条消息必须纯文本发。"""
    text = R.format_alert("A_B", "0x_abc_def", "bsc", 0.5, False, 5e4, 1e5)
    assert "0x_abc_def" in text, "地址要原样出现，抄下来才是对的"


# ── 全库护栏：共享结构不能各猜各的 ────────────────────────────
def test_every_reader_of_watchpct_treats_it_as_a_list():
    """`data["watchpct"]` 是扁平 list（storage.py 的初始化那行写着）。

    我在 rugwatch 里按字典写，线上每 5 分钟抛一次 AttributeError。
    **跨模块共享的数据结构最容易各猜各的**——一个模块写、另一个模块读，
    中间没有任何东西约束形状。这条扫一遍，谁再当字典用就红。
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    bad = []
    for f in sorted((root / "handlers").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r'data(?:\.get\(|\[)["\']watchpct["\']\)?[^\n]*', src):
            line = m.group(0)
            if ".items()" in line or ".values()" in line or ".keys()" in line:
                bad.append(f"{f.name}: {line.strip()}")
    assert not bad, f"watchpct 是 list，这些地方当字典用了：{bad}"


def test_storage_documents_the_shape():
    """形状要在 storage 的初始化那儿写清楚——那是唯一的权威。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "storage.py").read_text(encoding="utf-8")
    assert 'setdefault("watchpct", [])' in src
    assert "chat_id" in src.split('setdefault("watchpct"')[1][:120], \
        "注释里要写明每条记录自带 chat_id，否则读的人会以为外层是按 chat 分组的"
