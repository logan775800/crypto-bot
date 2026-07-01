"""币安(Binance)数据源，镜像 OKX 专区功能；也用于 OKX 查不到时的回退。
所有对外文本都标注 "(币安)" 以区分来源。
注意：币安 API 在部分地区(如中国大陆)可能被墙，连不上时各函数会优雅报错。
"""
import logging
import asyncio
import datetime
import httpx

SPOT = "https://api.binance.com"
FAPI = "https://fapi.binance.com"   # USDT 本位合约

LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")  # 杠杆代币，排除


async def _get(base, path, params=None):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base}{path}", params=params or {})
        resp.raise_for_status()
        return resp.json()


# ---------- 价格（供 quick_price 回退） ----------
async def get_price_binance(symbol):
    """现货 24h 行情，返回 {price, change, source}；查不到返回 None。"""
    sym = symbol.upper() + "USDT"
    try:
        d = await _get(SPOT, "/api/v3/ticker/24hr", {"symbol": sym})
        last = float(d["lastPrice"])
        ch = float(d["priceChangePercent"])
        if last > 0:
            return {"price": last, "change": ch, "source": "Binance"}
    except Exception:
        pass
    return None


async def get_swap_ticker_binance(symbol):
    """永续合约 24h 行情，返回 {price, change}；查不到返回 None。"""
    sym = symbol.upper() + "USDT"
    try:
        d = await _get(FAPI, "/fapi/v1/ticker/24hr", {"symbol": sym})
        last = float(d["lastPrice"])
        ch = float(d["priceChangePercent"])
        if last > 0:
            return {"price": last, "change": ch}
    except Exception:
        pass
    return None


async def get_funding_binance(symbol):
    """资金费率(百分比)，查不到返回 None。"""
    sym = symbol.upper() + "USDT"
    try:
        d = await _get(FAPI, "/fapi/v1/premiumIndex", {"symbol": sym})
        return float(d["lastFundingRate"]) * 100
    except Exception:
        return None


# ---------- 供按钮调用的文本版本（镜像 okx.py 的 build_*） ----------
async def build_gainers_text_bn(inst_type="SPOT"):
    try:
        if inst_type == "SWAP":
            data = await _get(FAPI, "/fapi/v1/ticker/24hr")
            title = "永续合约"
        else:
            data = await _get(SPOT, "/api/v3/ticker/24hr")
            title = "现货"
    except Exception as e:
        logging.error(f"币安涨幅榜出错: {e}")
        return "币安查询失败(可能网络不通)"

    coins = []
    for t in data:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if any(base.endswith(x) for x in LEV_SUFFIX):
            continue
        try:
            ch = float(t["priceChangePercent"])
            last = float(t["lastPrice"])
            if float(t.get("quoteVolume", 0)) < 100000:
                continue
            coins.append({"sym": base, "price": last, "change": ch})
        except (ValueError, KeyError):
            continue

    g = sorted(coins, key=lambda x: x["change"], reverse=True)[:15]
    lines = [f"🚀 *币安 {title} 24h涨幅榜*\n"]
    for i, c in enumerate(g, 1):
        lines.append(f"{i}. {c['sym']}: +{c['change']:.2f}%")
    if inst_type == "SPOT":
        l = sorted(coins, key=lambda x: x["change"])[:15]
        lines.append("\n📉 *跌幅榜*")
        for i, c in enumerate(l, 1):
            lines.append(f"{i}. {c['sym']}: {c['change']:.2f}%")
    lines.append("\n⚠️ 不构成投资建议")
    return "\n".join(lines)


async def build_funding_text_bn(symbol):
    rate = await get_funding_binance(symbol)
    if rate is None:
        return f"币安未找到 {symbol} 合约"
    hint = "偏多" if rate > 0 else "偏空"
    return f"💵 *{symbol} 永续合约* (币安)\n资金费率: {rate:+.4f}% ({hint})\n⚠️ 不构成投资建议"


async def build_ratio_text_bn(symbol):
    sym = symbol.upper() + "USDT"
    try:
        d = await _get(FAPI, "/futures/data/globalLongShortAccountRatio",
                       {"symbol": sym, "period": "5m", "limit": "1"})
        if not d:
            return f"币安未找到 {symbol} 多空比"
        ratio = float(d[-1]["longShortRatio"])
    except Exception:
        return f"币安未找到 {symbol} 多空比"
    hint = "散户偏多" if ratio > 1 else "散户偏空"
    return (f"⚖️ *{symbol} 多空比* (币安)\n多空账户比: {ratio:.2f} ({hint})\n"
            f"(散户情绪，常作反向参考)\n⚠️ 不构成投资建议")


async def build_fprice_text_bn(symbol):
    sym = symbol.upper() + "USDT"
    tk, pidx, oi = await asyncio.gather(
        _get(FAPI, "/fapi/v1/ticker/24hr", {"symbol": sym}),
        _get(FAPI, "/fapi/v1/premiumIndex", {"symbol": sym}),
        _get(FAPI, "/fapi/v1/openInterest", {"symbol": sym}),
        return_exceptions=True,
    )
    if not (isinstance(tk, dict) and tk.get("lastPrice")):
        return f"币安未找到 {symbol} 永续合约"
    last = float(tk["lastPrice"]); ch = float(tk["priceChangePercent"])
    high = float(tk["highPrice"]); low = float(tk["lowPrice"])
    emoji = "📈" if ch >= 0 else "📉"
    lines = [f"{emoji} *{symbol} 永续合约* (币安)\n",
             f"价格: ${last:,.4g} ({ch:+.2f}%)",
             f"24h高/低: ${high:,.4g} / ${low:,.4g}"]
    mark = None
    if isinstance(pidx, dict) and pidx.get("lastFundingRate"):
        rate = float(pidx["lastFundingRate"]) * 100
        lines.append(f"\n💵 资金费率: {rate:+.4f}% ({'偏多' if rate > 0 else '偏空'})")
        try:
            mark = float(pidx.get("markPrice") or 0)
        except (ValueError, TypeError):
            mark = None
    if isinstance(oi, dict) and oi.get("openInterest"):
        qty = float(oi["openInterest"])
        if mark:
            lines.append(f"📈 持仓量: ${qty * mark:,.0f}")
        else:
            lines.append(f"📈 持仓量: {qty:,.0f} {symbol}")
    lines.append("\n⚠️ 合约杠杆风险高，不构成投资建议")
    return "\n".join(lines)


async def build_new_text_bn():
    try:
        d = await _get(FAPI, "/fapi/v1/exchangeInfo")
    except Exception as e:
        logging.error(f"币安新币榜出错: {e}")
        return "币安查询失败(可能网络不通)"
    syms = [s for s in d.get("symbols", [])
            if s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL" and s.get("onboardDate")]
    syms.sort(key=lambda x: int(x["onboardDate"]), reverse=True)
    lines = ["🆕 *最近上线币安合约的新币*\n(币安现货无上线时间，用合约上线时间近似)\n"]
    now = datetime.datetime.now()
    for x in syms[:10]:
        base = x["baseAsset"]
        ld = datetime.datetime.fromtimestamp(int(x["onboardDate"]) / 1000)
        days = (now - ld).days
        ago = "今天" if days == 0 else ("昨天" if days == 1 else f"{days}天前")
        lines.append(f"• {base}/USDT - {ld.strftime('%m-%d')} ({ago})")
    lines.append("\n⚠️ 新币风险极高！不构成投资建议")
    return "\n".join(lines)


async def build_liq_text_bn(symbol):
    # 币安已关闭公开爆仓 REST 接口
    return "💥 币安未提供公开的爆仓数据接口。\n爆仓请用「🔥 OKX 专区 → 爆仓」查询。"
