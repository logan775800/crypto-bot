import logging
import httpx
from telegram import Update
from telegram.ext import ContextTypes

OKX_BASE = "https://www.okx.com"

async def _okx_get(path, params=None):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{OKX_BASE}{path}", params=params or {})
        resp.raise_for_status()
        return resp.json()

# /movers - 当前异动快照（涨跌+放量汇总）
async def movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 获取当前市场异动快照...")
    try:
        d = await _okx_get("/api/v5/market/tickers", {"instType": "SPOT"})
        if d["code"] != "0":
            await update.message.reply_text("查询失败")
            return

        # 拿合约列表（判断哪些币有合约）
        try:
            swap_d = await _okx_get("/api/v5/public/instruments", {"instType": "SWAP"})
            swap_coins = set()
            if swap_d["code"] == "0":
                for x in swap_d["data"]:
                    if x["instId"].endswith("-USDT-SWAP"):
                        swap_coins.add(x["instId"].replace("-USDT-SWAP", ""))
        except Exception:
            swap_coins = set()

        coins = []
        for t in d["data"]:
            if not t["instId"].endswith("-USDT"):
                continue
            try:
                last = float(t["last"])
                op = float(t["open24h"])
                vol = float(t["volCcy24h"])
                if op <= 0 or vol < 1000000:  # 成交量过滤
                    continue
                change = (last - op) / op * 100
                sym = t["instId"].replace("-USDT", "")
                coins.append({"sym": sym, "change": change, "price": last,
                              "vol": vol, "has_swap": sym in swap_coins})
            except (ValueError, KeyError):
                continue

        gainers = sorted(coins, key=lambda x: x["change"], reverse=True)[:8]
        losers = sorted(coins, key=lambda x: x["change"])[:8]

        def fmt(c):
            tag = "📈合约" if c["has_swap"] else ""
            return f"{c['sym']}: {c['change']:+.2f}% (${c['price']:,.4g}) {tag}"

        lines = ["📸 *当前市场异动快照*\n"]
        lines.append("🚀 *涨幅榜*")
        for c in gainers:
            lines.append(f"  {fmt(c)}")
        lines.append("\n💥 *跌幅榜*")
        for c in losers:
            lines.append(f"  {fmt(c)}")
        lines.append("\n📊 *成交量榜*")
        vol_top = sorted(coins, key=lambda x: x["vol"], reverse=True)[:5]
        for c in vol_top:
            lines.append(f"  {c['sym']}: ${c['vol']:,.0f}")
        lines.append("\n⚠️ 不构成投资建议")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"异动快照出错: {e}")
        await update.message.reply_text("查询失败")
