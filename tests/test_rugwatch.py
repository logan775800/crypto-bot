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
    storage.data["watchpct"] = {}
    yield
    storage.data["rugwatch"] = {}
    storage.data["watchpct"] = {}


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
def test_only_onchain_watches_are_collected():
    """交易所的币没有"池子"这回事，混进来会白白打接口。"""
    storage.data["watchpct"] = {
        "1": [{"symbol": "0xabc", "market": "onchain"},
              {"symbol": "BTC", "market": "spot"}],
    }
    got = R._onchain_watches()
    assert [w["symbol"] for _cid, w in got] == ["0xabc"]


def test_same_token_watched_by_several_chats():
    """同一个币被多个会话盯着时，取一次数发多次——不能各取各的。"""
    storage.data["watchpct"] = {
        "1": [{"symbol": "0xabc", "market": "onchain"}],
        "2": [{"symbol": "0xabc", "market": "onchain"}],
    }
    got = R._onchain_watches()
    assert len(got) == 2
    assert {cid for cid, _w in got} == {"1", "2"}


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
