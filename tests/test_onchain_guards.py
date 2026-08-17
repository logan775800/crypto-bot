"""链上的安全检查、信号闸门、告警硬化。

这几条合起来回答一个问题：**什么时候不该给交易型信号。**
链上池子浅、数据残缺、合约有后门的时候，一句「涨了 20%」不是信息而是陷阱——
照着它下单，亏的是滑点和退不出来，不是判断错。
"""
import asyncio
import time

import pytest

from handlers import onchain as OC
from handlers import tokensec as TS


def tok(liq=1_000_000, vol=500_000, price=1.0, buys=100, sells=90, pools=None,
        created=None):
    return {"symbol": "X", "name": "X", "address": "0xa", "pool": "0xp",
            "chain_key": "bsc", "chain": "bsc", "chain_cn": "BNB链", "dex": "uni",
            "price": price, "liq": liq, "vol24": vol, "chg24": 5.0, "chg1h": 1.0,
            "fdv": 10_000_000, "buys": buys, "sells": sells,
            "created_ms": created if created is not None else int(time.time() * 1000) - 86400_000 * 30,
            "pools": pools if pools is not None else [
                {"price": price, "liq": liq, "pool": "0xp"},
                {"price": price * 1.001, "liq": liq / 2, "pool": "0xq"}]}


# ── 滑点估算 ─────────────────────────────────────────────────────
def test_slippage_grows_as_pool_shrinks():
    deep = OC.slippage_pct(10_000_000, 5_000)
    thin = OC.slippage_pct(50_000, 5_000)
    assert thin > deep * 10
    assert deep < 1.0


def test_slippage_on_a_tiny_pool_is_brutal():
    """池子 2 万还想吃 5 千，冲击是三成——这不是"滑点"，是没法执行。"""
    assert OC.slippage_pct(20_000, 5_000) > 25


def test_slippage_never_divides_by_zero():
    assert OC.slippage_pct(0, 1_000) > 0


# ── 数据完整度 ───────────────────────────────────────────────────
def test_completeness_counts_what_is_missing():
    got, total, missing = OC.completeness(tok())
    assert total == 6 and "安全检查" in missing


def test_completeness_full_house():
    sec = {"ok": True, "dangers": [], "warnings": [], "unknown": []}
    got, total, missing = OC.completeness(tok(), sec)
    assert got == total and missing == []


# ── 信号闸门 ─────────────────────────────────────────────────────
def test_healthy_token_passes():
    sec = {"ok": True, "dangers": [], "warnings": [], "unknown": [],
           "sellable": True}
    ok, why = OC.gate(tok(), sec)
    assert ok and why == []


def test_thin_pool_blocks_signals():
    ok, why = OC.gate(tok(liq=20_000, vol=10_000))
    assert not ok
    assert any("没法执行" in r for r in why)


def test_dead_pool_blocks_signals():
    ok, why = OC.gate(tok(liq=5_000_000, vol=100, buys=1, sells=1))
    assert not ok
    assert any("假池" in r or "不可信" in r for r in why)


def test_wild_spread_blocks_signals():
    """同一个币两个池差 30%，此刻根本没有一个"公允价"。"""
    pools = [{"price": 1.0, "liq": 500_000, "pool": "a"},
             {"price": 1.3, "liq": 400_000, "pool": "b"}]
    ok, why = OC.gate(tok(pools=pools))
    assert not ok and any("偏差" in r for r in why)


def test_honeypot_blocks_signals():
    sec = {"ok": True, "dangers": ["⛔ 蜜罐"], "warnings": [], "unknown": [],
           "sellable": False}
    ok, why = OC.gate(tok(), sec)
    assert not ok
    assert any("卖不出去" in r for r in why)


def test_gate_text_says_no_direction_no_price():
    _ok, why = OC.gate(tok(liq=10_000, vol=5_000))
    txt = OC.gate_text(why)
    assert "暂停交易型信号" in txt and "不给方向和价位" in txt


# ── 安全检查解析 ─────────────────────────────────────────────────
def evm(**over):
    r = {"buy_tax": "0", "sell_tax": "0", "is_open_source": "1", "is_proxy": "0",
         "owner_address": TS.ZERO_ADDR, "holders": [], "lp_holders": []}
    r.update(over)
    return {"result": {"0xa": r}}


def test_missing_field_is_unknown_not_safe():
    """实测查「牛来」时 GoPlus 根本没返回 is_honeypot——
    把"没说有问题"读成"没问题"，等于发假的安全感。"""
    sec = TS._parse_evm(evm(), "0xa")
    assert sec["sellable"] is None
    assert any("可卖性" in u for u in sec["unknown"])


def test_honeypot_is_a_danger():
    sec = TS._parse_evm(evm(is_honeypot="1"), "0xa")
    assert sec["sellable"] is False
    assert any("蜜罐" in d for d in sec["dangers"])


def test_high_sell_tax_is_a_danger():
    sec = TS._parse_evm(evm(sell_tax="0.25"), "0xa")
    assert sec["sell_tax"] == pytest.approx(25.0)
    assert any("卖出税" in d for d in sec["dangers"])


def test_renounced_ownership_downgrades_dormant_permissions():
    """实测 PEPE：transfer_pausable=1、is_blacklisted=1 都是真的，但所有权已放弃、
    非代理、无隐藏所有者——没人能调用它们。报成"合约方可以随时冻结交易"
    既吓人又不准确。"""
    sec = TS._parse_evm(evm(transfer_pausable="1", is_blacklisted="1"), "0xa")
    assert sec["dangers"] == []
    assert any("所有权已放弃" in w for w in sec["warnings"])
    assert sec["renounced"] is True


def test_live_owner_keeps_permissions_dangerous():
    sec = TS._parse_evm(evm(transfer_pausable="1", owner_address="0xdeadbeef"), "0xa")
    assert any("冻结" in d for d in sec["dangers"])
    assert sec["renounced"] is False


def test_proxy_cancels_the_renouncement():
    """可升级代理下，"放弃所有权"不作数——逻辑随时能被换掉。"""
    sec = TS._parse_evm(evm(is_proxy="1", is_mintable="1"), "0xa")
    assert sec["renounced"] is False
    assert any("增发" in d for d in sec["dangers"])


def test_hidden_owner_is_always_a_danger():
    sec = TS._parse_evm(evm(hidden_owner="1"), "0xa")
    assert any("隐藏" in d for d in sec["dangers"])


def test_lp_burn_address_counts_as_locked():
    """烧币地址判定第一版写错过（多要了 24 个 0），PEPE 真正烧掉的那份没算进去。"""
    lp = [{"address": "0x1111111111111111111111111111111111111111",
           "percent": "0.2", "is_locked": 0},
          {"address": "0x000000000000000000000000000000000000dEaD",
           "percent": "0.8", "is_locked": 0}]
    sec = TS._parse_evm(evm(lp_holders=lp), "0xa")
    assert sec["lp_locked_pct"] == pytest.approx(80.0)
    assert not any("LP" in w for w in sec["warnings"])


def test_unreadable_lp_data_is_unknown_not_zero():
    """实测 PEPE 的 lp_holders 首位是代币合约自己占 99.88%，这份数据没法解释。
    这时报"只有 0% 锁仓、做市方随时能撤走"是在用读不懂的数吓人。"""
    lp = [{"address": "0xa", "percent": "0.998", "is_locked": 0}]
    sec = TS._parse_evm(evm(lp_holders=lp), "0xa")
    assert sec["lp_locked_pct"] is None
    assert any("LP" in u for u in sec["unknown"])


def test_unlocked_lp_is_warned():
    lp = [{"address": "0x1111111111111111111111111111111111111111",
           "percent": "0.9", "is_locked": 0}]
    sec = TS._parse_evm(evm(lp_holders=lp), "0xa")
    assert sec["lp_locked_pct"] == pytest.approx(0.0)
    assert any("撤走池子" in w for w in sec["warnings"])


def test_holder_concentration_is_flagged():
    hs = [{"percent": "0.35"}, {"percent": "0.2"}, {"percent": "0.1"}]
    sec = TS._parse_evm(evm(holders=hs), "0xa")
    assert sec["top10_pct"] == pytest.approx(65.0)
    assert any("第一大持有者" in w for w in sec["warnings"])


def test_solana_authorities_are_parsed():
    d = {"result": {"0xa": {"mintable": {"status": "1", "authority": []},
                            "freezable": {"status": "0", "authority": []},
                            "holders": [], "holder_count": "100"}}}
    sec = TS._parse_solana(d, "0xa")
    assert any("增发" in x for x in sec["dangers"])
    assert sec["chain_kind"] == "solana"


def test_verdict_never_claims_safe_when_unknown():
    sec = {"ok": True, "dangers": [], "warnings": [], "unknown": ["可卖性"]}
    icon, txt = TS.verdict(sec)
    assert icon == "❓" and "查不到" in txt


def test_render_says_unknown_is_not_safe():
    sec = {"ok": False, "why": "数据源没通"}
    assert "查不到 ≠ 安全" in TS.render(sec, "X")


# ── 告警硬化 ─────────────────────────────────────────────────────
def test_onchain_cooldown_is_longer():
    from handlers import watchpct as W
    assert W.ONCHAIN_COOLDOWN > W.COOLDOWN


def test_confirm_rejects_a_wick(monkeypatch):
    """瞬时价到了阈值，但收盘价没走出来——那是插针，不该当信号。"""
    from handlers import watchpct as W
    now = time.time() * 1000

    async def fake_ohlcv(chain, pool, tf, limit=6, why=False):
        rows = [[now - 900_000 * 2, 1, 1, 1, 1.0, 10],
                [now - 900_000, 1, 1.5, 1, 1.02, 10],      # 已收盘：只走了 2%
                [now - 60_000, 1, 1.5, 1, 1.3, 10]]        # 还没收盘
        return (rows, "") if why else rows
    monkeypatch.setattr(OC, "ohlcv", fake_ohlcv)
    w = {"pct": 20, "chain": "bsc", "pool": "0xp"}
    ok, why = asyncio.run(W._confirm_onchain(w, 1.0))
    assert ok is False and "收盘价只走了" in why


def test_confirm_accepts_a_real_move(monkeypatch):
    from handlers import watchpct as W
    now = time.time() * 1000

    async def fake_ohlcv(chain, pool, tf, limit=6, why=False):
        rows = [[now - 900_000 * 2, 1, 1, 1, 1.0, 10],
                [now - 900_000, 1, 1.4, 1, 1.35, 10],      # 已收盘就在高位
                [now - 60_000, 1, 1.4, 1, 1.36, 10]]
        return (rows, "") if why else rows
    monkeypatch.setattr(OC, "ohlcv", fake_ohlcv)
    ok, _why = asyncio.run(W._confirm_onchain({"pct": 20, "chain": "bsc",
                                               "pool": "0xp"}, 1.0))
    assert ok is True


def test_confirm_without_klines_does_not_block(monkeypatch):
    """拿不到K线就如实说"未确认"，不能因此把真波动吞掉。"""
    from handlers import watchpct as W

    async def none_ohlcv(*a, **k):
        return ([], "429") if k.get("why") else []
    monkeypatch.setattr(OC, "ohlcv", none_ohlcv)
    ok, _why = asyncio.run(W._confirm_onchain({"pct": 20, "chain": "bsc",
                                               "pool": "0xp"}, 1.0))
    assert ok is None


# ── 失效状态 / 去重（走完整轮询循环） ────────────────────────────
class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text=None, **kw):
        self.sent.append(text if text is not None else "")


class Ctx:
    def __init__(self):
        self.bot = Bot()


@pytest.fixture
def watch_env(monkeypatch):
    """装好一条链上监控，并接管取价 / K线 / 详情。"""
    import storage
    from handlers import watchpct as W
    storage.data["watchpct"] = []

    box = {"price": 1.0}

    async def fake_price_at(sym, label):
        return box["price"]

    async def fake_ohlcv(chain, pool, tf, limit=6, why=False):
        now = time.time() * 1000
        rows = [[now - 900_000 * 2, 1, 1, 1, box["price"], 10],
                [now - 900_000, 1, 1, 1, box["price"], 10],
                [now - 60_000, 1, 1, 1, box["price"], 10]]
        return (rows, "") if why else rows

    async def fake_by_address(addr, chain=None):
        return tok(), 1

    from handlers import source as S
    monkeypatch.setattr(S, "price_at", fake_price_at)
    monkeypatch.setattr(OC, "ohlcv", fake_ohlcv)
    monkeypatch.setattr(OC, "by_address", fake_by_address)

    storage.data["watchpct"].append({
        "chat_id": 1, "symbol": "0xa", "pct": 20, "market": "onchain",
        "base": 1.0, "src": "链上BNB链", "last_ts": 0, "set_by": "me",
        "name": "X", "chain": "bsc", "pool": "0xp", "misses": 0})
    yield box, storage.data["watchpct"][0], W
    storage.data["watchpct"] = []


def test_alert_carries_liquidity_slippage_completeness(watch_env):
    box, _w, W = watch_env
    box["price"] = 1.5                      # +50%，超阈值
    ctx = Ctx()
    asyncio.run(W.check_watchpct(ctx))
    assert ctx.bot.sent, "该报没报"
    msg = ctx.bot.sent[0]
    assert "池子" in msg and "滑点" in msg and "数据完整度" in msg


def test_duplicate_signal_is_suppressed(watch_env):
    """同方向同量级别重复播报——链上一笔大单能连着触发好几轮。"""
    box, w, W = watch_env
    box["price"] = 1.5
    ctx = Ctx()
    asyncio.run(W.check_watchpct(ctx))
    first = len(ctx.bot.sent)
    # 刚过冷却就又来一次同方向同量级的——这才是要去掉的"重复"。
    # （把 last_ts 清零等于模拟"很久以后"，那种情况本来就该再报）
    w["last_ts"] = time.time() - (W.ONCHAIN_COOLDOWN + 1)
    box["price"] = 1.5 * 1.5
    asyncio.run(W.check_watchpct(ctx))
    assert len(ctx.bot.sent) == first, "同方向同量级在去重窗口内被重复播报了"


def test_duplicate_window_expires(watch_env):
    """去重是**时间窗内**的：隔久了同样的动静是新事件，该报还得报。"""
    box, w, W = watch_env
    box["price"] = 1.5
    ctx = Ctx()
    asyncio.run(W.check_watchpct(ctx))
    first = len(ctx.bot.sent)
    w["last_ts"] = time.time() - W.ONCHAIN_COOLDOWN * 4
    box["price"] = 1.5 * 1.5
    asyncio.run(W.check_watchpct(ctx))
    assert len(ctx.bot.sent) > first


def test_pool_gone_marks_the_watch_invalid(watch_env, monkeypatch):
    """链上池子可能被撤走，那时价格永远取不到——一直沉默着，
    用户会以为"还在盯，只是没动"。"""
    _box, w, W = watch_env
    from handlers import source as S

    async def dead(*a, **k):
        return None
    monkeypatch.setattr(S, "price_at", dead)
    ctx = Ctx()
    for _ in range(W.DEAD_STRIKES):
        asyncio.run(W.check_watchpct(ctx))
    assert w.get("invalid")
    assert any("失效" in m for m in ctx.bot.sent)


def test_invalid_watch_stops_notifying(watch_env, monkeypatch):
    _box, w, W = watch_env
    w["invalid"] = "池子没了"
    ctx = Ctx()
    asyncio.run(W.check_watchpct(ctx))
    assert ctx.bot.sent == []


def test_unconfirmed_move_is_marked_and_base_not_reset(watch_env, monkeypatch):
    """未收盘确认的波动照发但标明"先当噪音看"，且**不重设基准**——
    重设了就等于承认这个插针价是真的。"""
    box, w, W = watch_env

    async def wick_ohlcv(chain, pool, tf, limit=6, why=False):
        now = time.time() * 1000
        rows = [[now - 900_000 * 2, 1, 1, 1, 1.0, 10],
                [now - 900_000, 1, 1.6, 1, 1.01, 10],     # 收盘只有 +1%
                [now - 60_000, 1, 1.6, 1, 1.5, 10]]
        return (rows, "") if why else rows
    monkeypatch.setattr(OC, "ohlcv", wick_ohlcv)
    box["price"] = 1.5
    ctx = Ctx()
    asyncio.run(W.check_watchpct(ctx))
    assert any("未确认" in m for m in ctx.bot.sent)
    assert w["base"] == 1.0, "未确认就不该重设基准"


# ── 限频：实测 GeckoTerminal 连打第 3 次就 429 ────────────────────
def test_alert_includes_security_so_completeness_is_not_permanently_short():
    """告警里原来没查安全检查，于是完整度**永远**是 5/6「缺安全检查」——
    一条必现的假缺失，看几次之后这一行就没人信了。"""
    import inspect
    from handlers import watchpct as W
    src = inspect.getsource(W._push_move)
    assert "tokensec" in src
    assert "completeness(t, sec)" in src


def test_kline_failure_says_why():
    """限频和"这个池子没被索引"是两回事，都塌成"拿不到K线"，
    用户只会理解成"你的数据又缺了"。"""
    assert "限频" in OC.GT_WHY["429"]
    assert "索引" in OC.GT_WHY["404"]


def test_confirm_reports_the_reason(monkeypatch):
    from handlers import watchpct as W

    async def limited(*a, **k):
        return ([], "429") if k.get("why") else []
    monkeypatch.setattr(OC, "ohlcv", limited)
    ok, why = asyncio.run(W._confirm_onchain({"pct": 20, "chain": "bsc",
                                              "pool": "0xp"}, 1.0))
    assert ok is None and "限频" in why


def test_gt_throttle_settings_are_conservative():
    """免费额度突发卡得极死，最小间隔不能设太小。"""
    assert OC._GT_MIN_GAP >= 2.0
    assert OC._GT_RETRIES >= 1
