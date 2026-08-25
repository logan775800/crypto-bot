"""发币名查价的「完整信息」输出。

一次查询推送两条消息：
  1) 信息卡：价格/来源 + 市值·排名·成交量·多周期涨跌 + RSI(4h·1d) + 资金费率
             + 全市场主动买卖估算(Binance/OKX/Bitget/Bybit 四所现货齐全才显示)
  2) 蜡烛图(MA3/13/23 + 成交量) + 综合研判(趋势/动能/量能/强度)

数据源都是各交易所公开接口，无需鉴权。任一环节失败都优雅降级，不影响其余内容。
"""
import io
import asyncio
import logging
import httpx

from telegram import InputMediaPhoto

from api import get_market_data, get_fear_greed
from indicators import (rsi as _rsi, sma, macd_hist, dmi as _dmi, support_resistance,
                        rsi_divergence, early_reversal, volume_poc)
from handlers.util import escape_md, safe_reply

log = logging.getLogger(__name__)

# 四所现货 1 小时 K 线，覆盖最近 72 根 = 3 天；4h/1d/3d 窗口分别取末 4/24/72 根
_HOURS = 72
_WINDOWS = [("4h", 4), ("1d", 24), ("3d", 72)]


def _fmt_amt(v):
    """成交量(枚)格式：千分位 + 4 位小数，对齐截图风格。"""
    return f"{v:,.4f}"


# ---------- 各所现货 1h K 线，统一成 [{open, close, vol, buy, sell}, ...] 时间正序 ----------
async def _binance_klines(client, sym):
    """币安现货：有真实主动买量(takerBuyBase)，最精确。"""
    r = await client.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": f"{sym}USDT", "interval": "1h", "limit": _HOURS})
    r.raise_for_status()
    out = []
    for k in r.json():
        vol = float(k[5]); buy = float(k[9])          # 9 = taker buy base volume
        out.append({"open": float(k[1]), "close": float(k[4]),
                    "vol": vol, "buy": buy, "sell": max(vol - buy, 0.0)})
    return out


def _split_intrabar(h, l, c, vol):
    """无主动买卖字段的所：按收盘在当根振幅中的位置估算主动买卖强度。
    buy_ratio=(收-低)/(高-低)——收盘越靠近高点，主动买占比越大。比阴阳线一刀切更接近真实。"""
    rng = h - l
    if rng <= 0:
        return vol / 2, vol / 2
    buy_ratio = (c - l) / rng
    buy = vol * buy_ratio
    return buy, vol - buy


async def _okx_klines(client, sym):
    r = await client.get("https://www.okx.com/api/v5/market/candles",
                         params={"instId": f"{sym}-USDT", "bar": "1H", "limit": str(_HOURS)})
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "0" or not d.get("data"):
        raise RuntimeError("OKX 无数据")
    rows = list(reversed(d["data"]))   # OKX 最新在前 → 反转成正序
    out = []
    for k in rows:
        o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4]); vol = float(k[5])
        buy, sell = _split_intrabar(h, l, c, vol)
        out.append({"open": o, "close": c, "vol": vol, "buy": buy, "sell": sell})
    return out


async def _bybit_klines(client, sym):
    r = await client.get("https://api.bybit.com/v5/market/kline",
                         params={"category": "spot", "symbol": f"{sym}USDT",
                                 "interval": "60", "limit": _HOURS})
    r.raise_for_status()
    d = r.json()
    if d.get("retCode") != 0 or not d.get("result", {}).get("list"):
        raise RuntimeError("Bybit 无数据")
    rows = list(reversed(d["result"]["list"]))   # Bybit 最新在前 → 反转成正序
    out = []
    for k in rows:
        o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4]); vol = float(k[5])
        buy, sell = _split_intrabar(h, l, c, vol)
        out.append({"open": o, "close": c, "vol": vol, "buy": buy, "sell": sell})
    return out


async def _bitget_klines(client, sym):
    r = await client.get("https://api.bitget.com/api/v2/spot/market/candles",
                         params={"symbol": f"{sym}USDT", "granularity": "1h", "limit": str(_HOURS)})
    r.raise_for_status()
    d = r.json()
    if str(d.get("code")) != "00000" or not d.get("data"):
        raise RuntimeError("Bitget 无数据")
    rows = sorted(d["data"], key=lambda x: int(x[0]))   # 按时间正序
    out = []
    for k in rows:
        o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4]); vol = float(k[5])   # 5 = base volume
        buy, sell = _split_intrabar(h, l, c, vol)
        out.append({"open": o, "close": c, "vol": vol, "buy": buy, "sell": sell})
    return out


_EXCHANGES = [("Binance", _binance_klines), ("OKX", _okx_klines),
              ("Bitget", _bitget_klines), ("Bybit", _bybit_klines)]


async def flow_totals(symbol):
    """四所现货主动买卖聚合的**数字**，形如 {"4h_buy":…, "4h_sell":…, "1d_buy":…}。
    四所必须齐全，缺一所返回 None。

    拆出这一层是因为同一份数据有两个消费者：信息卡要排版成文字、信号引擎要拿它
    做资金面确认。各拉一遍等于把四家交易所的请求翻倍。"""
    sym = symbol.upper()
    results = {}
    async with httpx.AsyncClient(timeout=10) as client:
        fetched = await asyncio.gather(
            *[fn(client, sym) for _, fn in _EXCHANGES], return_exceptions=True)
    for (name, _), candles in zip(_EXCHANGES, fetched):
        if isinstance(candles, Exception) or not candles:
            log.info(f"[flow] {name} {sym} 不可用: {candles}")
            return None   # 四所齐全才显示
        results[name] = candles

    out = {}
    for label, n in _WINDOWS:
        buy = sell = 0.0
        for candles in results.values():
            for c in candles[-n:]:
                buy += c["buy"]; sell += c["sell"]
        out[f"{label}_buy"], out[f"{label}_sell"] = buy, sell
    return out


def flow_lines(totals):
    """把 flow_totals 的数字排版成信息卡里那几行。totals 为 None 时返回 None。"""
    if not totals:
        return None
    lines = ["全市场买卖估算(现货) 来源: Binance/OKX/Bitget/Bybit"]
    for label, _ in _WINDOWS:
        lines.append(f"{label}: 买入 {_fmt_amt(totals[f'{label}_buy'])} 枚  "
                     f"|  卖出 {_fmt_amt(totals[f'{label}_sell'])} 枚")
    return lines


async def build_flow_block(symbol):
    """四所现货主动买卖聚合，返回文本行(list[str])或 None。自己取数的老入口。"""
    return flow_lines(await flow_totals(symbol))


# ---------- RSI 4h / 1d ----------
async def _closes_binance(client, sym, interval, limit=120):
    try:
        r = await client.get("https://api.binance.com/api/v3/klines",
                             params={"symbol": f"{sym}USDT", "interval": interval, "limit": limit})
        r.raise_for_status()
        return [float(k[4]) for k in r.json()]
    except Exception:
        return None


async def _closes_okx(client, sym, bar, limit=120):
    try:
        r = await client.get("https://www.okx.com/api/v5/market/candles",
                             params={"instId": f"{sym}-USDT", "bar": bar, "limit": str(limit)})
        d = r.json()
        if d.get("code") == "0" and d.get("data"):
            return [float(k[4]) for k in reversed(d["data"])]
    except Exception:
        pass
    return None


async def build_rsi_multi(symbol):
    """4h 与 1d 的 RSI(14)。返回 (rsi_4h, rsi_1d)，取不到为 None。"""
    sym = symbol.upper()
    async with httpx.AsyncClient(timeout=10) as client:
        c4 = await _closes_binance(client, sym, "4h") or await _closes_okx(client, sym, "4H")
        c1 = await _closes_binance(client, sym, "1d") or await _closes_okx(client, sym, "1D")
    r4 = _rsi(c4, 14) if c4 and len(c4) > 15 else None
    r1 = _rsi(c1, 14) if c1 and len(c1) > 15 else None
    return r4, r1


# ---------- 合约深度：持仓量 OI(+24h变化) + 多空比（币安合约数据） ----------
async def build_oi_ls(symbol):
    """返回 {oi_usd, oi_chg, ls} 或 None。币安合约接口，取不到的字段留 None。"""
    sym = symbol.upper() + "USDT"
    oi_usd = oi_chg = ls = None
    async with httpx.AsyncClient(timeout=10) as client:
        # 持仓量 + 24h 变化：1h 粒度取 25 根，比较首尾（sumOpenInterestValue 为 USD 名义值）
        try:
            r = await client.get("https://fapi.binance.com/futures/data/openInterestHist",
                                 params={"symbol": sym, "period": "1h", "limit": 25})
            r.raise_for_status()
            hist = r.json()
            if isinstance(hist, list) and len(hist) >= 2:
                old = float(hist[0]["sumOpenInterestValue"])
                new = float(hist[-1]["sumOpenInterestValue"])
                oi_usd = new
                if old > 0:
                    oi_chg = (new - old) / old * 100
        except Exception:
            pass
        # 多空账户比
        try:
            r = await client.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                                 params={"symbol": sym, "period": "5m", "limit": 1})
            d = r.json()
            if isinstance(d, list) and d:
                ls = float(d[-1]["longShortRatio"])
        except Exception:
            pass
    if oi_usd is None and ls is None:
        return None
    return {"oi_usd": oi_usd, "oi_chg": oi_chg, "ls": ls}


_FNG_ZH = {
    "Extreme Fear": "极度恐惧", "Fear": "恐惧", "Neutral": "中性",
    "Greed": "贪婪", "Extreme Greed": "极度贪婪",
}


def _fmt_hours(h):
    """把小时数格式化：1/4/8 → '1h'；不足1小时 → '30分钟'。"""
    if h >= 1:
        return f"{h:g}h"
    return f"{int(round(h * 60))}分钟"


async def get_funding_interval(sym):
    """取合约资金费结算周期(小时)。Bybit fundingInterval(分钟)最准，回退 OKX 时间差。
    返回 {'hours': float, 'src': str} 或 None。1h 这类高频周期是踩坑重灾区。"""
    s = sym.upper()
    async with httpx.AsyncClient(timeout=8) as c:
        try:
            r = await c.get("https://api.bybit.com/v5/market/instruments-info",
                            params={"category": "linear", "symbol": f"{s}USDT"})
            d = r.json()
            lst = d.get("result", {}).get("list") or []
            if lst and lst[0].get("fundingInterval"):
                return {"hours": int(lst[0]["fundingInterval"]) / 60, "src": "Bybit"}
        except Exception:
            pass
        try:
            r = await c.get("https://www.okx.com/api/v5/public/funding-rate",
                            params={"instId": f"{s}-USDT-SWAP"})
            d = r.json()
            if d.get("code") == "0" and d.get("data"):
                row = d["data"][0]
                ft, nft = row.get("fundingTime"), row.get("nextFundingTime")
                if ft and nft and int(nft) > int(ft):
                    return {"hours": (int(nft) - int(ft)) / 3_600_000, "src": "OKX"}
        except Exception:
            pass
    return None


# ---------- 信息卡（消息 1） ----------
async def build_info_card(symbol, spot, spot_src, swap, swap_fr, swap_src, flow_pre=None):
    """组装完整信息卡文本。spot/swap 为 quick_price 已取到的行情，避免重复请求。"""
    sym = symbol.upper()
    lines = [f"💎 *{escape_md(sym)}*\n"]

    price = None
    if spot:
        price = spot["price"]
        e = "📈" if spot["change"] >= 0 else "📉"
        lines.append(f"{e} 现货: ${_fmt_price(price)} ({spot['change']:+.2f}%)")
    if swap:
        e2 = "📈" if swap["change"] >= 0 else "📉"
        fr = f" | 费率{swap_fr:+.3f}%" if swap_fr is not None else ""
        lines.append(f"{e2} 合约: ${_fmt_price(swap['price'])} ({swap['change']:+.2f}%){fr}")
        if price is None:
            price = swap["price"]
    lines.append(f"来源: {spot_src or swap_src or '—'}")

    # 市值 / RSI / 四所买卖聚合 / 合约深度 / 恐惧贪婪 五块并发拉取
    # flow_pre 是调用方已经取好的买卖聚合（send_full_detail 会取一次给两条消息共用）；
    # 没给才自己取，保证这个函数单独调用时行为不变。
    _flow_job = (asyncio.sleep(0, result=flow_lines(flow_pre)) if flow_pre is not None
                 else build_flow_block(sym))
    md_res, rsi_res, flow, oils, fng, fint = await asyncio.gather(
        get_market_data([sym]), build_rsi_multi(sym), _flow_job,
        build_oi_ls(sym), get_fear_greed(), get_funding_interval(sym),
        return_exceptions=True)

    # 市值/排名/成交量/多周期涨跌 + 24h高低 + ATH + 供应量/FDV（CoinGecko 同一接口）
    md = None
    if isinstance(md_res, dict):
        md = md_res.get(sym)
    elif isinstance(md_res, Exception):
        log.info(f"[card] {sym} 市值数据不可用: {md_res}")
    if md:
        lines.append("")
        lines.append(f"市值排名: #{md['market_cap_rank']}")
        lines.append(f"市值: ${md['market_cap']:,.0f}")
        lines.append(f"24h成交量: ${md['volume']:,.0f}")
        lines.append(f"涨跌幅: 24h: {md['change_24h']:+.2f}% | "
                     f"7d: {md['change_7d']:+.2f}% | 30d: {md['change_30d']:+.2f}%")
        if md.get("high_24h") and md.get("low_24h"):
            lines.append(f"24h高/低: ${_fmt_price(md['high_24h'])} / ${_fmt_price(md['low_24h'])}")
        if md.get("ath"):
            chg = f"（距ATH {md['ath_change']:+.1f}%）" if md.get("ath_change") is not None else ""
            lines.append(f"历史最高: ${_fmt_price(md['ath'])}{chg}")
        if md.get("circ_supply"):
            tot = f" / 总量 {md['total_supply']:,.0f}" if md.get("total_supply") else ""
            lines.append(f"供应量: 流通 {md['circ_supply']:,.0f}{tot} 枚")
        if md.get("fdv"):
            lines.append(f"FDV(完全稀释): ${md['fdv']:,.0f}")

    # RSI 4h / 1d
    r4, r1 = rsi_res if isinstance(rsi_res, tuple) else (None, None)
    if r4 is not None or r1 is not None:
        s4 = f"{r4:.0f}" if r4 is not None else "—"
        s1 = f"{r1:.0f}" if r1 is not None else "—"
        lines.append("")
        lines.append(f"RSI: 4h {s4} | 1d {s1}")

    # 资金费率（带结算周期：1h 这类高频 = 持仓成本高，重点提醒）
    if swap_fr is not None:
        ivl = ""
        if isinstance(fint, dict) and fint.get("hours"):
            h = fint["hours"]
            warn = " ⚠️高频吸血，持仓成本高" if h < 8 else ""
            ivl = f"（每{_fmt_hours(h)}结算{warn}）"
        lines.append(f"资金费率: {swap_fr:+.4f}% {swap_src or ''}".rstrip() + ivl)

    # 合约深度：持仓量 OI(24h变化) + 多空比
    if isinstance(oils, dict):
        if oils.get("oi_usd"):
            chg = f" (24h {oils['oi_chg']:+.1f}%)" if oils.get("oi_chg") is not None else ""
            lines.append(f"持仓量: ${oils['oi_usd']:,.0f}{chg}")
        if oils.get("ls") is not None:
            hint = "散户偏多" if oils["ls"] > 1 else "散户偏空"
            lines.append(f"多空比: {oils['ls']:.2f}（{hint}，常作反向参考）")

    # 恐惧贪婪指数（全局）
    if isinstance(fng, dict) and fng.get("value") is not None:
        zh = _FNG_ZH.get(fng.get("classification"), fng.get("classification", ""))
        lines.append(f"恐惧贪婪指数: {fng['value']}（{zh}）")

    # 全市场买卖估算（四所齐全才显示）
    if isinstance(flow, list) and flow:
        lines.append("")
        lines.extend(flow)

    return "\n".join(lines)


def _fmt_price(p):
    if p >= 1:
        return f"{p:,.2f}"
    elif p >= 0.01:
        return f"{p:.4f}"
    elif p >= 0.0001:
        return f"{p:.6f}"
    return f"{p:.8f}"


# ---------- 蜡烛图 + 综合研判（消息 2） ----------
# 多周期看图的顺序：宏观定大势 → 微观找入场。三张一起看才知道
# "日线的多头" 到底是周线趋势里的顺势，还是周线跌势里的一次反抽。
_TF_LABELS = [("1w", "周线"), ("1d", "日线"), ("4h", "4小时")]
# OKX 的周期名和币安不一样，取数时要翻译（写错不会报错，只会拿到另一个周期的图）
_OKX_BAR = {"1w": "1W", "1d": "1D", "4h": "4H", "1h": "1H"}


async def _ohlcv(symbol, interval="1d", limit=120):
    """某周期的 OHLCV：返回 [(ts_ms,o,h,l,c,vol), ...] 正序；先币安后 OKX。"""
    sym = symbol.upper()
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get("https://api.binance.com/api/v3/klines",
                                 params={"symbol": f"{sym}USDT", "interval": interval,
                                         "limit": limit})
            r.raise_for_status()
            return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                     float(k[4]), float(k[5])) for k in r.json()]
        except Exception:
            pass
        try:
            r = await client.get("https://www.okx.com/api/v5/market/candles",
                                 params={"instId": f"{sym}-USDT",
                                         "bar": _OKX_BAR.get(interval, "1D"),
                                         "limit": str(limit)})
            d = r.json()
            if d.get("code") == "0" and d.get("data"):
                return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                         float(k[4]), float(k[5])) for k in reversed(d["data"])]
        except Exception:
            pass
    return None


async def _daily_ohlcv(symbol, limit=120):
    """日线 OHLCV（`_ohlcv` 的老入口，保留给既有调用方）。"""
    return await _ohlcv(symbol, "1d", limit)


def _build_signal_text(o, h, l, c, v, closes_4h=None, flow=None):
    """由日线 OHLCV 生成综合研判。

    2026-08-25 大改：原来只有 MA/MACD/RSI/量能四项相加，**ADX 只印在屏幕上、
    一分不参与**。参考 `D:\\Scripts\\bit` 那个 Go 机器人补上了四层：

      1. ADX 当闸门 —— 震荡市(ADX<20)里顺势指标会被反复打脸，要降级；
      2. +DI/-DI 方向校验 —— 这条是**修 bug**，见下；
      3. 背离与早期反转预警 —— 在均线掉头之前降级；
      4. 量能中枢 POC / 资金面 —— 位置与资金的修正。

    **第 2 条修的是什么**：ADX 只测强度不带方向。一段猛跌里的逆势反抽同样能把
    ADX 顶到 80，而反抽那几根上 MA/MACD/RSI 全部翻多——老版本据此给出
    「买入·强」，而那恰恰是最容易被套的位置。判据钉在
    `tests/test_signal_factors.py::test_counter_trend_bounce_is_not_a_bull_signal`。

    closes_4h / flow 都可以为 None（取不到就少一层确认，不影响主结论）。
    """
    closes, highs, lows, vols = c, h, l, v
    opens = o
    last = closes[-1]
    # 均线口径全局统一成 MA3/13/23（annotchart.MA_PERIODS）——
    # 以前这张日线图是 MA7/25/99、标注图是 EMA20/50/200、破位扫描又是 3/13/23，
    # 同一个币在三个地方能得出三种"排列"，用户没法知道该信哪个。
    from handlers.annotchart import MA_PERIODS
    _P1, _P2, _P3 = MA_PERIODS
    ma_fast = sma(closes, _P1)
    ma_mid = sma(closes, _P2)
    ma_slow = sma(closes, _P3)
    ma_mid_prev = sma(closes[:-3], _P2) if len(closes) > _P2 + 3 else None
    mh = macd_hist(closes)
    r = _rsi(closes, 14)
    dm = _dmi(highs, lows, closes, 14)
    ax = dm["adx"] if dm else None
    div = rsi_divergence(closes)
    rev = early_reversal(opens, highs, lows, closes, closes_4h)
    poc = volume_poc(highs, lows, vols)

    # 量能：最近一根 vs 前若干根均量
    prior = vols[-6:-1] if len(vols) >= 6 else vols[:-1]
    avg_prior = sum(prior) / len(prior) if prior else (vols[-1] or 1)
    vol_ratio = vols[-1] / avg_prior if avg_prior else 1.0

    score = 0   # 多空计分
    parts = []  # 综合信号后缀原因

    # —— 趋势 ——
    if ma_fast and ma_mid:
        if ma_fast > ma_mid:
            score += 1
            trend_ma = f"MA{_P1}>MA{_P2} 短期偏强"
        else:
            score -= 1
            trend_ma = f"MA{_P1}<MA{_P2} 短期偏弱"
    else:
        trend_ma = "均线数据不足"
    if ma_mid and ma_mid_prev:
        if ma_mid > ma_mid_prev:
            trend_dir = f"MA{_P2} 上行"; score += 1
        else:
            trend_dir = f"MA{_P2} 下行"; score -= 1
    else:
        trend_dir = f"MA{_P2} 走平"
    trend_line = f"趋势：{trend_ma}，{trend_dir}"

    # —— 动能 ——
    if mh:
        rising = abs(mh["hist"]) > abs(mh["hist_prev"])
        if mh["hist"] > 0:
            momo = f"MACD 红柱{'走强' if rising else '走弱'}"
            score += 1 if rising else 0
        else:
            momo = f"MACD 绿柱{'走强' if rising else '走弱'}"
            score -= 1 if rising else 0
    else:
        momo = "MACD 数据不足"
    if r is not None:
        rtag = "超买" if r >= 70 else ("超卖" if r <= 30 else "中性")
        if r >= 70: score -= 1
        elif r <= 30: score += 1
        momo += f"，RSI {r:.0f}（{rtag}）"
    momo_line = f"动能：{momo}"

    # —— 量能 ——
    if vol_ratio >= 1.5:
        vol_desc = f"明显放量（近量约 {vol_ratio:.1f}× 前均量），参与度高"
        parts.append("放量")
    elif vol_ratio <= 0.7:
        vol_desc = f"温和缩量（近量约 {vol_ratio:.1f}× 前均量），参与度偏低，追随需防假动作"
        parts.append("缩量，参与度低、谨防假动作")
    else:
        vol_desc = f"量能正常（近量约 {vol_ratio:.1f}× 前均量）"
    vol_line = f"量能：{vol_desc}"

    # —— 强度（ADX 只测强弱，方向要看 DI）——
    if ax is not None:
        # 措辞里**不能出现"可顺势"**：ADX 只测强弱，顺哪个方向由下面的 DI 说了算。
        # 原来写"趋势较强，可顺势"，碰上逆势反抽就会一边写"可顺势"、
        # 一边在下面警告"别当趋势追"，自己跟自己打架。
        if ax >= 40:
            strg = f"ADX {ax:.0f}（趋势很强）"
        elif ax >= 25:
            strg = f"ADX {ax:.0f}（趋势成形）"
        elif ax >= 20:
            strg = f"ADX {ax:.0f}（趋势萌芽）"
        else:
            strg = f"ADX {ax:.0f}（无明显趋势，震荡为主）"
        if dm:
            di_word = "多头占优" if dm["pdi"] > dm["mdi"] else "空头占优"
            strg += f"｜+DI {dm['pdi']:.0f} / -DI {dm['mdi']:.0f} {di_word}"
    else:
        strg = "ADX 数据不足"
    strg_line = f"强度：{strg}"

    # —— 综合信号 ——
    # 背离优先级最高：它是反转信号，直接决定方向，不跟着顺势分数走。
    if div["bearish"]:
        head, base = "卖出信号", -1
    elif div["bullish"]:
        head, base = "买入信号", 1
    elif score >= 2:
        head, base = "买入信号", 1
    elif score <= -2:
        head, base = "卖出信号", -1
    else:
        head, base = "观望信号", 0

    levels = ["弱", "中", "强"]
    lvl = 2 if abs(score) >= 3 else (1 if abs(score) >= 2 else 0)
    warns = []

    # 量能不足时降一级——避免"缩量却给强信号"自相矛盾
    if vol_ratio <= 0.7:
        lvl = max(0, lvl - 1)

    if base != 0 and ax is not None:
        strong_trend = ax >= 25
        # ① 方向冲突：ADX 再高也不能顺着反抽喊多。这是整套改造里最要紧的一条。
        if dm and strong_trend:
            di_dir = 1 if dm["pdi"] > dm["mdi"] else -1
            if di_dir != base:
                lvl = 0
                warns.append("ADX 高但 DI 方向相反，多半是逆势反抽，别当趋势追")
        # ② 震荡市：顺势指标在横盘里会被反复打脸
        if ax < 20:
            lvl = max(0, lvl - 1)
            warns.append("ADX<20 无明显趋势，震荡里的方向信号可信度低")
        # ③ 多因子共振但趋势没确认——多半是趋势刚起。这时不能一边写"多头排列"
        #    一边喊"横盘震荡"自相矛盾，给弱信号并说清在等什么。
        elif not strong_trend and abs(score) >= 3:
            lvl = min(lvl, 1)
            warns.append("多因子共振但 ADX 未确认，或为趋势初起，等放量突破再确认")

    # ④ 早期反转预警：领先信号已共振指向反转，且和当前顺势方向相反 → 降级
    if (rev["top_risk"] and base > 0) or (rev["bottom_risk"] and base < 0):
        lvl = max(0, lvl - 1)
        warns.append("反转预警：" + " + ".join(rev["reasons"]))

    # ⑤ 资金面确认：喊反转却和近日资金流反着来，说明还没获得资金面配合
    if flow and base != 0:
        net = flow.get("1d_buy", 0) - flow.get("1d_sell", 0)
        if net and (1 if net > 0 else -1) != base:
            lvl = max(0, lvl - 1)
            warns.append("近一日资金流与该方向相反，尚未获资金面配合")

    # 「观望·弱」是句废话——观望不分强弱。只有买/卖才带强度档。
    suffix = f"（{('，'.join(parts))}）" if parts else ""
    if base == 0:
        signal_line = f"综合信号：观望{suffix}"
        # 观望最需要解释：ADX 很强、DI 又明确偏向一边时，光写"观望"看起来像判据坏了。
        if ax is not None and ax >= 25 and dm:
            side = "多头" if dm["pdi"] > dm["mdi"] else "空头"
            warns.append(f"趋势强度够（ADX {ax:.0f}、{side}占优），"
                         f"但均线/动能没同向共振，等它们跟上再谈方向")
    else:
        signal_line = f"综合信号：{head}·{levels[lvl]}{suffix}"

    # —— 关键位（近30根支撑/阻力 + 量能中枢）——
    sr = support_resistance(closes)
    sr_line = None
    if sr:
        sr_line = (f"关键位：阻力 ${_fmt_price(sr['resistance'])} "
                   f"| 支撑 ${_fmt_price(sr['support'])}（近30根）")
        if poc and last:
            # 带上距现价百分比：光给一个价格看不出远近，而 POC 的窗口（全量120根）
            # 和支撑阻力（近30根）不一样，并排放着容易被当成同一口径读。
            sr_line += f"\n量重心 POC：${_fmt_price(poc)}（{(poc / last - 1) * 100:+.0f}%，全量120根）"

    out = [signal_line, trend_line, momo_line, vol_line, strg_line]
    if sr_line:
        out.append(sr_line)

    # —— 反转扫描：**查过没命中也要印**（这一行是他要的）——
    # 只在命中时才显示的话，「这次没有背离」和「这机器人根本不看背离」
    # 在屏幕上完全一样，用的人无从判断。所以逐项打勾/打叉。
    if rev.get("checks") is not None:
        marks = {"顶背离": div["bearish"], "底背离": div["bullish"]}
        marks.update(rev["checks"])
        out.append("反转扫描：" + " ".join(
            f"{k}{'✅' if v else '✗'}" for k, v in marks.items()))
    else:
        out.append("反转扫描：日线不足 40 根，这轮没查")

    if div["text"]:
        out.append("⚠️ " + div["text"])
    # 贴近 POC：多空换手最密集，容易在这里反复纠缠
    if poc and last and abs(last - poc) / last <= 0.02:
        out.append("贴近量能中枢 POC，此处易反复")
    for w in warns:
        out.append("⚠️ " + w)
    return "\n".join(out)


def _render_candles(sym, interval, ohlcv):
    """把一段 OHLCV 画成蜡烛图 PNG。返回 (buf, 列数据字典) 或 None。

    列数据一并回传，是为了让调用方不用再把 DataFrame 拆一遍。"""
    try:
        import datetime
        import pandas as pd
        import mplfinance as mpf
        from handlers.annotchart import MA_PERIODS, apply_cjk
    except Exception as e:
        log.error(f"[chart] 绘图库缺失: {e}")
        return None

    idx = [datetime.datetime.utcfromtimestamp(row[0] / 1000) for row in ohlcv]
    df = pd.DataFrame(
        {"Open": [r[1] for r in ohlcv], "High": [r[2] for r in ohlcv],
         "Low": [r[3] for r in ohlcv], "Close": [r[4] for r in ohlcv],
         "Volume": [r[5] for r in ohlcv]},
        index=pd.DatetimeIndex(idx),
    )
    last = df["Close"].iloc[-1]
    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit",
                               wick="inherit", volume="in")
    style = mpf.make_mpf_style(**apply_cjk(dict(
        base_mpf_style="charles", marketcolors=mc,
        gridstyle=":", facecolor="white",
        # MA3黄/MA13蓝/MA23紫
        mavcolors=["#f5b800", "#2962ff", "#8e44ad"])))
    buf = io.BytesIO()
    try:
        mpf.plot(df, type="candle", mav=MA_PERIODS, volume=True, style=style,
                 title=f"[{sym}] {interval}  #{last:g}", figsize=(11, 6.5),
                 tight_layout=True, savefig=dict(fname=buf, dpi=90, format="png"))
    except Exception as e:
        log.error(f"[chart] {sym} {interval} 绘图失败: {e}")
        return None
    buf.seek(0)
    cols = {"o": list(df["Open"]), "h": list(df["High"]), "l": list(df["Low"]),
            "c": list(df["Close"]), "v": list(df["Volume"])}
    return buf, cols


async def build_signal_chart(symbol, flow=None):
    """日线蜡烛图 + 综合研判文案。返回 (buf, caption) 或 None。"""
    sym = symbol.upper()
    ohlcv = await _ohlcv(sym, "1d")
    if not ohlcv or len(ohlcv) < 30:
        return None
    # 4h 收盘价给早期反转预警的 L4 用（4h RSI 从超买/超卖回头，领先日线约一天）。
    # 取不到就少一层确认，不影响主结论。
    try:
        async with httpx.AsyncClient(timeout=10) as _c:
            closes_4h = await _closes_binance(_c, sym, "4h", 120)
    except Exception:
        closes_4h = None
    r = _render_candles(sym, "1d", ohlcv)
    if not r:
        return None
    buf, cols = r
    caption = (_build_signal_text(cols["o"], cols["h"], cols["l"], cols["c"], cols["v"],
                                  closes_4h=closes_4h, flow=flow)
               + "\n⚠️ 仅供参考，不构成投资建议")
    return buf, caption


async def build_multi_charts(symbol, flow=None):
    """周线 / 日线 / 4h 三张图 + 一段研判，合成一条相册消息。

    返回 ([(buf, 周期名), ...], caption)；一张都画不出来时返回 None。

    **为什么是三张而不是一张**：单看日线的"多头排列"分不清它是周线趋势里的顺势，
    还是周线跌势里的一次反抽——而这两种情况该做的事完全相反。
    宏观定大势(周线) → 微观找入场(4h)。

    **研判固定用日线算**：均线口径 MA3/13/23 是按日线定的语义，
    换个周期同一套阈值说的不是一回事。三张图只是给眼睛看的上下文。

    ⚠️ 相册消息**挂不了 inline 按钮**（Telegram 的限制），所以按钮只能留在
    前面那张信息卡上——调用方别把按钮传到这儿来。
    """
    sym = symbol.upper()
    fetched = await asyncio.gather(
        *[_ohlcv(sym, tf) for tf, _ in _TF_LABELS], return_exceptions=True)

    charts, daily_cols = [], None
    for (tf, label), ohlcv in zip(_TF_LABELS, fetched):
        if isinstance(ohlcv, Exception) or not ohlcv or len(ohlcv) < 30:
            log.info(f"[chart] {sym} {tf} 数据不足，这张跳过")
            continue
        r = _render_candles(sym, tf, ohlcv)
        if not r:
            continue
        buf, cols = r
        charts.append((buf, label))
        if tf == "1d":
            daily_cols = cols
    if not charts:
        return None

    if daily_cols:
        closes_4h = None
        try:
            async with httpx.AsyncClient(timeout=10) as _c:
                closes_4h = await _closes_binance(_c, sym, "4h", 120)
        except Exception:
            pass
        text = _build_signal_text(daily_cols["o"], daily_cols["h"], daily_cols["l"],
                                  daily_cols["c"], daily_cols["v"],
                                  closes_4h=closes_4h, flow=flow)
    else:
        # 日线没取到就只给图，不拿别的周期硬算研判——那会用错阈值
        text = "日线数据暂缺，本次只给图不给研判"

    got = "／".join(lb for _, lb in charts)
    caption = f"{got}（宏观定大势 → 微观找入场）\n{text}\n⚠️ 仅供参考，不构成投资建议"
    return charts, caption


# ---------- 对外总入口：发两条消息 ----------
async def send_full_detail(message, symbol, spot, spot_src, swap, swap_fr, swap_src, reply_markup=None):
    """由 quick_price 调用：先发信息卡，再发蜡烛图+研判。任一失败不影响另一条。"""
    sym = symbol.upper()
    # 买卖聚合取一次给两条消息共用：信息卡要排版成文字，信号引擎要拿它做资金面确认。
    # 各取各的等于把四家交易所的请求翻倍。取不到就两边都降级，不影响其余内容。
    try:
        flow = await flow_totals(sym)
    except Exception as e:
        log.info(f"[detail] {sym} 买卖聚合不可用: {e}")
        flow = None

    # do_quote=False：群里直接发普通消息，不引用用户那条币名消息
    try:
        card = await build_info_card(sym, spot, spot_src, swap, swap_fr, swap_src,
                                     flow_pre=flow)
        await safe_reply(message, card, reply_markup=reply_markup,
                         parse_mode="Markdown", do_quote=False)
    except Exception as e:
        log.error(f"[detail] {sym} 信息卡失败: {e}")

    # 周线/日线/4h 三张图合成一条相册。任何一步出岔都退回原来的单张日线图——
    # 相册是锦上添花，不能让它把"发币名有图看"这件事整个搞没。
    try:
        multi = await build_multi_charts(sym, flow=flow)
        if multi and len(multi[0]) >= 2:
            charts, caption = multi
            media = [InputMediaPhoto(media=buf,
                                     caption=caption if i == 0 else None)
                     for i, (buf, _lb) in enumerate(charts)]
            await message.reply_media_group(media=media, do_quote=False)
            return
        if multi:                      # 只画出一张，没必要发相册
            (buf, _lb), caption = multi[0][0], multi[1]
            await message.reply_photo(photo=buf, caption=caption, do_quote=False)
            return
    except Exception as e:
        log.error(f"[detail] {sym} 多周期相册失败，退回单图: {e}")

    try:
        chart = await build_signal_chart(sym, flow=flow)
        if chart:
            buf, caption = chart
            await message.reply_photo(photo=buf, caption=caption, do_quote=False)
    except Exception as e:
        log.error(f"[detail] {sym} 蜡烛图失败: {e}")
