"""发币名查价的「完整信息」输出。

一次查询推送两条消息：
  1) 信息卡：价格/来源 + 市值·排名·成交量·多周期涨跌 + RSI(4h·1d) + 资金费率
             + 全市场主动买卖估算(Binance/OKX/Bitget/Bybit 四所现货齐全才显示)
  2) 蜡烛图(MA7/25/99 + 成交量) + 综合研判(趋势/动能/量能/强度)

数据源都是各交易所公开接口，无需鉴权。任一环节失败都优雅降级，不影响其余内容。
"""
import io
import asyncio
import logging
import httpx

from api import get_market_data
from indicators import rsi as _rsi, sma, macd_hist, adx as _adx
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


def _split_by_direction(o, c, vol):
    """无主动买卖字段的所：按 K 线方向估算(阳线记买、阴线记卖)。"""
    if c >= o:
        return vol, 0.0
    return 0.0, vol


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
        o = float(k[1]); c = float(k[4]); vol = float(k[5])
        buy, sell = _split_by_direction(o, c, vol)
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
        o = float(k[1]); c = float(k[4]); vol = float(k[5])
        buy, sell = _split_by_direction(o, c, vol)
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
        o = float(k[1]); c = float(k[4]); vol = float(k[5])   # 5 = base volume
        buy, sell = _split_by_direction(o, c, vol)
        out.append({"open": o, "close": c, "vol": vol, "buy": buy, "sell": sell})
    return out


_EXCHANGES = [("Binance", _binance_klines), ("OKX", _okx_klines),
              ("Bitget", _bitget_klines), ("Bybit", _bybit_klines)]


async def build_flow_block(symbol):
    """四所现货主动买卖聚合。按用户要求：四所必须齐全，缺一所则整块不显示。
    返回文本行(list[str])或 None。"""
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

    lines = ["全市场买卖估算(现货) 来源: Binance/OKX/Bitget/Bybit"]
    for label, n in _WINDOWS:
        buy = sell = 0.0
        for candles in results.values():
            for c in candles[-n:]:
                buy += c["buy"]; sell += c["sell"]
        lines.append(f"{label}: 买入 {_fmt_amt(buy)} 枚  |  卖出 {_fmt_amt(sell)} 枚")
    return lines


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


# ---------- 信息卡（消息 1） ----------
async def build_info_card(symbol, spot, spot_src, swap, swap_fr, swap_src):
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

    # 市值 / RSI(4h·1d) / 四所买卖聚合 三块并发拉取
    md_res, rsi_res, flow = await asyncio.gather(
        get_market_data([sym]), build_rsi_multi(sym), build_flow_block(sym),
        return_exceptions=True)

    # 市值/排名/成交量/多周期涨跌（CoinGecko）
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

    # RSI 4h / 1d
    r4, r1 = rsi_res if isinstance(rsi_res, tuple) else (None, None)
    if r4 is not None or r1 is not None:
        s4 = f"{r4:.0f}" if r4 is not None else "—"
        s1 = f"{r1:.0f}" if r1 is not None else "—"
        lines.append("")
        lines.append(f"RSI: 4h {s4} | 1d {s1}")

    # 资金费率
    if swap_fr is not None:
        lines.append(f"资金费率: {swap_fr:+.4f}% {swap_src or ''}".rstrip())

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
async def _daily_ohlcv(symbol, limit=120):
    """日线 OHLCV：返回 [(ts_ms,o,h,l,c,vol), ...] 正序；先币安后 OKX。"""
    sym = symbol.upper()
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get("https://api.binance.com/api/v3/klines",
                                 params={"symbol": f"{sym}USDT", "interval": "1d", "limit": limit})
            r.raise_for_status()
            return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                     float(k[4]), float(k[5])) for k in r.json()]
        except Exception:
            pass
        try:
            r = await client.get("https://www.okx.com/api/v5/market/candles",
                                 params={"instId": f"{sym}-USDT", "bar": "1D", "limit": str(limit)})
            d = r.json()
            if d.get("code") == "0" and d.get("data"):
                return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                         float(k[4]), float(k[5])) for k in reversed(d["data"])]
        except Exception:
            pass
    return None


def _build_signal_text(o, h, l, c, v):
    """由日线 OHLCV 生成 综合信号/趋势/动能/量能/强度 五行研判。"""
    closes, highs, lows, vols = c, h, l, v
    last = closes[-1]
    ma7 = sma(closes, 7)
    ma25 = sma(closes, 25)
    ma99 = sma(closes, 99)
    ma25_prev = sma(closes[:-3], 25) if len(closes) > 28 else None
    mh = macd_hist(closes)
    r = _rsi(closes, 14)
    ax = _adx(highs, lows, closes, 14)

    # 量能：最近一根 vs 前若干根均量
    prior = vols[-6:-1] if len(vols) >= 6 else vols[:-1]
    avg_prior = sum(prior) / len(prior) if prior else (vols[-1] or 1)
    vol_ratio = vols[-1] / avg_prior if avg_prior else 1.0

    score = 0   # 多空计分
    parts = []  # 综合信号后缀原因

    # —— 趋势 ——
    if ma7 and ma25:
        if ma7 > ma25:
            score += 1
            trend_ma = "MA7>MA25 短期偏强"
        else:
            score -= 1
            trend_ma = "MA7<MA25 短期偏弱"
    else:
        trend_ma = "均线数据不足"
    if ma25 and ma25_prev:
        if ma25 > ma25_prev:
            trend_dir = "MA25 上行"; score += 1
        else:
            trend_dir = "MA25 下行"; score -= 1
    else:
        trend_dir = "MA25 走平"
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

    # —— 强度 ——
    if ax is not None:
        if ax >= 40:
            strg = f"ADX {ax:.0f}（趋势较强，可顺势）"
        elif ax >= 25:
            strg = f"ADX {ax:.0f}（趋势成形）"
        elif ax >= 20:
            strg = f"ADX {ax:.0f}（趋势萌芽）"
        else:
            strg = f"ADX {ax:.0f}（无明显趋势，震荡为主）"
    else:
        strg = "ADX 数据不足"
    strg_line = f"强度：{strg}"

    # —— 综合信号 ——
    if score >= 2:
        head = "买入信号"
    elif score <= -2:
        head = "卖出信号"
    else:
        head = "观望信号"
    strength = "强" if abs(score) >= 3 else ("中" if abs(score) >= 2 else "弱")
    suffix = f"（{('，'.join(parts))}）" if parts else ""
    signal_line = f"综合信号：{head}·{strength}{suffix}"

    return "\n".join([signal_line, trend_line, momo_line, vol_line, strg_line])


async def build_signal_chart(symbol):
    """生成蜡烛图(MA7/25/99+量) 并附综合研判文案。返回 (buf, caption) 或 None。"""
    ohlcv = await _daily_ohlcv(symbol)
    if not ohlcv or len(ohlcv) < 30:
        return None
    try:
        import datetime
        import pandas as pd
        import mplfinance as mpf
    except Exception as e:
        log.error(f"[chart] 绘图库缺失: {e}")
        return None

    sym = symbol.upper()
    idx = [datetime.datetime.utcfromtimestamp(row[0] / 1000) for row in ohlcv]
    df = pd.DataFrame(
        {"Open": [r[1] for r in ohlcv], "High": [r[2] for r in ohlcv],
         "Low": [r[3] for r in ohlcv], "Close": [r[4] for r in ohlcv],
         "Volume": [r[5] for r in ohlcv]},
        index=pd.DatetimeIndex(idx),
    )
    # 只画最近 ~120 根，均线用全量算好再截可保证 MA99 有值；这里数据本就是最近 120 根
    last = df["Close"].iloc[-1]
    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit",
                               wick="inherit", volume="in")
    style = mpf.make_mpf_style(base_mpf_style="charles", marketcolors=mc,
                               gridstyle=":", facecolor="white")
    buf = io.BytesIO()
    try:
        mpf.plot(df, type="candle", mav=(7, 25, 99), volume=True, style=style,
                 title=f"[{sym}] 1d  #{last:g}", figsize=(11, 6.5),
                 tight_layout=True, savefig=dict(fname=buf, dpi=90, format="png"))
    except Exception as e:
        log.error(f"[chart] {sym} 绘图失败: {e}")
        return None
    buf.seek(0)

    o = list(df["Open"]); h = list(df["High"]); l = list(df["Low"])
    c = list(df["Close"]); v = list(df["Volume"])
    caption = _build_signal_text(o, h, l, c, v) + "\n⚠️ 仅供参考，不构成投资建议"
    return buf, caption


# ---------- 对外总入口：发两条消息 ----------
async def send_full_detail(message, symbol, spot, spot_src, swap, swap_fr, swap_src, reply_markup=None):
    """由 quick_price 调用：先发信息卡，再发蜡烛图+研判。任一失败不影响另一条。"""
    sym = symbol.upper()
    try:
        card = await build_info_card(sym, spot, spot_src, swap, swap_fr, swap_src)
        await safe_reply(message, card, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        log.error(f"[detail] {sym} 信息卡失败: {e}")

    try:
        chart = await build_signal_chart(sym)
        if chart:
            buf, caption = chart
            await message.reply_photo(photo=buf, caption=caption)
    except Exception as e:
        log.error(f"[detail] {sym} 蜡烛图失败: {e}")
