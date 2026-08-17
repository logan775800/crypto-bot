"""链上代币查询。

和交易所行情有个本质区别，这里的测试基本都围着它转：
**链上没有上币审核，同名假币是常态。**
实测搜 "PEPE" 返回 30 个交易对、跨 6 条链，接口默认顺序第一名是流动性
2.4 万的「Pepe in Hood」，而真 PEPE 的池子有 2048 万——差 800 倍。
把默认顺序直接给用户，等于把假币推到他面前。
"""
import asyncio

import pytest

from handlers import onchain as OC


# ── 真实响应样本（抄自 tools/probe 实测） ────────────────────────
def pair(sym, name, liq, chain="ethereum", addr="0xabc", chg=1.0, fdv=0,
         vol=0, buys=0, sells=0, created=None):
    return {
        "chainId": chain, "dexId": "uniswap", "priceUsd": "0.000002615",
        "baseToken": {"address": addr, "name": name, "symbol": sym},
        "liquidity": {"usd": liq}, "volume": {"h24": vol},
        "priceChange": {"h24": chg, "h1": 0.1}, "fdv": fdv,
        "txns": {"h24": {"buys": buys, "sells": sells}},
        "pairCreatedAt": created, "url": "https://dexscreener.com/x",
    }


class FakeResp:
    """限频层要看 status_code，所以假响应也得有。"""

    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params or {}))
        for key, payload in self.mapping.items():
            if key in url:
                return FakeResp(payload)
        raise AssertionError(f"没预料到的请求: {url}")


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    OC._cache.clear()
    monkeypatch.setattr(OC, "_GT_MIN_GAP", 0)      # 测试里不必真等限频间隔
    OC._gt_last[0] = 0.0
    yield
    OC._cache.clear()


@pytest.fixture
def client(monkeypatch):
    def apply(mapping):
        from handlers import source as S
        c = FakeClient(mapping)
        monkeypatch.setattr(S, "client", lambda: c)
        return c
    return apply


# ── 同名假币：这是整个模块存在的理由 ─────────────────────────────
def test_search_ranks_by_liquidity_not_api_order(client):
    """接口默认把山寨排前面。真 PEPE 池子 2048 万，山寨 2.4 万——必须按池子排。"""
    # 成交额按池子规模给：零成交的大池子在现实里就是假池（见下面 effective_liq 那组）
    client({"dexscreener.com": {"pairs": [
        pair("PEPE", "Pepe in Hood", 24_477, addr="0xfake", vol=5_000, buys=40, sells=30),
        pair("PEPE", "Pepe", 20_483_393, addr="0xreal", vol=800_000, buys=400, sells=350),
    ]}})
    items, total = asyncio.run(OC.by_name("PEPE"))
    assert items[0]["address"] == "0xreal"
    assert items[0]["liq"] > items[1]["liq"]
    assert total == 2


def test_search_dedupes_pools_of_the_same_token(client):
    """同一个代币有多个池子，只留流动性最大的那个，别在列表里刷屏。"""
    client({"dexscreener.com": {"pairs": [
        pair("PEPE", "Pepe", 1_000, addr="0xreal", vol=300, buys=20, sells=18),
        pair("PEPE", "Pepe", 9_000, addr="0xreal", vol=2_000, buys=60, sells=55),
        pair("PEPE", "Other", 500, addr="0xother", vol=100, buys=9, sells=8),
    ]}})
    items, total = asyncio.run(OC.by_name("PEPE"))
    assert total == 2
    assert items[0]["liq"] == 9_000


def test_same_token_on_different_chains_is_not_deduped(client):
    """同名同址跨链是两个东西（桥过去的和原生的），不能合并。"""
    client({"dexscreener.com": {"pairs": [
        pair("USDT", "Tether", 100, chain="ethereum", addr="0xaaa"),
        pair("USDT", "Tether", 200, chain="bsc", addr="0xaaa"),
    ]}})
    _items, total = asyncio.run(OC.by_name("USDT"))
    assert total == 2


def test_list_output_warns_about_impostors(client):
    client({"dexscreener.com": {"pairs": [
        pair("PEPE", "Pepe", 20_000_000, addr="0xreal", vol=900_000, buys=400, sells=380),
        pair("PEPE", "Pepe in Hood", 24_000, addr="0xfake", vol=5_000, buys=40, sells=35),
    ]}})
    items, total = asyncio.run(OC.by_name("PEPE"))
    txt = OC.render_list(items, "PEPE", total)
    assert "流动性" in txt
    assert "0xreal" in txt        # 合约地址必须给出来
    assert "⛔" in txt             # 池子 2.4 万的那个要标危险


def test_address_lookup_picks_the_deepest_pool(client):
    client({"dexscreener.com": {"pairs": [
        pair("PEPE", "Pepe", 100, addr="0xreal", vol=30, buys=5, sells=4),
        pair("PEPE", "Pepe", 9_000, addr="0xreal", vol=2_000, buys=60, sells=55),
    ]}})
    t, pools = asyncio.run(OC.by_address("0xreal"))
    assert t["liq"] == 9_000 and pools == 2


def test_address_not_found(client):
    client({"dexscreener.com": {"pairs": []}})
    t, pools = asyncio.run(OC.by_address("0x" + "0" * 40))
    assert t is None and pools == 0


# ── 地址识别 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("s,want", [
    ("0x6982508145454ce325ddbe47a25d4ec3d2311933", True),
    ("So11111111111111111111111111111111111111112", True),
    ("0x123", False), ("BTC", False), ("pepe", False), ("", False),
    ("0xZZZ2508145454ce325ddbe47a25d4ec3d2311933", False),
])
def test_address_detection(s, want):
    assert OC.is_address(s) is want


# ── 风险标注：链上的钱基本都亏在这几条上 ─────────────────────────
def test_shallow_pool_is_called_out():
    t = OC._pair(pair("X", "X", 3_000))
    assert any("池子" in r and "⛔" in r for r in OC.risks(t))


def test_new_pool_is_called_out():
    import time
    t = OC._pair(pair("X", "X", 1_000_000, created=int(time.time() * 1000) - 3600_000))
    assert any("新池" in r for r in OC.risks(t))


def test_fdv_far_above_liquidity_is_called_out():
    t = OC._pair(pair("X", "X", 100_000, fdv=100_000_000))
    assert any("盘子是空的" in r for r in OC.risks(t))


def test_wash_trading_pattern_is_called_out():
    t = OC._pair(pair("X", "X", 100_000, vol=10_000_000))
    assert any("刷量" in r for r in OC.risks(t))


def test_lopsided_buys_are_called_out():
    """买 500 笔卖 20 笔——先怀疑能不能卖出去。"""
    t = OC._pair(pair("X", "X", 1_000_000, buys=500, sells=20))
    assert any("卖出异常少" in r for r in OC.risks(t))


def test_healthy_token_has_no_noise():
    t = OC._pair(pair("X", "X", 20_000_000, fdv=100_000_000, vol=300_000,
                      buys=300, sells=250, chg=2.0))
    assert OC.risks(t) == []


# ── 标记：深度和涨幅要分开看 ─────────────────────────────────────
def test_pumped_token_is_not_marked_safe():
    """池子 39 万但 24h 涨 4750% —— 给绿标等于背书。真机上就出过这一幕。"""
    assert OC.flag(400_000, 4750) == "🚀"
    assert OC.flag(400_000, 5) == "✅"


def test_shallow_beats_pump_in_the_flag():
    """池子太浅是更硬的否决——涨多少都改变不了出不来这件事。"""
    assert OC.flag(3_000, 4750) == "⛔"


def test_pump_warning_in_the_card():
    t = OC._pair(pair("X", "X", 1_000_000, chg=900))
    assert any("接盘" in r for r in OC.risks(t))


# ── 链名：认不出必须拒绝，不能悄悄换一条链 ───────────────────────
@pytest.mark.parametrize("s,want", [
    ("solana", "sol"), ("SOL", "sol"), ("bnb", "bsc"), ("币安链", "bsc"),
    ("ethereum", "eth"), ("以太坊", "eth"), ("tron", "tron"),
    ("火币链", None), ("", None), ("随便写的", None),
])
def test_chain_aliases(s, want):
    assert OC.resolve_chain(s) == want


def test_trending_refuses_an_unknown_chain():
    """悄悄回落到默认链，用户会拿着 BNB 链的榜以为是 Solana 的。"""
    with pytest.raises(ValueError):
        asyncio.run(OC.trending("火币链"))


def test_trending_caches(client):
    c = client({"geckoterminal.com": {"data": []}})
    asyncio.run(OC.trending("bsc"))
    asyncio.run(OC.trending("bsc"))
    assert len(c.calls) == 1, "60 秒内该走缓存，免费额度只有 30 次/分钟"


def test_trending_parses_geckoterminal(client):
    client({"geckoterminal.com": {"data": [{
        "attributes": {"name": "MarsCoin / WBNB",
                       "base_token_price_usd": "0.00887",
                       "price_change_percentage": {"h24": "4477.1"},
                       "reserve_in_usd": "375641", "volume_usd": {"h24": "12345"}},
        "relationships": {"base_token": {"data": {"id": "bsc_0xabc"}}},
    }]}})
    items = asyncio.run(OC.trending("bsc"))
    assert items[0]["chg24"] == pytest.approx(4477.1)
    assert items[0]["liq"] == pytest.approx(375641)
    assert items[0]["address"] == "0xabc"


def test_trending_is_not_paid_promotion():
    """DexScreener 的 token-boosts 是付费推广位，拿它当热门榜等于推广告。"""
    import inspect
    src = inspect.getsource(OC)
    assert "token-boosts" not in src.replace("token-boosts 接口", "")


# ── 入口 ─────────────────────────────────────────────────────────
def test_button_is_routed():
    import inspect
    from handlers import menu
    assert 'startswith("oc:")' in inspect.getsource(menu._dispatch)


def test_command_registered():
    import inspect
    import bot
    src = inspect.getsource(bot.main)
    assert 'CommandHandler("onchain"' in src


def test_onchain_has_its_own_home_entry():
    """链上是**另一个市场**，和交易所行情并列，不该埋在机会扫描里。"""
    from handlers import menu
    cbs = [b.callback_data for row in menu.main_menu_kb().inline_keyboard
           for b in row]
    assert "cat_onchain" in cbs, "首页必须有链上入口"


def test_scan_panel_links_to_the_onchain_zone():
    from handlers import menu
    rows = menu.CATS["cat_scan"][1]
    cbs = [b.callback_data for row in rows for b in row]
    assert "cat_onchain" in cbs


def test_onchain_home_covers_the_whole_path():
    """查币 → 各链热门 → 我的监控，一条完整的路都要有按钮。"""
    cbs = [b.callback_data for row in OC.home_kb().inline_keyboard for b in row]
    assert "oc:ask" in cbs
    assert "oc:my" in cbs
    for k in OC.CHAINS:
        assert f"oc:t:{k}" in cbs


def test_onchain_zone_is_routed():
    import inspect
    from handlers import menu
    assert 'd == "cat_onchain"' in inspect.getsource(menu._dispatch)


def test_address_pasted_in_chat_is_handled():
    """直接发合约地址就该查——地址是无歧义的，落到查价分支只会说不支持。"""
    import inspect
    from handlers import quickprice
    assert "is_address" in inspect.getsource(quickprice.quick_price)


# ── 死池/假池：标称流动性是可以造的，成交量造不了 ─────────────────
# 实测搜 AKE，排第一的池子标称 2.2 亿美元，24h 只成交 4 美元、2 笔。
# 纯按标称流动性排序会把这种假池推到第一位——比不排序还糟，因为它看起来最可信。
DEAD = {"liq": 220_193_272, "vol24": 4, "buys": 1, "sells": 1, "chg24": 0.0}
LIVE = {"liq": 6_232_322, "vol24": 1_150_014, "buys": 30, "sells": 27, "chg24": 0.0}


def test_dead_pool_is_detected():
    assert OC.is_dead_pool(DEAD)
    assert not OC.is_dead_pool(LIVE)


def test_small_quiet_pool_is_not_called_dead():
    """小池子成交少很正常，这条规则只抓「大而空」。"""
    assert not OC.is_dead_pool({"liq": 8_000, "vol24": 3, "buys": 1, "sells": 0})


def test_effective_liquidity_discounts_unbacked_pools():
    """2.2 亿标称 / 4 美元成交 → 可信流动性只剩几百，排不到前面。"""
    assert OC.effective_liq(DEAD) < OC.effective_liq(LIVE)
    assert OC.effective_liq(DEAD) <= 4 * OC.LIQ_CREDIT


def test_effective_liquidity_keeps_real_pools_intact():
    """成交量足够时不该打折——否则刚建的真池会被埋掉。"""
    assert OC.effective_liq(LIVE) == LIVE["liq"]


def test_search_ranks_dead_pool_below_real_one(client):
    client({"dexscreener.com": {"pairs": [
        pair("AKE", "AKEDO", 220_193_272, chain="solana", addr="Fake",
             vol=4, buys=1, sells=1),
        pair("AKE", "AKEDO", 6_232_322, chain="solana", addr="Real",
             vol=1_150_014, buys=30, sells=27),
    ]}})
    items, _total = asyncio.run(OC.by_name("AKE"))
    assert items[0]["address"] == "Real", "标称 2.2 亿的空池不该排第一"


def test_address_lookup_skips_the_dead_pool(client):
    """同一个代币下也会有大而空的池子，选主池时同样要看成交。"""
    client({"dexscreener.com": {"pairs": [
        pair("X", "X", 99_000_000, addr="0xa", vol=2, buys=1, sells=0),
        pair("X", "X", 500_000, addr="0xa", vol=400_000, buys=80, sells=70),
    ]}})
    t, _pools = asyncio.run(OC.by_address("0xa"))
    assert t["liq"] == 500_000


def test_dead_pool_flag_and_warning():
    t = OC._pair(pair("X", "X", 220_193_272, vol=4, buys=1, sells=1))
    assert OC.flag_of(t) == "💀"
    assert any("不可信" in r for r in OC.risks(t))


# ── 列表要能点进去 + K线 ─────────────────────────────────────────
# 用户实测反馈：「看不到当前价格啊还有k线图之类的 你显示给我的这个是啥」——
# 搜索结果只是一串文字，点不进去，要看详情得手工复制地址再发一遍。
# 那段手工搬运正是「看完就算了」的原因（分析闭环那次已经吃过一回教训）。
def test_search_list_has_a_button_per_result(client):
    client({"dexscreener.com": {"pairs": [
        pair("A", "AA", 1_000_000, chain="bsc", addr="0xa", vol=90_000,
             buys=50, sells=45),
        pair("B", "BB", 500_000, chain="solana", addr="Sol1", vol=50_000,
             buys=40, sells=38),
    ]}})
    items, _t = asyncio.run(OC.by_name("x"))
    cbs = [b.callback_data for row in OC.list_kb(items).inline_keyboard
           for b in row if b.callback_data]
    assert "oc:d:bsc:0xa" in cbs
    assert "oc:d:sol:Sol1" in cbs


def test_list_button_callbacks_fit_telegram_limit(client):
    """Solana 地址 44 字符，加前缀不能超 64 字节。"""
    long_sol = "djN5QdTLZGoCNwa2Q2BqKNKaZHoKn4J6BSZzWxVpump"
    client({"dexscreener.com": {"pairs": [
        pair("X", "X", 1_000_000, chain="solana", addr=long_sol, vol=90_000,
             buys=50, sells=45)]}})
    items, _t = asyncio.run(OC.by_name("x"))
    for row in OC.list_kb(items).inline_keyboard:
        for b in row:
            if b.callback_data:
                assert len(b.callback_data.encode()) <= 64


def test_detail_card_leads_to_charts():
    t = OC._pair(pair("X", "X", 1_000_000, chain="bsc", addr="0xa"))
    t["pool"] = "0xpool"
    cbs = [b.callback_data for row in OC.detail_kb(t).inline_keyboard
           for b in row if b.callback_data]
    assert any(c.startswith("oc:k:bsc:0xpool:") for c in cbs)


def test_price_is_prominent_in_the_card():
    """用户第一反应是「看不到当前价格」——价格必须一眼看见。"""
    t = OC._pair(pair("X", "X", 1_000_000, chain="bsc"))
    assert "当前价" in OC.render_token(t)


def test_price_is_labelled_in_the_list():
    t = OC._pair(pair("X", "X", 1_000_000, chain="bsc"))
    assert "现价" in OC.render_list([t], "X", 1)


def test_chart_needs_the_pool_address_not_the_token():
    """GeckoTerminal 的 OHLCV 按池子给，拿代币地址去请求只会 404。"""
    assert asyncio.run(OC.ohlcv("bsc", "")) == []


def test_ohlcv_is_sorted_oldest_first(client):
    """接口给的是新→旧，指标和画图都要旧→新。"""
    client({"geckoterminal.com": {"data": {"attributes": {"ohlcv_list": [
        [1786950000, 2, 2, 1, 1.5, 100],
        [1786946400, 1, 1.2, 0.9, 1.0, 80],
    ]}}}})
    rows = asyncio.run(OC.ohlcv("bsc", "0xpool", "1h"))
    assert [r[0] for r in rows] == [1786946400_000, 1786950000_000]
    assert rows[-1][4] == 1.5


def test_ohlcv_rejects_unknown_chain():
    assert asyncio.run(OC.ohlcv("火币链", "0xpool")) == []


def test_chart_title_falls_back_to_ascii_without_a_cjk_font(monkeypatch):
    """没装中文字体时，「牛来」硬画出来是一排豆腐块——退回合约地址开头。
    （镜像里现在装了 fonts-noto-cjk，但本地/旧镜像可能没有，两条路都要对。）"""
    from handlers import annotchart as A
    monkeypatch.setitem(A._CJK, "checked", True)
    monkeypatch.setitem(A._CJK, "name", None)
    t = {"symbol": "牛来", "address": "0xBEEA1D618e533a387D941F58a7d4c9b7bD377777",
         "chain": "bsc"}
    title = OC._ascii_title(t, "1h")
    assert title.isascii()
    assert "0xBEEA1D61" in title


def test_chart_title_uses_the_real_name_when_the_font_exists(monkeypatch):
    from handlers import annotchart as A
    monkeypatch.setitem(A._CJK, "checked", True)
    monkeypatch.setitem(A._CJK, "name", "Noto Sans CJK SC")
    t = {"symbol": "牛来", "address": "0xa", "chain": "bsc"}
    assert "牛来" in OC._ascii_title(t, "1h")


def test_chart_title_keeps_ascii_symbols():
    t = {"symbol": "PEPE", "address": "0xa", "chain": "eth"}
    assert "PEPE" in OC._ascii_title(t, "4h")
