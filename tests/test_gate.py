"""Gate 专区：镜像币安专区的七个功能。

下面所有假数据的字段名和量级都抄自 2026-08-14 对 Gate V4 的实探
（tools/probe_gate.py），不是照记忆编的——字段拼错在真机上表现为
「查询失败」或者一片 0，本地测试反而全绿，那是最没用的一种绿。

三条是这个模块真正的风险点，重点守：
  1. 919 个合约里 346 个是代币化股票（韩股居多，一天上几十个），
     不过滤的话涨幅榜/新币榜会被它们淹没；
  2. 持仓量要 张数 × 合约乘数 × 标记价 才是美元，直接拿 total_size 会差几万倍；
  3. 1h 结算的合约扣费是常规 8h 的 8 倍，这个必须在费率卡上说出来。
"""
import asyncio

import pytest

from handlers import gate as gt


@pytest.fixture(autouse=True)
def _no_cache():
    """两份元数据都有 1 小时缓存，测试之间必须清干净，否则先跑的把后跑的喂饱了。

    （不是 setdefault 式的「没有才建」——服务器上 pytest 是随机顺序跑的，
    留一点脏状态就会变成时挂时不挂的 flaky。）
    """
    for c in (gt._contracts_cache, gt._pairs_cache):
        c.update(at=0.0, data=None)
    yield
    for c in (gt._contracts_cache, gt._pairs_cache):
        c.update(at=0.0, data=None)


# ── 假数据（字段名照实探） ─────────────────────────────────────────
CONTRACTS = [
    {"name": "BTC_USDT", "contract_type": "", "in_delisting": False,
     "is_pre_market": False, "launch_time": 1600000000, "funding_interval": 28800,
     "quanto_multiplier": "0.0001", "funding_next_apply": 1786752000},
    {"name": "TUT_USDT", "contract_type": "", "in_delisting": False,
     "is_pre_market": False, "launch_time": 1786600000, "funding_interval": 3600,
     "quanto_multiplier": "1", "funding_next_apply": 1786752000},
    {"name": "NAVER_USDT", "contract_type": "stocks", "in_delisting": False,
     "is_pre_market": False, "launch_time": 1786700000, "funding_interval": 28800,
     "quanto_multiplier": "0.01"},
    {"name": "KODEX200_USDT", "contract_type": "stocks", "in_delisting": False,
     "is_pre_market": False, "launch_time": 1786700001, "funding_interval": 28800,
     "quanto_multiplier": "0.1"},
    {"name": "OLD_USDT", "contract_type": "", "in_delisting": True,
     "is_pre_market": False, "launch_time": 1786690000, "funding_interval": 3600,
     "quanto_multiplier": "1"},
    # 下面两个是给现货「尾 G 股票镜像」规则用的：SNDKG→SNDK 是股票，
    # 而 MOG→MO 也是股票（奥驰亚），但 MOG 本身是真币，靠名字放行
    {"name": "SNDK_USDT", "contract_type": "stocks", "in_delisting": False,
     "launch_time": 1786690000, "funding_interval": 28800, "quanto_multiplier": "1"},
    {"name": "MO_USDT", "contract_type": "stocks", "in_delisting": False,
     "launch_time": 1786690000, "funding_interval": 28800, "quanto_multiplier": "1"},
]

FUT_TICKERS = [
    {"contract": "BTC_USDT", "last": "62812.9", "change_percentage": "-1.12",
     "high_24h": "63975.3", "low_24h": "62669", "funding_rate": "0.000063",
     "funding_rate_indicative": "0.000070", "mark_price": "62814.71",
     "index_price": "62841.42", "total_size": "705372304",
     "volume_24h_settle": "2756389836"},
    {"contract": "TUT_USDT", "last": "0.033658", "change_percentage": "18.40",
     "high_24h": "0.04", "low_24h": "0.03", "funding_rate": "-0.000251",
     "mark_price": "0.0336", "index_price": "0.0337", "total_size": "1000",
     "volume_24h_settle": "900000"},
    {"contract": "NAVER_USDT", "last": "180", "change_percentage": "99.00",
     "mark_price": "180", "index_price": "180", "total_size": "10",
     "volume_24h_settle": "5000000"},          # 股票：涨得最猛也不该进榜
    {"contract": "DEAD_USDT", "last": "1", "change_percentage": "50.00",
     "mark_price": "1", "index_price": "1", "total_size": "1",
     "volume_24h_settle": "10"},               # 成交额太小，僵尸盘
]

SPOT_TICKERS = [
    {"currency_pair": "BTC_USDT", "last": "62842.4", "change_percentage": "-1.12",
     "quote_volume": "313305378"},
    {"currency_pair": "TUT_USDT", "last": "0.033", "change_percentage": "20.00",
     "quote_volume": "5000000"},
    {"currency_pair": "DUST_USDT", "last": "1", "change_percentage": "88.00",
     "quote_volume": "5000"},                  # 门槛以下，不该进榜
    {"currency_pair": "BTC_ETH", "last": "1", "change_percentage": "5.00",
     "quote_volume": "9999999"},               # 非 USDT 计价，不该进榜
]

STATS = [
    {"time": 1786700000 + i * 3600, "lsr_account": 2.13, "lsr_taker": 1.36,
     "top_lsr_account": 0.5, "open_interest_usd": 4425426554.0,
     "long_liq_usd": 1000.0 * i, "short_liq_usd": 10.0}
    for i in range(24)
]


SPOT_PAIRS = [
    {"base": "BTC", "base_name": "Bitcoin", "quote": "USDT"},
    {"base": "TUT", "base_name": "Tutorial", "quote": "USDT"},
    {"base": "SNDKG", "base_name": "SanDisk", "quote": "USDT"},        # 尾G股票镜像
    {"base": "QQQG", "base_name": "NASDAQ 100 Index ETF", "quote": "USDT"},
    {"base": "SOXLG", "base_name": "Direxion Daily Semiconductor Bull 3X ETF",
     "quote": "USDT"},
    {"base": "SKHYON", "base_name": "SK Hynix Ondo Tokenized", "quote": "USDT"},
    {"base": "MOG", "base_name": "Mog Coin", "quote": "USDT"},         # 真币，别误伤
    {"base": "USDY", "base_name": "Ondo US Dollar Yield", "quote": "USDT"},  # 同上
    {"base": "PAXG", "base_name": "PAX Gold", "quote": "USDT"},
]


def _routes(**overrides):
    """按 path 分发的假 _get。"""
    async def fake(path, params=None):
        params = params or {}
        if path in overrides:
            return overrides[path]
        if path == "/spot/currency_pairs":
            return SPOT_PAIRS
        if path == "/futures/usdt/contracts":
            return CONTRACTS
        if path == "/futures/usdt/tickers":
            c = params.get("contract")
            return [t for t in FUT_TICKERS if t["contract"] == c] if c else FUT_TICKERS
        if path == "/spot/tickers":
            c = params.get("currency_pair")
            return [t for t in SPOT_TICKERS if t["currency_pair"] == c] if c else SPOT_TICKERS
        if path == "/futures/usdt/contract_stats":
            return STATS[-int(params.get("limit", 24)):]
        raise AssertionError(f"没预料到的请求: {path}")
    return fake


def run(coro_fn, monkeypatch, **overrides):
    monkeypatch.setattr(gt, "_get", _routes(**overrides))
    return asyncio.run(coro_fn)


# ── 交易对拼装 ───────────────────────────────────────────────────
@pytest.mark.parametrize("raw,want", [
    ("BTC", "BTC_USDT"), ("btc", "BTC_USDT"), ("BTCUSDT", "BTC_USDT"),
    ("BTC/USDT", "BTC_USDT"), ("BTC-USDT", "BTC_USDT"), (" eth ", "ETH_USDT"),
])
def test_pair_normalises_every_caller_style(raw, want):
    """按钮给裸币名、实盘给 BTCUSDT、AI 给 BTC/USDT——拼成 BTCUSDTUSDT 就查不到了。"""
    assert gt._pair(raw) == want


def test_pair_keeps_short_names_ending_in_usdt():
    """别把本来就短的名字截秃了。"""
    assert gt._pair("USDT") == "USDT_USDT"


# ── 涨幅榜 ───────────────────────────────────────────────────────
def test_swap_gainers_exclude_tokenised_stocks(monkeypatch):
    """代币化股票占了三分之一，涨 99% 也不该出现在合约涨幅榜里。"""
    txt = run(gt.build_gainers_text_gt("SWAP"), monkeypatch)
    assert "TUT" in txt
    assert "NAVER" not in txt
    assert "代币化股票" in txt


def test_gainers_never_print_plus_minus(monkeypatch):
    """全市场翻绿那天涨幅榜第一名也是负的，写死加号会渲染成 "+-1.12%"。"""
    allred = [{"currency_pair": "AAA_USDT", "last": "1", "change_percentage": "-1.12",
               "quote_volume": "9000000"},
              {"currency_pair": "BBB_USDT", "last": "1", "change_percentage": "-8.40",
               "quote_volume": "9000000"}]
    txt = run(gt.build_gainers_text_gt("SPOT"), monkeypatch,
              **{"/spot/tickers": allred})
    assert "+-" not in txt
    assert "-1.12%" in txt


def test_gainers_drop_illiquid_pairs(monkeypatch):
    """成交额门槛：僵尸盘的涨幅是滑点，不是机会。"""
    swap = run(gt.build_gainers_text_gt("SWAP"), monkeypatch)
    assert "DEAD" not in swap
    spot = run(gt.build_gainers_text_gt("SPOT"), monkeypatch)
    assert "DUST" not in spot


def test_spot_gainers_only_usdt_quoted(monkeypatch):
    txt = run(gt.build_gainers_text_gt("SPOT"), monkeypatch)
    assert "BTC_ETH" not in txt and "ETH:" not in txt


def test_gainers_survive_network_failure(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("Gate 挂了")
    monkeypatch.setattr(gt, "_get", boom)
    assert "失败" in asyncio.run(gt.build_gainers_text_gt("SPOT"))


# ── 杠杆代币：现货榜第一版被它们霸榜，真机冒烟才看出来 ──────────────
@pytest.mark.parametrize("base", ["SNXX3L", "BEAT3S", "SOL5S", "MSTR3L",
                                  "BTCUP", "ETHDOWN", "XRPBULL", "LTCBEAR"])
def test_leveraged_tokens_are_recognised(base):
    """3L/3S/5L/5S 是 3倍/5倍 ETF：有磨损、会再平衡、长期归零，不是币。"""
    assert gt._is_leveraged(base)


@pytest.mark.parametrize("base", ["WELL3", "API3", "PEPE2", "SHIB2", "B3",
                                  "RSS3", "BTC", "1000SATS"])
def test_real_coins_ending_in_digits_are_not_mistaken(base):
    """必须是「数字+L/S」结尾才算；误伤 API3、PEPE2 这些真币就砸了自己的脚。"""
    assert not gt._is_leveraged(base)


def test_spot_gainers_drop_leveraged_tokens(monkeypatch):
    lev = SPOT_TICKERS + [{"currency_pair": "SNXX3L_USDT", "last": "1",
                           "change_percentage": "165.65", "quote_volume": "9000000"}]
    txt = run(gt.build_gainers_text_gt("SPOT"), monkeypatch,
              **{"/spot/tickers": lev})
    assert "SNXX3L" not in txt
    assert "杠杆代币" in txt


def test_spot_gainers_drop_tokenised_stocks(monkeypatch):
    """现货接口没有品类字段，靠合约的 contract_type 反查同名 base。"""
    withstock = SPOT_TICKERS + [{"currency_pair": "NAVER_USDT", "last": "180",
                                 "change_percentage": "99.0",
                                 "quote_volume": "9000000"}]
    txt = run(gt.build_gainers_text_gt("SPOT"), monkeypatch,
              **{"/spot/tickers": withstock})
    assert "NAVER" not in txt


# ── 现货股票代币三层识别（真机跑出来的三个坑） ─────────────────────
def _spot_extra(*bases):
    return SPOT_TICKERS + [{"currency_pair": f"{b}_USDT", "last": "1",
                            "change_percentage": "50.0",
                            "quote_volume": "9000000"} for b in bases]


def test_spot_drops_etf_named_tokens(monkeypatch):
    """QQQG「NASDAQ 100 Index ETF」这类名字里写明了的，一眼就该滤掉。"""
    txt = run(gt.build_gainers_text_gt("SPOT"), monkeypatch,
              **{"/spot/tickers": _spot_extra("QQQG", "SOXLG")})
    assert "QQQG" not in txt and "SOXLG" not in txt


def test_spot_drops_trailing_g_stock_mirrors(monkeypatch):
    """SNDKG=SanDisk：去掉尾 G 正好是合约里的股票代码。"""
    txt = run(gt.build_gainers_text_gt("SPOT"), monkeypatch,
              **{"/spot/tickers": _spot_extra("SNDKG")})
    assert "SNDKG" not in txt


def test_spot_drops_ondo_tokenised_stocks(monkeypatch):
    """SKHYON =「SK Hynix Ondo Tokenized」，第一版漏了它。"""
    txt = run(gt.build_gainers_text_gt("SPOT"), monkeypatch,
              **{"/spot/tickers": _spot_extra("SKHYON")})
    assert "SKHYON" not in txt


def test_tokenized_rule_does_not_eat_ondo_the_project(monkeypatch):
    """认的是 Tokenized 这个标记，不是 Ondo 这个项目名——USDY 是真币。"""
    txt = run(gt.build_gainers_text_gt("SPOT"), monkeypatch,
              **{"/spot/tickers": _spot_extra("USDY")})
    assert "USDY" in txt


def test_trailing_g_rule_does_not_eat_real_coins(monkeypatch):
    """MOG 是 Mog Coin，但 MO 是奥驰亚的股票代码——只靠尾 G 规则会把它误杀。"""
    txt = run(gt.build_gainers_text_gt("SPOT"), monkeypatch,
              **{"/spot/tickers": _spot_extra("MOG")})
    assert "MOG" in txt


def test_stock_filter_degrades_gracefully_without_pairs():
    """交易对接口挂了也不能让涨幅榜整个报废，只是滤得糙一点。"""
    meta = {c["name"]: c for c in CONTRACTS}
    assert "NAVER" in gt._noncrypto_spot_bases(meta, None)
    assert "SNDKG" not in gt._noncrypto_spot_bases(meta, None)


# ── 「查不到」和「网络挂了」不能混为一谈 ──────────────────────────
def _http400():
    import httpx as _h
    req = _h.Request("GET", "https://api.gateio.ws/x")
    return _h.HTTPStatusError("400", request=req, response=_h.Response(400, request=req))


@pytest.mark.parametrize("fn,word", [
    (gt.build_funding_text_gt, "合约"),
    (gt.build_ratio_text_gt, "多空比"),
    (gt.build_liq_text_gt, "爆仓"),
    (gt.build_fprice_text_gt, "永续合约"),
])
def test_unknown_coin_says_not_found_not_network_error(monkeypatch, fn, word):
    """Gate 对不存在的合约回 400。说成「网络不通」会让人去折腾网络而不是换币名。"""
    async def four_hundred(*_a, **_k):
        raise _http400()
    monkeypatch.setattr(gt, "_get", four_hundred)
    txt = asyncio.run(fn("NOTACOIN"))
    assert "未找到" in txt and "网络" not in txt


@pytest.mark.parametrize("fn", [gt.build_funding_text_gt, gt.build_ratio_text_gt,
                                gt.build_liq_text_gt, gt.build_fprice_text_gt])
def test_real_outage_still_says_network(monkeypatch, fn):
    """反过来也不能骗人：真断网时说「未找到」等于甩锅给用户。"""
    async def boom(*_a, **_k):
        raise RuntimeError("connect timeout")
    monkeypatch.setattr(gt, "_get", boom)
    assert "网络" in asyncio.run(fn("BTC"))


# ── 资金费率 ─────────────────────────────────────────────────────
def test_funding_flags_hourly_settlement(monkeypatch):
    """1h 结算是 8h 的 8 倍消耗——不说出来，拿一天才发现被扣光。"""
    txt = run(gt.build_funding_text_gt("TUT"), monkeypatch)
    assert "1h" in txt and "8 倍" in txt


def test_funding_stays_quiet_on_normal_interval(monkeypatch):
    txt = run(gt.build_funding_text_gt("BTC"), monkeypatch)
    assert "8h" in txt and "8 倍" not in txt


def test_funding_direction(monkeypatch):
    assert "偏多" in run(gt.build_funding_text_gt("BTC"), monkeypatch)
    assert "偏空" in run(gt.build_funding_text_gt("TUT"), monkeypatch)


def test_funding_missing_contract(monkeypatch):
    txt = run(gt.build_funding_text_gt("NOPE"), monkeypatch)
    assert "未找到" in txt


# ── 多空比 ───────────────────────────────────────────────────────
def test_ratio_shows_retail_and_whales(monkeypatch):
    """散户和大户背离时才有信息量，只给一个账户比等于没说。"""
    txt = run(gt.build_ratio_text_gt("BTC"), monkeypatch)
    assert "账户多空比" in txt and "大户账户比" in txt and "吃单多空比" in txt
    assert "2.13" in txt and "0.50" in txt


def test_ratio_handles_empty(monkeypatch):
    txt = run(gt.build_ratio_text_gt("BTC"), monkeypatch,
              **{"/futures/usdt/contract_stats": []})
    assert "未找到" in txt


# ── 爆仓（币安没有，Gate 有真数据） ───────────────────────────────
def test_liq_totals_the_last_24h(monkeypatch):
    txt = run(gt.build_liq_text_gt("BTC"), monkeypatch)
    longs = sum(1000.0 * i for i in range(24))
    assert f"${longs:,.0f}" in txt
    assert "多头被打得更狠" in txt


def test_liq_reports_quiet_period(monkeypatch):
    quiet = [{"time": 1786700000, "long_liq_usd": 0, "short_liq_usd": 0}]
    txt = run(gt.build_liq_text_gt("BTC"), monkeypatch,
              **{"/futures/usdt/contract_stats": quiet})
    assert "没有爆仓记录" in txt


def test_liq_does_not_claim_market_wide(monkeypatch):
    """只有 Gate 一家的数，别让人当成全市场爆仓。"""
    assert "仅 Gate" in run(gt.build_liq_text_gt("BTC"), monkeypatch)


# ── 合约行情 ─────────────────────────────────────────────────────
def test_fprice_converts_open_interest_to_usd(monkeypatch):
    """张数 × 合约乘数 × 标记价 才是美元。直接拿 total_size 会差几万倍。"""
    txt = run(gt.build_fprice_text_gt("BTC"), monkeypatch)
    want = 705372304 * 0.0001 * 62814.71        # ≈ 44.3 亿，与实探的 open_interest_usd 对得上
    assert f"${want:,.0f}" in txt


def test_fprice_marks_tokenised_stock(monkeypatch):
    txt = run(gt.build_fprice_text_gt("NAVER"), monkeypatch)
    assert "代币化股票" in txt


def test_fprice_small_price_not_scientific(monkeypatch):
    """Gate 全是小币，0.033 显示成 3.3e-02 没法看。"""
    txt = run(gt.build_fprice_text_gt("TUT"), monkeypatch)
    assert "0.033" in txt and "e-" not in txt


def test_fprice_missing_contract(monkeypatch):
    assert "未找到" in run(gt.build_fprice_text_gt("NOPE"), monkeypatch)


# ── 新币榜 ───────────────────────────────────────────────────────
def test_new_list_is_crypto_only_and_newest_first(monkeypatch):
    txt = run(gt.build_new_text_gt(), monkeypatch)
    assert "TUT" in txt
    assert "NAVER" not in txt and "KODEX200" not in txt   # 股票
    assert "OLD" not in txt                               # 退市中
    stocks = sum(1 for c in CONTRACTS if c["contract_type"])
    assert f"剔除 {stocks} 个" in txt                      # 数字从夹具算，别写死


def test_new_list_flags_unusual_settlement(monkeypatch):
    """新上的小币常是 1h 结算，进场前得知道。"""
    assert "1h结算" in run(gt.build_new_text_gt(), monkeypatch)


# ── 缓存 ─────────────────────────────────────────────────────────
def test_contracts_are_cached(monkeypatch):
    """1.2MB / 3 秒的元数据，不该每点一次按钮拉一遍。"""
    calls = []

    async def counting(path, params=None):
        calls.append(path)
        return CONTRACTS
    monkeypatch.setattr(gt, "_get", counting)
    asyncio.run(gt.contracts())
    asyncio.run(gt.contracts())
    assert len(calls) == 1


def test_cache_can_be_forced(monkeypatch):
    calls = []

    async def counting(path, params=None):
        calls.append(path)
        return CONTRACTS
    monkeypatch.setattr(gt, "_get", counting)
    asyncio.run(gt.contracts())
    asyncio.run(gt.contracts(force=True))
    assert len(calls) == 2


# ── 入口 ─────────────────────────────────────────────────────────
def test_gate_zone_is_reachable_by_button():
    """新功能必须有按钮入口，光有代码不算做完。

    2026-08-20 起四个交易所专区合并到「🏦 交易所专区」一个入口下
    （它们本来就是同一批功能的四份拷贝），所以判据从"在首页"改成"点得到"。
    """
    import inspect
    from handlers import menu
    cbs = [b.callback_data for row in menu.main_menu_kb().inline_keyboard for b in row]
    assert "cat_venues" in cbs
    seg = inspect.getsource(menu._dispatch).split('elif d == "cat_venues":')[1]
    assert '"cat_gate"' in seg.split("elif d ==")[0]


def test_gate_panel_mirrors_binance():
    """七个功能一个都不能少——这是「复制币安专区」的验收条件。"""
    import inspect
    src = inspect.getsource(gt)
    for fn in ("build_new_text_gt", "build_gainers_text_gt", "build_funding_text_gt",
               "build_ratio_text_gt", "build_liq_text_gt", "build_fprice_text_gt"):
        assert f"def {fn}" in src
    menu_src = inspect.getsource(__import__("handlers.menu", fromlist=["menu"]))
    for cb in ("gt_new", "gt_gainers", "gt_swap", "gt_funding_sel",
               "gt_ratio_sel", "gt_liq_sel", "gt_fprice_sel"):
        assert f'"{cb}"' in menu_src, f"{cb} 没接线"
