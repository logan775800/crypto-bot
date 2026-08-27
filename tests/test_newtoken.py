"""链上新币上线告警。

他的原话（2026-08-27）：「链上出了新币，发行的时候可以第一时间告警推送到群里吗」。

**这个功能最大的敌人不是技术，是刷屏。**
实测（GeckoTerminal new_pools）：光 BSC 一条链最近一小时就新建 60 个池子、
Solana 40 个，而且那还是分页上限。全推等于每小时上百条，群当场废掉——
然后所有人把机器人静音，连真正有用的告警也一起听不到。

所以这里守的主要是**筛得够不够狠**，以及安全检查不能被绕过。
"""
import time

import pytest

import storage
from handlers import newtoken as N


@pytest.fixture(autouse=True)
def _clean():
    storage.data["newtoken"] = {"on": [], "seen": {}}
    yield
    storage.data["newtoken"] = {"on": [], "seen": {}}


def _pool(liq=100_000, buys=40, sells=20, age_h=1.0, name="景甜 / WBNB"):
    from datetime import datetime, timezone, timedelta
    t = datetime.now(timezone.utc) - timedelta(hours=age_h)
    return {
        "name": name,
        "pool_address": f"0xpool{liq}{buys}",
        "reserve_in_usd": str(liq),
        "fdv_usd": "500000",
        "pool_created_at": t.isoformat().replace("+00:00", "Z"),
        "transactions": {"h1": {"buys": buys, "sells": sells}},
        "_relationships": {"base_token": {"data": {"id": "bsc_0xdeadbeef"}}},
        "_net": "bsc",
    }


# ── 三层闸 ────────────────────────────────────────────────────
def test_thin_pools_are_rejected():
    """池子太浅的买进去就出不来。实测 100 个新池里 ≥5 万的只有 4 个。"""
    ok, why = N.passes(_pool(liq=5_000), 50_000, 30)
    assert not ok and why == "流动性不足"


def test_pools_with_no_traction_are_rejected():
    """建了池子没人买 = 还没成型，等它有量再说。"""
    ok, why = N.passes(_pool(buys=1, sells=0), 50_000, 30)
    assert not ok and why == "还没人交易"


def test_old_pools_are_not_new_coins():
    """三天前的池子被推出来，那是漏推不是新上线。"""
    ok, why = N.passes(_pool(age_h=72), 50_000, 30)
    assert not ok and why == "太老"


def test_a_real_launch_passes():
    assert N.passes(_pool(), 50_000, 30)[0] is True


def test_rejection_reason_is_kept():
    """**调阈值时他会问「为什么一条都没有」**，那时得答得出卡在哪一层。"""
    for p, expect in ((_pool(liq=1), "流动性不足"),
                      (_pool(buys=0, sells=0), "还没人交易"),
                      (_pool(age_h=99), "太老")):
        assert N.passes(p, 50_000, 30)[1] == expect


# ── 安全检查不能被绕过 ────────────────────────────────────────
def test_token_address_comes_from_relationships():
    """GeckoTerminal 的 attributes 里只有**池子地址**，代币地址在 relationships。
    搞混的话查出来的是一个不存在的东西，而且不会报错。"""
    assert N.base_token_address(_pool()) == "0xdeadbeef"


def test_missing_token_address_blocks_the_push():
    import asyncio
    ok, why = asyncio.run(N.safety("bsc", ""))
    assert ok is False and "地址" in why


def test_unknown_safety_is_treated_as_unsafe(monkeypatch):
    """**「没查到风险」和「没有风险」是两回事。**
    这条链上后者的代价是归零，所以查不出来就不推。"""
    import asyncio

    async def _boom(chain, addr):
        raise RuntimeError("接口挂了")
    from handlers import tokensec
    monkeypatch.setattr(tokensec, "check", _boom)
    assert asyncio.run(N.safety("bsc", "0xabc"))[0] is False


def test_dangerous_tokens_are_never_pushed(monkeypatch):
    """推一个买得进卖不出的币，比不推有害得多。"""
    import asyncio

    async def _honeypot(chain, addr):
        return {"ok": True, "dangers": ["⛔ **蜜罐**：买得进卖不出"], "warnings": []}
    from handlers import tokensec
    monkeypatch.setattr(tokensec, "check", _honeypot)
    ok, why = asyncio.run(N.safety("bsc", "0xabc"))
    assert ok is False and "蜜罐" in why


def test_warnings_do_not_block_but_are_carried(monkeypatch):
    """有税、集中度高这类要说，但不至于不推——不然几乎没有币能过。"""
    import asyncio

    async def _warn(chain, addr):
        return {"ok": True, "dangers": [], "warnings": ["⚠️ 买入税 5%"]}
    from handlers import tokensec
    monkeypatch.setattr(tokensec, "check", _warn)
    ok, why = asyncio.run(N.safety("bsc", "0xabc"))
    assert ok is True and "5%" in why


# ── 去重 / 订阅 ───────────────────────────────────────────────
def test_same_pool_is_not_pushed_twice():
    N._mark("0xabc")
    assert N._fresh("0xabc") is False
    assert N._fresh("0xother") is True


def test_seen_list_is_capped():
    """新池子是无限供应的，不封顶 data.json 会一直长。"""
    for i in range(N.SEEN_KEEP + 200):
        N._mark(f"0x{i}")
    assert len(storage.data["newtoken"]["seen"]) <= N.SEEN_KEEP


def test_subscription_toggle():
    assert N.is_on(-100) is False
    N.toggle(-100, True)
    assert N.is_on(-100) is True
    N.toggle(-100, False)
    assert N.is_on(-100) is False


def test_thresholds_are_adjustable():
    N.set_threshold(min_liq=20_000, min_txns=10)
    assert N.thresholds() == (20_000, 10)


def test_liquidity_floor_cannot_be_set_to_zero():
    """门槛设成 0 等于把刷屏闸门整个拆掉。"""
    N.set_threshold(min_liq=0)
    assert N.thresholds()[0] >= 1000


# ── 文案 ──────────────────────────────────────────────────────
def test_alert_gives_enough_to_judge():
    """只报个名字没用——要给出能自己判断的东西。"""
    txt = N.format_alert(_pool(), "BSC", "")
    for must in ("流动性", "1h 成交", "合约", "/oc"):
        assert must in txt


def test_alert_does_not_pretend_it_is_a_good_project():
    """筛掉蜜罐 ≠ 这是好项目。不写清楚的话，"机器人推的"会被当成背书。"""
    txt = N.format_alert(_pool(), "BSC", "")
    assert "九成归零" in txt
    assert "不代表它是好项目" in txt


def test_warnings_show_up_in_the_alert():
    txt = N.format_alert(_pool(), "BSC", "⚠️ 买入税 5%")
    assert "5%" in txt


# ── 接线 ──────────────────────────────────────────────────────
def test_command_and_job_are_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("newtoken"' in src
    assert "newtoken.scan" in src, "没注册定时任务，订阅了也不会推"
    assert '"链上新币"' in src, "没挂心跳——这条链坏了会静默"


def test_toggle_is_in_the_alert_panel():
    from handlers import menu
    cbs = [b.callback_data for row in menu.notify_kb(-100).inline_keyboard for b in row]
    assert "nt:toggle" in cbs


# ── 热点模式：中文梗币（2026-08-27 他追加的需求）──────────────
# 「能把像 我的女友景甜、牛来 这种有潜力有新闻的链上新币过滤出来吗」
#
# **「有潜力」是判断不是数据，给不了。** 但他举的两个例子有个明确的共同特征：
# 中文梗名。实测 BSC 100 个新池里带中文名的 20 个，而且他说的那两个都在里面。
# 更要紧的是**这类的流动性只有 $3k~$20k**，普通模式（门槛 5 万）会把它们全筛掉——
# 这正是他问这个问题的原因。

def _hot(name="我的女友景甜 / USDT 0.25%", liq=5000, buys=30, sells=20,
         addr="0xa", age_h=0.2):
    from datetime import datetime, timezone, timedelta
    t = datetime.now(timezone.utc) - timedelta(hours=age_h)
    return {"name": name, "pool_address": addr, "reserve_in_usd": str(liq),
            "pool_created_at": t.isoformat().replace("+00:00", "Z"),
            "transactions": {"h1": {"buys": buys, "sells": sells}},
            "_relationships": {"base_token": {"data": {"id": "bsc_0xdead"}}}}


def test_chinese_names_are_the_signal():
    """他举的两个例子都是中文梗名。这是这批币真实、稳定的共同特征——
    而"像不像 meme"没法用数据判。"""
    assert N.is_hot_name(_hot("我的女友景甜 / USDT")) is True
    assert N.is_hot_name(_hot("牛来 / USDT")) is True
    assert N.is_hot_name(_hot("PEPE / WBNB")) is False


def test_hot_threshold_is_far_below_the_normal_one():
    """**这就是他问这个问题的原因。**
    实测这类币流动性只有 $3k~$20k，默认门槛 5 万会把它们全筛掉。"""
    assert N.HOT_MIN_LIQ < N.MIN_LIQ / 5
    assert N.passes(_hot(liq=5000), N.HOT_MIN_LIQ, N.HOT_MIN_TXNS)[0] is True
    assert N.passes(_hot(liq=5000), N.MIN_LIQ, N.MIN_TXNS)[0] is False


def test_fake_pools_are_still_filtered():
    """实测有两个「我的女友景甜」**流动性只有 $12 却有 60 笔成交** ——
    典型假盘。门槛压低不等于不设防。"""
    assert N.passes(_hot(liq=12, buys=40, sells=20),
                    N.HOT_MIN_LIQ, N.HOT_MIN_TXNS)[0] is False


def test_base_name_strips_pair_and_tax():
    """`我的女友景甜 / USDT 0.25%` → `我的女友景甜`。同名归组靠它。"""
    assert N.base_name(_hot("我的女友景甜 / USDT 0.25%")) == "我的女友景甜"
    assert N.base_name(_hot("牛来 / USDT")) == "牛来"


def test_same_name_collision_is_counted_and_ranked():
    """**同名多个 = 这个梗正在热**（有人在抄），但大部分是跟风盘。
    只报数量不说排名等于把最关键的那句省掉——他要知道自己看到的是
    原盘还是第 3 个抄的。"""
    pools = [_hot(liq=5000, addr="0x1"), _hot(liq=12, addr="0x2"),
             _hot(liq=3000, addr="0x3")]
    n, rank = N.rank_same_name(pools, pools[0])
    assert n == 3 and rank == 1
    n2, rank2 = N.rank_same_name(pools, pools[1])
    assert n2 == 3 and rank2 == 3


def test_different_names_do_not_collide():
    pools = [_hot("我的女友景甜 / USDT"), _hot("牛来 / USDT", addr="0x9")]
    assert N.rank_same_name(pools, pools[0])[0] == 1


def test_hot_alert_says_how_many_are_copying():
    txt = N.format_hot(_hot(), "BSC", "", 5, 1)
    assert "5 个" in txt and "正在热" in txt
    assert "流动性最大的那个" in txt
    assert "认准合约地址" in txt


def test_hot_alert_says_it_ranks_lower_when_it_does():
    txt = N.format_hot(_hot(), "BSC", "", 5, 3)
    assert "排第 3" in txt


def test_hot_alert_refuses_to_claim_potential():
    """**他要的是「有潜力」，而那不是数据能给的。**
    含糊过去的话，机器人推的会被当成背书。"""
    txt = N.format_hot(_hot(), "BSC", "", 1, 1)
    assert "彩票" in txt
    assert "没有任何「有潜力」的判断" in txt
    assert "随时归零" in txt


def test_hot_is_a_separate_subscription():
    """门槛差一个数量级，不能和普通模式共用一个开关。"""
    assert N.hot_enabled(-100) is False
    N.toggle_hot(-100, True)
    assert N.hot_enabled(-100) is True and N.is_on(-100) is False


def test_off_turns_both_off():
    """他发 off 是想清净，不该只关掉一半。"""
    import inspect
    src = inspect.getsource(N.newtoken_cmd)
    seg = src.split('"off"')[1][:300]
    assert "toggle_hot" in seg


def test_only_the_biggest_of_a_name_is_pushed():
    """同名 5 个全推 = 5 条一模一样的消息。只推流动性最大的那个，
    其余是跟风盘，推出来只会稀释注意力。"""
    import inspect
    src = inspect.getsource(N.scan)
    assert "rank != 1" in src
    assert "seen_names" in src
