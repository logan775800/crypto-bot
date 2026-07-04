"""策略类命令：/weak 弱势·横盘扫描，/momentum 动量轮动回测。

复用 api.py（带缓存+限流的 CoinGecko 封装），输出为 Telegram 友好的精简排行。
⚠️ 所有结果不构成投资建议。
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
import api
from handlers.util import safe_reply

# 稳定币 / 包装币 / 质押衍生品：无独立行情，扫描时剔除
SKIP = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD",
    "USDD", "GUSD", "USDP", "FRAX", "LUSD", "BUSD", "EURC", "USD1",
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "WBETH", "RETH",
    "CBBTC", "LBTC", "SOLVBTC", "BSC-USD", "WBNB", "JITOSOL", "MSOL",
}


def _pct(x):
    return f"{x:+.1f}%"


# ---------------- /weak：弱势 / 横盘扫描 ----------------
async def weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/weak [N]  扫市值前N(默认50)，输出最横盘/最弱/相对抗跌三张榜。"""
    top = 50
    if context.args:
        try:
            top = max(10, min(100, int(context.args[0])))
        except ValueError:
            pass
    await update.message.reply_text(f"🔎 扫描市值前 {top} 主流币...")
    try:
        rows = [c for c in await api.get_markets_full(top) if c["symbol"] not in SKIP]
        if not rows:
            await update.message.reply_text("没拿到数据，稍后再试。")
            return

        btc = next((c for c in rows if c["symbol"] == "BTC"), None)
        btc7 = btc["change_7d"] if btc else 0.0

        # 横盘分：0.6×|7天| + 0.4×24h波动，越小越『没怎么动』
        def flat_score(c):
            return 0.6 * abs(c["change_7d"]) + 0.4 * c["range_24h"]

        flat = sorted(rows, key=flat_score)[:8]
        weakest = sorted(rows, key=lambda c: c["change_7d"])[:8]
        for c in rows:
            c["rs"] = c["change_7d"] - btc7
        strong = sorted(rows, key=lambda c: c["rs"], reverse=True)[:8]

        L = [f"📊 *主流币弱势/横盘扫描*（前{top}）\n"]
        L.append("😴 *最横盘*（低波动盘整）")
        for c in flat:
            L.append(f"  {c['symbol']}: 7d {_pct(c['change_7d'])}  24h幅{c['range_24h']:.1f}%")
        L.append("\n💥 *最弱*（近7天跌最多）")
        for c in weakest:
            L.append(f"  {c['symbol']}: {_pct(c['change_7d'])}")
        L.append(f"\n🛡️ *相对抗跌*（RS vs BTC {_pct(btc7)}）")
        for c in strong:
            L.append(f"  {c['symbol']}: 7d {_pct(c['change_7d'])}  RS {c['rs']:+.1f}%")
        L.append("\n_横盘≠蓄势，弱市『没跌』常是『没人碰』。不构成投资建议_")

        await safe_reply(update.message, "\n".join(L), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"/weak 出错: {type(e).__name__}: {e}")
        await update.message.reply_text(f"扫描失败（{type(e).__name__}），稍后再试。")


# ---------------- /momentum：动量轮动回测 ----------------
def _backtest(panel, lookback, hold, rebalance, cash_filter):
    """panel: {sym: [日线收盘, ...]}（各币已按索引对齐、等长）。
    返回 (策略净值曲线, BTC净值, 等权净值, 最近调仓记录)。"""
    syms = list(panel)
    L = len(next(iter(panel.values())))
    start = lookback
    eq = [1.0]
    holdings = []
    log = []

    for i in range(start + 1, L):
        # 调仓日：按过去 lookback 天涨幅排名
        if (i - start - 1) % rebalance == 0:
            ranked = []
            for s in syms:
                a = panel[s][i - 1 - lookback]
                b = panel[s][i - 1]
                if a > 0:
                    ranked.append((s, b / a - 1))
            ranked.sort(key=lambda x: x[1], reverse=True)
            holdings = [s for s, m in ranked[:hold] if (m > 0 or not cash_filter)]
            log.append((list(holdings), [round(m * 100) for _, m in ranked[:hold]]))
        # 当日等权收益（空仓部分算现金0，分母固定=hold）
        if holdings:
            day = sum(panel[s][i] / panel[s][i - 1] - 1 for s in holdings) / hold
        else:
            day = 0.0
        eq.append(eq[-1] * (1 + day))

    def bench(arr):
        e = [1.0]
        for i in range(start + 1, L):
            e.append(e[-1] * (arr[i] / arr[i - 1]))
        return e

    btc_eq = bench(panel["BTC"]) if "BTC" in panel else None
    # 等权全体
    ew = [1.0]
    for i in range(start + 1, L):
        r = sum(panel[s][i] / panel[s][i - 1] - 1 for s in syms) / len(syms)
        ew.append(ew[-1] * (1 + r))
    return eq, btc_eq, ew, log


def _mdd(eq):
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return dd


async def momentum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/momentum  动量轮动回测：每周持有过去30天最强的3个币，对比死拿BTC。
    可选参数：/momentum <宇宙N> <回看天> <持有K> <调仓天>，如 /momentum 15 20 3 7"""
    # 默认参数（宇宙偏小以控制拉数据耗时、降低被限流概率）
    N, lookback, hold, rebalance, days = 12, 30, 3, 7, 180
    try:
        a = context.args
        if len(a) >= 1: N = max(6, min(30, int(a[0])))
        if len(a) >= 2: lookback = max(5, min(90, int(a[1])))
        if len(a) >= 3: hold = max(1, min(8, int(a[2])))
        if len(a) >= 4: rebalance = max(1, min(30, int(a[3])))
    except ValueError:
        pass

    await update.message.reply_text(
        f"⏳ 回测动量轮动（宇宙{N}/回看{lookback}天/持{hold}/每{rebalance}天调仓）...\n"
        f"需逐个拉日线，约需 30~60 秒,请稍候")
    try:
        leaders = await api.get_market_leaders(N + 12)
        syms = [c["symbol"] for c in leaders if c["symbol"] not in SKIP][:N]
        if "BTC" not in syms:
            syms = ["BTC"] + syms[:N - 1]

        # 逐币容错：数据源(CoinGecko免费额度)易触发限流(429)，
        # 单个币拉失败就跳过，拿到几个算几个，避免整个回测崩掉。
        panel = {}
        skipped = 0
        min_len = lookback + rebalance + 5
        for s in syms:
            try:
                prices = await api.get_daily_prices(s, days)
            except Exception:
                prices = None
            if prices and len(prices) >= min_len:
                panel[s] = prices
            else:
                skipped += 1
        # BTC 是基准，若被限流漏掉，单独补拉一次
        if "BTC" not in panel:
            try:
                b = await api.get_daily_prices("BTC", days)
                if b and len(b) >= min_len:
                    panel["BTC"] = b
            except Exception:
                pass
        if len(panel) < 5 or "BTC" not in panel:
            await update.message.reply_text(
                f"数据源限流，暂时只取到 {len(panel)} 个币（回测需≥5且含BTC）。"
                f"过一两分钟再试即可。")
            return

        # 按最短长度对齐（从末尾截取，保证最新日期对齐）
        Lmin = min(len(v) for v in panel.values())
        panel = {s: v[-Lmin:] for s, v in panel.items()}

        eq, btc_eq, ew, log = _backtest(panel, lookback, hold, rebalance, True)

        def line(name, e):
            tot = (e[-1] / e[0] - 1) * 100
            return f"{name} 总收益 {tot:+.0f}%  最大回撤 {_mdd(e)*100:.0f}%"

        strat_tot = eq[-1] / eq[0] - 1
        btc_tot = btc_eq[-1] / btc_eq[0] - 1
        alpha = (strat_tot - btc_tot) * 100
        verdict = "跑赢BTC ✅" if alpha > 0 else "跑输BTC ❌"

        skip_note = f"（{skipped}个币被限流跳过）" if skipped else ""
        L = [f"📈 *动量轮动回测*（近{Lmin}天，{len(panel)}币宇宙{skip_note}）\n"]
        L.append("🚀 " + line("策略", eq))
        L.append("🟠 " + line("死拿BTC", btc_eq))
        L.append("⚪ " + line("等权全体", ew))
        L.append(f"\n结论：{verdict}（超额 {alpha:+.0f} 个百分点）")
        if log:
            picks, moms = log[-1]
            tag = ", ".join(f"{p}({m:+d}%)" for p, m in zip(picks, moms)) or "空仓"
            L.append(f"最近一次调仓选中：{tag}")
        L.append("\n_单一区间成绩可能过拟合，多改参数验证稳健性。不构成投资建议_")

        await safe_reply(update.message, "\n".join(L), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"/momentum 出错: {type(e).__name__}: {e}")
        await update.message.reply_text(f"回测失败（{type(e).__name__}），稍后再试。")
