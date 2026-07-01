import logging
import httpx
from telegram import Update
from telegram.ext import ContextTypes

OKX_BASE = "https://www.okx.com"


def funding_hint(rate):
    """资金费率通俗解读。rate是百分比数值（如-0.442）"""
    if rate > 0:
        base = "正费率：做多的人多，多头付钱给空头"
    elif rate < 0:
        base = "负费率：做空的人多，空头付钱给多头"
    else:
        base = "费率持平，多空均衡"
    # 极端值提示
    if abs(rate) >= 0.1:
        if rate > 0:
            extra = "⚠️ 费率偏高，多头拥挤，警惕回调插针"
        else:
            extra = "⚠️ 费率偏负，空头拥挤，警惕反弹逼空"
        return f"{base}\n{extra}"
    return base

async def _okx_get(path, params=None):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{OKX_BASE}{path}", params=params or {})
        resp.raise_for_status()
        return resp.json()

# /funding BTC - 资金费率
async def funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0].upper() if context.args else "BTC"
    inst = f"{symbol}-USDT-SWAP"
    try:
        d = await _okx_get("/api/v5/public/funding-rate", {"instId": inst})
        if d["code"] != "0" or not d["data"]:
            await update.message.reply_text(f"未找到 {symbol} 的合约")
            return
        item = d["data"][0]
        rate = float(item["fundingRate"]) * 100
        await update.message.reply_text(
            f"💵 {symbol}-USDT 永续合约\n\n"
            f"资金费率: {rate:+.4f}%\n"
            f"{funding_hint(rate)}\n\n"
            f"💡 每8小时结算一次，是多空双方互相付的钱\n"
            f"⚠️ 不构成投资建议"
        )
    except Exception as e:
        logging.error(f"资金费率出错: {e}")
        await update.message.reply_text("查询失败")

# /oi BTC - 持仓量
async def open_interest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0].upper() if context.args else "BTC"
    inst = f"{symbol}-USDT-SWAP"
    try:
        d = await _okx_get("/api/v5/public/open-interest", {"instId": inst})
        if d["code"] != "0" or not d["data"]:
            await update.message.reply_text(f"未找到 {symbol} 的合约")
            return
        item = d["data"][0]
        oi_usd = float(item["oiUsd"])
        oi_ccy = float(item["oiCcy"])
        await update.message.reply_text(
            f"📊 {symbol}-USDT 永续合约持仓量\n\n"
            f"未平仓: {oi_ccy:,.0f} {symbol}\n"
            f"价值: ${oi_usd:,.0f}\n\n"
            f"(持仓量大=市场参与度高、博弈激烈)\n"
            f"⚠️ 不构成投资建议"
        )
    except Exception as e:
        logging.error(f"持仓量出错: {e}")
        await update.message.reply_text("查询失败")

# /okxk BTC - OKX K线数据（最近几根）
async def okx_kline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0].upper() if context.args else "BTC"
    inst = f"{symbol}-USDT"
    try:
        d = await _okx_get("/api/v5/market/candles", {"instId": inst, "bar": "1H", "limit": "6"})
        if d["code"] != "0" or not d["data"]:
            await update.message.reply_text(f"未找到 {symbol}")
            return
        lines = [f"🕯 {symbol}-USDT 最近6小时K线(OKX)\n"]
        # OKX返回最新在前，倒序显示
        for c in reversed(d["data"]):
            # [ts, open, high, low, close, vol, ...]
            o, h, l, close = float(c[1]), float(c[2]), float(c[3]), float(c[4])
            change = (close - o) / o * 100
            emoji = "🟢" if close >= o else "🔴"
            lines.append(f"{emoji} O:{o:,.0f} H:{h:,.0f} L:{l:,.0f} C:{close:,.0f} ({change:+.2f}%)")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logging.error(f"OKX K线出错: {e}")
        await update.message.reply_text("查询失败")


import datetime

# /new - 新币榜（最近上线OKX的币）
async def new_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🆕 查询最近上线 OKX 的新币...")
    try:
        d = await _okx_get("/api/v5/public/instruments", {"instType": "SPOT"})
        if d["code"] != "0":
            await update.message.reply_text("查询失败")
            return
        # 筛 USDT 交易对，按上线时间排序
        usdt_pairs = [
            x for x in d["data"]
            if x.get("quoteCcy") == "USDT" and x.get("listTime")
        ]
        usdt_pairs.sort(key=lambda x: int(x["listTime"]), reverse=True)

        lines = ["🆕 最近上线 OKX 的新币 (USDT交易对)\n"]
        now = datetime.datetime.now()
        for x in usdt_pairs[:10]:
            base = x["baseCcy"]
            list_ts = int(x["listTime"]) / 1000
            list_date = datetime.datetime.fromtimestamp(list_ts)
            days_ago = (now - list_date).days
            date_str = list_date.strftime("%Y-%m-%d")
            if days_ago == 0:
                ago = "今天"
            elif days_ago == 1:
                ago = "昨天"
            else:
                ago = f"{days_ago}天前"
            lines.append(f"• {base}/USDT - {date_str} ({ago})")
        lines.append("\n⚠️ 新币风险极高，谨慎！不构成投资建议")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logging.error(f"新币榜出错: {e}")
        await update.message.reply_text("查询失败")

# /gainers - OKX涨幅榜
async def gainers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 获取 OKX 涨跌榜...")
    try:
        d = await _okx_get("/api/v5/market/tickers", {"instType": "SPOT"})
        if d["code"] != "0":
            await update.message.reply_text("查询失败")
            return
        # 只看 USDT 交易对，算涨跌幅
        coins = []
        for t in d["data"]:
            if not t["instId"].endswith("-USDT"):
                continue
            try:
                last = float(t["last"])
                open24 = float(t["open24h"])
                if open24 <= 0:
                    continue
                change = (last - open24) / open24 * 100
                vol = float(t["volCcy24h"])
                # 过滤掉成交量太小的（避免冷门币噪音）
                if vol < 100000:
                    continue
                coins.append({
                    "sym": t["instId"].replace("-USDT", ""),
                    "price": last, "change": change,
                })
            except (ValueError, KeyError):
                continue

        gainers = sorted(coins, key=lambda x: x["change"], reverse=True)[:15]
        losers = sorted(coins, key=lambda x: x["change"])[:15]

        lines = ["🚀 OKX 24h 涨幅榜\n"]
        for i, c in enumerate(gainers, 1):
            lines.append(f"{i}. {c['sym']}: +{c['change']:.2f}% (${c['price']:,.4g})")
        lines.append("\n📉 OKX 24h 跌幅榜\n")
        for i, c in enumerate(losers, 1):
            lines.append(f"{i}. {c['sym']}: {c['change']:.2f}% (${c['price']:,.4g})")
        lines.append("\n⚠️ 不构成投资建议")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logging.error(f"涨幅榜出错: {e}")
        await update.message.reply_text("查询失败")

# /swap - 合约涨幅榜
async def swap_gainers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 获取 OKX 合约涨幅榜...")
    try:
        d = await _okx_get("/api/v5/market/tickers", {"instType": "SWAP"})
        if d["code"] != "0":
            await update.message.reply_text("查询失败")
            return
        coins = []
        for t in d["data"]:
            if not t["instId"].endswith("-USDT-SWAP"):
                continue
            try:
                last = float(t["last"])
                open24 = float(t["open24h"])
                if open24 <= 0:
                    continue
                change = (last - open24) / open24 * 100
                vol = float(t["volCcy24h"])
                if vol < 100000:
                    continue
                coins.append({
                    "sym": t["instId"].replace("-USDT-SWAP", ""),
                    "price": last, "change": change,
                })
            except (ValueError, KeyError):
                continue
        gainers = sorted(coins, key=lambda x: x["change"], reverse=True)[:15]
        lines = ["📊 OKX 永续合约 24h 涨幅榜\n"]
        for i, c in enumerate(gainers, 1):
            lines.append(f"{i}. {c['sym']}: +{c['change']:.2f}% (${c['price']:,.4g})")
        lines.append("\n(合约波动更大，杠杆风险高)\n⚠️ 不构成投资建议")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logging.error(f"合约涨幅榜出错: {e}")
        await update.message.reply_text("查询失败")


# ===== 供按钮调用的文本版本 =====
async def build_new_text():
    d = await _okx_get("/api/v5/public/instruments", {"instType": "SPOT"})
    if d["code"] != "0":
        return "查询失败"
    pairs = [x for x in d["data"] if x.get("quoteCcy") == "USDT" and x.get("listTime")]
    pairs.sort(key=lambda x: int(x["listTime"]), reverse=True)
    lines = ["🆕 *最近上线 OKX 的新币*\n"]
    now = datetime.datetime.now()
    for x in pairs[:10]:
        base = x["baseCcy"]
        ld = datetime.datetime.fromtimestamp(int(x["listTime"])/1000)
        days = (now - ld).days
        ago = "今天" if days == 0 else ("昨天" if days == 1 else f"{days}天前")
        lines.append(f"• {base}/USDT - {ld.strftime('%m-%d')} ({ago})")
    lines.append("\n⚠️ 新币风险极高！不构成投资建议")
    return "\n".join(lines)

async def build_gainers_text(inst_type="SPOT"):
    d = await _okx_get("/api/v5/market/tickers", {"instType": inst_type})
    if d["code"] != "0":
        return "查询失败"
    suffix = "-USDT-SWAP" if inst_type == "SWAP" else "-USDT"
    coins = []
    for t in d["data"]:
        if not t["instId"].endswith(suffix):
            continue
        try:
            last = float(t["last"]); op = float(t["open24h"])
            if op <= 0: continue
            ch = (last - op) / op * 100
            if float(t["volCcy24h"]) < 100000: continue
            coins.append({"sym": t["instId"].replace(suffix, ""), "price": last, "change": ch})
        except (ValueError, KeyError):
            continue
    g = sorted(coins, key=lambda x: x["change"], reverse=True)[:15]
    title = "永续合约" if inst_type == "SWAP" else "现货"
    lines = [f"🚀 *OKX {title} 24h涨幅榜*\n"]
    for i, c in enumerate(g, 1):
        lines.append(f"{i}. {c['sym']}: +{c['change']:.2f}%")
    if inst_type == "SPOT":
        l = sorted(coins, key=lambda x: x["change"])[:15]
        lines.append("\n📉 *跌幅榜*")
        for i, c in enumerate(l, 1):
            lines.append(f"{i}. {c['sym']}: {c['change']:.2f}%")
    lines.append("\n⚠️ 不构成投资建议")
    return "\n".join(lines)

async def build_funding_text(symbol):
    try:
        d = await _okx_get("/api/v5/public/funding-rate", {"instId": f"{symbol}-USDT-SWAP"})
        if d["code"] == "0" and d["data"]:
            rate = float(d["data"][0]["fundingRate"]) * 100
            hint = "偏多" if rate > 0 else "偏空"
            return f"💵 *{symbol} 永续合约* (OKX)\n资金费率: {rate:+.4f}% ({hint})\n⚠️ 不构成投资建议"
    except Exception as e:
        logging.error(f"OKX资金费率出错,转币安: {e}")
    # OKX 没有 → 回退币安
    from handlers.binance import build_funding_text_bn
    return await build_funding_text_bn(symbol)


# /depth BTC - 订单簿深度
async def depth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0].upper() if context.args else "BTC"
    inst = f"{symbol}-USDT"
    try:
        d = await _okx_get("/api/v5/market/books", {"instId": inst, "sz": "10"})
        if d["code"] != "0" or not d["data"]:
            await update.message.reply_text(f"未找到 {symbol}")
            return
        book = d["data"][0]
        asks = book["asks"]  # 卖盘 [价格,量,...]
        bids = book["bids"]  # 买盘

        # 算买卖盘总量（前10档）
        ask_vol = sum(float(a[1]) for a in asks)
        bid_vol = sum(float(b[1]) for b in bids)
        ratio = bid_vol / ask_vol if ask_vol else 0

        lines = [f"📖 {symbol}-USDT 订单簿(前5档)\n"]
        lines.append("🔴 卖盘(压力):")
        for a in reversed(asks[:5]):
            lines.append(f"  ${float(a[0]):,.1f}  量{float(a[1]):.3f}")
        lines.append("🟢 买盘(支撑):")
        for b in bids[:5]:
            lines.append(f"  ${float(b[0]):,.1f}  量{float(b[1]):.3f}")
        lines.append(f"\n买/卖盘量比: {ratio:.2f}")
        if ratio > 1.2:
            lines.append("买盘较强 📈")
        elif ratio < 0.8:
            lines.append("卖盘较强 📉")
        else:
            lines.append("买卖均衡")
        lines.append("\n⚠️ 不构成投资建议")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logging.error(f"订单簿出错: {e}")
        await update.message.reply_text("查询失败")

# /ratio BTC - 多空比
async def long_short(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0].upper() if context.args else "BTC"
    try:
        d = await _okx_get("/api/v5/rubik/stat/contracts/long-short-account-ratio",
                           {"ccy": symbol, "period": "5m"})
        if d["code"] != "0" or not d["data"]:
            await update.message.reply_text(f"未找到 {symbol} 的多空比数据")
            return
        # 最新一条 [ts, ratio]
        latest = d["data"][0]
        ratio = float(latest[1])

        if ratio > 1.5:
            hint = "散户大幅偏多 ⚠️(散户常做反指，过度偏多需警惕)"
        elif ratio > 1:
            hint = "散户偏多"
        elif ratio < 0.67:
            hint = "散户大幅偏空 ⚠️"
        else:
            hint = "散户偏空"
        await update.message.reply_text(
            f"⚖️ {symbol} 多空比(OKX散户)\n\n"
            f"多空账户比: {ratio:.2f}\n"
            f"{hint}\n\n"
            f"(>1做多人数多，<1做空人数多。散户情绪指标，常作反向参考)\n"
            f"⚠️ 不构成投资建议"
        )
    except Exception as e:
        logging.error(f"多空比出错: {e}")
        await update.message.reply_text("查询失败")


# /liq BTC - 爆仓数据
async def liquidation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0].upper() if context.args else "BTC"
    family = f"{symbol}-USDT"
    try:
        d = await _okx_get("/api/v5/public/liquidation-orders", {
            "instType": "SWAP", "instFamily": family, "state": "filled", "limit": "20"
        })
        if d["code"] != "0" or not d["data"]:
            await update.message.reply_text(f"暂无 {symbol} 的爆仓数据")
            return

        # 汇总：多单爆仓 vs 空单爆仓
        details = d["data"][0].get("details", [])
        if not details:
            await update.message.reply_text(f"暂无 {symbol} 最近爆仓记录")
            return

        long_liq = 0   # 多单爆仓笔数
        short_liq = 0  # 空单爆仓笔数
        long_sz = 0.0
        short_sz = 0.0
        for x in details:
            sz = float(x.get("sz", 0))
            # posSide: long=多单被爆, short=空单被爆
            if x.get("posSide") == "long":
                long_liq += 1
                long_sz += sz
            elif x.get("posSide") == "short":
                short_liq += 1
                short_sz += sz

        lines = [f"💥 {symbol} 永续合约爆仓 (最近{len(details)}笔)\n"]
        lines.append(f"🔴 多单爆仓: {long_liq}笔 (量{long_sz:.2f})")
        lines.append(f"🟢 空单爆仓: {short_liq}笔 (量{short_sz:.2f})")
        lines.append("")
        if long_sz > short_sz * 1.5:
            lines.append("📉 多单爆仓为主 - 价格下跌中，多头被强平")
        elif short_sz > long_sz * 1.5:
            lines.append("📈 空单爆仓为主 - 价格上涨中，空头被强平")
        else:
            lines.append("多空爆仓均衡")
        lines.append("\n(大量爆仓常伴随剧烈波动)\n⚠️ 不构成投资建议")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logging.error(f"爆仓数据出错: {e}")
        await update.message.reply_text("查询失败")


async def build_ratio_text(symbol):
    try:
        d = await _okx_get("/api/v5/rubik/stat/contracts/long-short-account-ratio", {"ccy": symbol, "period": "5m"})
        if d["code"] == "0" and d["data"]:
            ratio = float(d["data"][0][1])
            hint = "散户偏多" if ratio > 1 else "散户偏空"
            return f"⚖️ *{symbol} 多空比* (OKX)\n多空账户比: {ratio:.2f} ({hint})\n(散户情绪，常作反向参考)\n⚠️ 不构成投资建议"
    except Exception as e:
        logging.error(f"OKX多空比出错,转币安: {e}")
    from handlers.binance import build_ratio_text_bn
    return await build_ratio_text_bn(symbol)

async def build_liq_text(symbol):
    d = await _okx_get("/api/v5/public/liquidation-orders", {"instType": "SWAP", "instFamily": f"{symbol}-USDT", "state": "filled", "limit": "20"})
    if d["code"] != "0" or not d["data"]:
        return f"暂无 {symbol} 爆仓数据"
    details = d["data"][0].get("details", [])
    if not details:
        return f"暂无 {symbol} 爆仓记录"
    long_sz = sum(float(x["sz"]) for x in details if x.get("posSide") == "long")
    short_sz = sum(float(x["sz"]) for x in details if x.get("posSide") == "short")
    lines = [f"💥 *{symbol} 爆仓* (最近{len(details)}笔)", f"🔴 多单: {long_sz:.1f}  🟢 空单: {short_sz:.1f}"]
    if long_sz > short_sz * 1.5:
        lines.append("📉 多单爆仓为主")
    elif short_sz > long_sz * 1.5:
        lines.append("📈 空单爆仓为主")
    lines.append("⚠️ 不构成投资建议")
    return "\n".join(lines)


# D-2：资金费率榜（全市场）
async def funding_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💵 获取资金费率榜...")
    try:
        # 先拿所有SWAP合约
        d = await _okx_get("/api/v5/public/instruments", {"instType": "SWAP"})
        if d["code"] != "0":
            await update.message.reply_text("查询失败")
            return
        # 只取USDT永续，限制数量（避免太多请求）
        insts = [x["instId"] for x in d["data"]
                 if x["instId"].endswith("-USDT-SWAP")][:50]  # 取前50个主流

        # 用tickers一次拿不到资金费率，需要逐个查——改用批量思路
        # OKX没有批量资金费率接口，用ticker的成交量筛主流币再查
        # 这里简化：查几个主流币的资金费率做榜
        import asyncio
        majors = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
                  "LINK", "DOT", "LTC", "BCH", "ATOM", "UNI", "FIL"]

        async def get_one(sym):
            try:
                r = await _okx_get("/api/v5/public/funding-rate", {"instId": f"{sym}-USDT-SWAP"})
                if r["code"] == "0" and r["data"]:
                    return {"sym": sym, "rate": float(r["data"][0]["fundingRate"]) * 100}
            except Exception:
                return None
            return None

        results = await asyncio.gather(*[get_one(s) for s in majors])
        rates = [r for r in results if r]

        if not rates:
            await update.message.reply_text("获取失败")
            return

        rates.sort(key=lambda x: x["rate"], reverse=True)

        lines = ["💵 *资金费率榜* (主流永续)\n"]
        lines.append("📈 最高(多头付费，偏热):")
        for r in rates[:5]:
            lines.append(f"  {r['sym']}: {r['rate']:+.4f}%")
        lines.append("\n📉 最低(空头付费，偏冷):")
        for r in rates[-5:]:
            lines.append(f"  {r['sym']}: {r['rate']:+.4f}%")
        lines.append("\n(费率正=多头付费给空头，过高常是过热信号)")
        lines.append("⚠️ 不构成投资建议")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"资金费率榜出错: {e}")
        await update.message.reply_text("查询失败")


# /fprice BTC - 合约行情卡（价格+资金费率+持仓量一站式）
async def fprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/fprice BTC\n查永续合约行情")
        return
    symbol = context.args[0].upper()
    swap = f"{symbol}-USDT-SWAP"
    try:
        # 并发拿：ticker + 资金费率 + 持仓量
        import asyncio
        ticker_t = _okx_get("/api/v5/market/ticker", {"instId": swap})
        funding_t = _okx_get("/api/v5/public/funding-rate", {"instId": swap})
        oi_t = _okx_get("/api/v5/public/open-interest", {"instId": swap})
        ticker, funding, oi = await asyncio.gather(
            ticker_t, funding_t, oi_t, return_exceptions=True
        )

        # 价格
        if isinstance(ticker, dict) and ticker.get("code") == "0" and ticker.get("data"):
            t = ticker["data"][0]
            last = float(t["last"])
            op = float(t["open24h"])
            high = float(t["high24h"])
            low = float(t["low24h"])
            change = (last - op) / op * 100 if op > 0 else 0
        else:
            await update.message.reply_text(f"未找到 {symbol} 的永续合约\n(可能该币无合约)")
            return

        emoji = "📈" if change >= 0 else "📉"
        lines = [f"{emoji} *{symbol} 永续合约* (OKX)\n"]
        lines.append(f"价格: ${last:,.4g} ({change:+.2f}%)")
        lines.append(f"24h高/低: ${high:,.4g} / ${low:,.4g}")

        # 资金费率
        if isinstance(funding, dict) and funding.get("code") == "0" and funding.get("data"):
            rate = float(funding["data"][0]["fundingRate"]) * 100
            hint = "偏多" if rate > 0 else "偏空"
            lines.append(f"\n💵 资金费率: {rate:+.4f}% ({hint})")

        # 持仓量
        if isinstance(oi, dict) and oi.get("code") == "0" and oi.get("data"):
            oi_usd = float(oi["data"][0]["oiUsd"])
            lines.append(f"📈 持仓量: ${oi_usd:,.0f}")

        lines.append("\n⚠️ 合约杠杆风险高，不构成投资建议")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"合约行情出错: {e}")
        await update.message.reply_text("查询失败")



async def build_fprice_text(symbol):
    """合约行情文本（供按钮调用）。OKX 没有该合约则回退币安。"""
    import asyncio
    swap = f"{symbol}-USDT-SWAP"
    ticker, funding, oi = await asyncio.gather(
        _okx_get("/api/v5/market/ticker", {"instId": swap}),
        _okx_get("/api/v5/public/funding-rate", {"instId": swap}),
        _okx_get("/api/v5/public/open-interest", {"instId": swap}),
        return_exceptions=True
    )
    if not (isinstance(ticker, dict) and ticker.get("code") == "0" and ticker.get("data")):
        from handlers.binance import build_fprice_text_bn
        return await build_fprice_text_bn(symbol)
    t = ticker["data"][0]
    last = float(t["last"]); op = float(t["open24h"])
    change = (last - op) / op * 100 if op > 0 else 0
    emoji = "📈" if change >= 0 else "📉"
    lines = [f"{emoji} *{symbol} 永续合约* (OKX)\n",
             f"价格: ${last:,.4g} ({change:+.2f}%)",
             f"24h高/低: ${float(t['high24h']):,.4g} / ${float(t['low24h']):,.4g}"]
    if isinstance(funding, dict) and funding.get("code") == "0" and funding.get("data"):
        rate = float(funding["data"][0]["fundingRate"]) * 100
        lines.append(f"\n💵 资金费率: {rate:+.4f}% ({'偏多' if rate>0 else '偏空'})")
    if isinstance(oi, dict) and oi.get("code") == "0" and oi.get("data"):
        lines.append(f"📈 持仓量: ${float(oi['data'][0]['oiUsd']):,.0f}")
    lines.append("\n⚠️ 合约杠杆风险高，不构成投资建议")
    return "\n".join(lines)
