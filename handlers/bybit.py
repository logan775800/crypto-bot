"""Bybit 数据源，镜像 OKX/币安 专区功能（现货/合约行情、资金费率、涨跌榜、多空比、新币）。
数据来自 Bybit v5 公开接口（无需鉴权）。所有对外文本都标注 "(Bybit)" 以区分来源。
注意：Bybit API 在部分地区可能被墙，连不上时各函数会优雅报错。
"""
import re
import logging
import asyncio
import datetime
import httpx

BASE = "https://api.bybit.com"

# Bybit 现货杠杆代币形如 BTC3LUSDT / ETH3SUSDT，基名以 数字+L/S 结尾，涨跌榜里排除
_LEV_RE = re.compile(r"\d+[LS]$")


async def _get(path, params=None):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{BASE}{path}", params=params or {})
        resp.raise_for_status()
        d = resp.json()
    if d.get("retCode") != 0:
        raise RuntimeError(d.get("retMsg") or "Bybit 返回异常")
    return d.get("result", {})


# ---------- 价格（供 quick_price 回退） ----------
async def get_price_bybit(symbol):
    """现货 24h 行情，返回 {price, change, source}；查不到返回 None。"""
    sym = symbol.upper() + "USDT"
    try:
        r = await _get("/v5/market/tickers", {"category": "spot", "symbol": sym})
        lst = r.get("list") or []
        if lst:
            t = lst[0]
            last = float(t["lastPrice"])
            ch = float(t["price24hPcnt"]) * 100
            if last > 0:
                return {"price": last, "change": ch, "source": "Bybit"}
    except Exception:
        pass
    return None


async def get_swap_ticker_bybit(symbol):
    """永续合约 24h 行情，返回 {price, change}；查不到返回 None。"""
    sym = symbol.upper() + "USDT"
    try:
        r = await _get("/v5/market/tickers", {"category": "linear", "symbol": sym})
        lst = r.get("list") or []
        if lst:
            t = lst[0]
            last = float(t["lastPrice"])
            ch = float(t["price24hPcnt"]) * 100
            if last > 0:
                return {"price": last, "change": ch}
    except Exception:
        pass
    return None


async def get_funding_bybit(symbol):
    """资金费率(百分比)，查不到返回 None。"""
    sym = symbol.upper() + "USDT"
    try:
        r = await _get("/v5/market/tickers", {"category": "linear", "symbol": sym})
        lst = r.get("list") or []
        if lst and lst[0].get("fundingRate"):
            return float(lst[0]["fundingRate"]) * 100
    except Exception:
        pass
    return None


# ---------- 供按钮调用的文本版本（镜像 binance.py 的 build_*） ----------
async def build_gainers_text_by(inst_type="SPOT"):
    category = "linear" if inst_type == "SWAP" else "spot"
    title = "永续合约" if inst_type == "SWAP" else "现货"
    try:
        r = await _get("/v5/market/tickers", {"category": category})
    except Exception as e:
        logging.error(f"Bybit涨幅榜出错: {e}")
        return "Bybit查询失败(可能网络不通)"

    coins = []
    for t in r.get("list", []):
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if inst_type == "SPOT" and _LEV_RE.search(base):
            continue  # 排除杠杆代币
        try:
            last = float(t["lastPrice"])
            ch = float(t["price24hPcnt"]) * 100
            if float(t.get("turnover24h", 0)) < 100000:
                continue
            coins.append({"sym": base, "price": last, "change": ch})
        except (ValueError, KeyError):
            continue

    g = sorted(coins, key=lambda x: x["change"], reverse=True)[:15]
    lines = [f"🚀 *Bybit {title} 24h涨幅榜*\n"]
    for i, c in enumerate(g, 1):
        lines.append(f"{i}. {c['sym']}: +{c['change']:.2f}%")
    l = sorted(coins, key=lambda x: x["change"])[:15]
    lines.append(f"\n📉 *Bybit {title} 24h跌幅榜*")
    for i, c in enumerate(l, 1):
        lines.append(f"{i}. {c['sym']}: {c['change']:.2f}%")
    lines.append("\n⚠️ 不构成投资建议")
    return "\n".join(lines)


async def build_funding_text_by(symbol):
    rate = await get_funding_bybit(symbol)
    if rate is None:
        return f"Bybit未找到 {symbol} 合约"
    hint = "偏多" if rate > 0 else "偏空"
    return f"💵 *{symbol} 永续合约* (Bybit)\n资金费率: {rate:+.4f}% ({hint})\n⚠️ 不构成投资建议"


async def build_ratio_text_by(symbol):
    sym = symbol.upper() + "USDT"
    try:
        r = await _get("/v5/market/account-ratio",
                       {"category": "linear", "symbol": sym, "period": "1h", "limit": "1"})
        lst = r.get("list") or []
        if not lst:
            return f"Bybit未找到 {symbol} 多空比"
        item = lst[0]
        buy = float(item["buyRatio"])
        sell = float(item["sellRatio"])
        ratio = buy / sell if sell else 0
    except Exception:
        return f"Bybit未找到 {symbol} 多空比"
    hint = "散户偏多" if ratio > 1 else "散户偏空"
    return (f"⚖️ *{symbol} 多空比* (Bybit)\n多空账户比: {ratio:.2f} ({hint})\n"
            f"(散户情绪，常作反向参考)\n⚠️ 不构成投资建议")


async def build_fprice_text_by(symbol):
    sym = symbol.upper() + "USDT"
    try:
        r = await _get("/v5/market/tickers", {"category": "linear", "symbol": sym})
    except Exception as e:
        logging.error(f"Bybit合约行情出错: {e}")
        return f"Bybit未找到 {symbol} 永续合约"
    lst = r.get("list") or []
    if not lst:
        return f"Bybit未找到 {symbol} 永续合约"
    t = lst[0]
    try:
        last = float(t["lastPrice"])
        ch = float(t["price24hPcnt"]) * 100
        high = float(t["highPrice24h"])
        low = float(t["lowPrice24h"])
    except (ValueError, KeyError):
        return f"Bybit未找到 {symbol} 永续合约"
    emoji = "📈" if ch >= 0 else "📉"
    lines = [f"{emoji} *{symbol} 永续合约* (Bybit)\n",
             f"价格: ${last:,.4g} ({ch:+.2f}%)",
             f"24h高/低: ${high:,.4g} / ${low:,.4g}"]
    if t.get("fundingRate"):
        rate = float(t["fundingRate"]) * 100
        lines.append(f"\n💵 资金费率: {rate:+.4f}% ({'偏多' if rate > 0 else '偏空'})")
    if t.get("openInterestValue"):
        try:
            lines.append(f"📈 持仓量: ${float(t['openInterestValue']):,.0f}")
        except (ValueError, TypeError):
            pass
    lines.append("\n⚠️ 合约杠杆风险高，不构成投资建议")
    return "\n".join(lines)


async def build_new_text_by():
    try:
        r = await _get("/v5/market/instruments-info", {"category": "linear"})
    except Exception as e:
        logging.error(f"Bybit新币榜出错: {e}")
        return "Bybit查询失败(可能网络不通)"
    syms = [
        x for x in r.get("list", [])
        if x.get("quoteCoin") == "USDT" and x.get("contractType") == "LinearPerpetual"
        and x.get("launchTime") and int(x["launchTime"]) > 0
    ]
    syms.sort(key=lambda x: int(x["launchTime"]), reverse=True)
    lines = ["🆕 *最近上线 Bybit 合约的新币*\n(用合约上线时间近似)\n"]
    now = datetime.datetime.now()
    for x in syms[:10]:
        base = x["baseCoin"]
        ld = datetime.datetime.fromtimestamp(int(x["launchTime"]) / 1000)
        days = (now - ld).days
        ago = "今天" if days == 0 else ("昨天" if days == 1 else f"{days}天前")
        lines.append(f"• {base}/USDT - {ld.strftime('%m-%d')} ({ago})")
    lines.append("\n⚠️ 新币风险极高！不构成投资建议")
    return "\n".join(lines)


async def build_liq_text_by(symbol):
    # Bybit 无公开的历史爆仓 REST 接口（仅 websocket 实时推送）
    return "💥 Bybit 未提供公开的爆仓数据接口。\n爆仓请用「🔥 OKX 专区 → 爆仓」查询。"
