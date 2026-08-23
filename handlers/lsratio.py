"""多空比极值榜 /lsr —— 全市场散户账户多空比，最高 3 个 + 最低 3 个。

和 `/ratio BTC`（单个币查一次）的区别：那个是"我想知道这个币"，
这个是"现在全市场谁最一边倒"。

## 三个探针实测的结论（决定了这个模块长成这样，别照文档改回去）

**1. 三家没有一家有批量接口。** 资金费率能一次拿全量（`/fex` 就是这么做的），
多空比三家都必须**逐个 symbol 查**：
  · 币安 `futures/data/globalLongShortAccountRatio` 不带 symbol → 400 Invalid symbol
  · Bybit `v5/market/account-ratio` 不带 symbol → retCode 10001 symbol not support
所以只能按成交额取前 N 个逐个拉，覆盖范围必须写在卡片上。

**2. 三家的数字对不上，所以不能合成一张榜。** 同一时刻实测：

    ETH   币安 2.69   OKX 1.33   Bybit 1.86
    BTC   币安 1.07   OKX 1.19   Bybit 1.13

不是谁错了——各家统计的是**自己那批用户**，人群不同结果就不同。
合成一张榜的话，"最看多 Top3"排出来的其实是"哪家的用户最偏多"，
而不是"哪个币最被看多"。**所以这里是换所，不是合并。**

**3. 不接 OKX。** 它的 rubik 接口按 **ccy** 查且只收录主流币
（实测 TUT / AGI / USELESS 全部返回 51012/50011），冷门币一个都没有——
而榜单要找的极值恰恰常在冷门币上。`/fex` 当初跳过 OKX 也是同一个理由。
`/ratio BTC` 仍然走 OKX，那是单币查询，不受影响。

## 口径

多空比 = 做多账户数 ÷ 做空账户数（**账户数，不是持仓金额**）。

    2.5  → 每 10 个做空的对应 25 个做多的，多头拥挤
    0.4  → 空头是多头的 2.5 倍（1 ÷ 0.4），空头拥挤

这是**散户情绪指标，常作反向参考**：一边倒的那一侧往往是被收割的一侧。
但它只是情绪，不是信号——真要用得配合资金费率和持仓量一起看。
"""
import asyncio
import logging
import time

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.util import safe_reply, safe_edit
from handlers import busy
# 「有哪些永续对 + 成交额」这层 dayrank 已经写好并且有测试，共用它。
# 再抄一份的话，哪天加第四家交易所就要改两个地方，漏掉的那处不会报错。
from handlers.dayrank import _uni_binance, _uni_bybit, _keep, norm_base

log = logging.getLogger(__name__)

BN = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"

# 成交额门槛比涨跌榜高（涨跌榜是 500 万）：多空比是**账户数**统计，
# 池子太小的币可能只有几十个账户，比值随便一个人开仓就跳，
# 那种极值是噪音不是信息。
MIN_TURNOVER = 20_000_000
MAX_SCAN = 80             # 按成交额取前 N 个逐个查（没有批量接口，这是纯请求数）
CONCURRENCY = 8
TOP_N = 3                 # 他要的就是"最大和最小的 top3"
CACHE_TTL = 300

V_LABEL = {"binance": "币安", "bybit": "Bybit"}
VENUES = ("binance", "bybit")

_cache = {}               # venue -> {"ts","rows","stats"}


# ── 取数 ────────────────────────────────────────────────────
async def _lsr_binance(client, inst):
    r = await client.get(f"{BN}/futures/data/globalLongShortAccountRatio",
                         params={"symbol": inst, "period": "5m", "limit": 1})
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        return None
    x = rows[0]
    return float(x["longShortRatio"]), float(x["longAccount"])


async def _lsr_bybit(client, inst):
    r = await client.get(f"{BYBIT}/v5/market/account-ratio",
                         params={"category": "linear", "symbol": inst,
                                 "period": "5min", "limit": 1})
    d = r.json()
    if d.get("retCode") != 0:
        return None
    lst = (d.get("result") or {}).get("list") or []
    if not lst:
        return None
    buy, sell = float(lst[0]["buyRatio"]), float(lst[0]["sellRatio"])
    if sell <= 0:
        return None
    # Bybit 给的是两个占比（和为 1），比值要自己算——别直接把 buyRatio 当多空比，
    # 那是"多头占比"，0.53 和 1.13 是两个完全不同的数
    return buy / sell, buy


_FETCH = {"binance": _lsr_binance, "bybit": _lsr_bybit}
_UNI = {"binance": _uni_binance, "bybit": _uni_bybit}


async def _one(client, sem, venue, inst, base, turnover):
    for attempt in (0, 1):
        async with sem:
            try:
                got = await _FETCH[venue](client, inst)
                if got:
                    ratio, long_share = got
                    if ratio > 0:
                        return {"sym": base, "inst": inst, "ratio": ratio,
                                "long_share": long_share, "turnover": turnover}
                    return None
            except Exception as e:
                if attempt:
                    log.debug(f"多空比取失败 {venue}/{inst}: {e}")
        if not attempt:
            await asyncio.sleep(0.4)
    return None


async def scan(venue="binance"):
    """→ (rows, stats)。rows 按 ratio 降序。"""
    # 代币化美股/商品必须剔。第一版只复用了 dayrank 的 `_keep`（那层只管稳定币和
    # 杠杆代币），品类过滤漏了——真机第一跑「最被看多」榜首就是 SOXL
    # （3 倍做多半导体 ETF），第二档还有 CL（原油）。
    # 同一个坑第三次了（微市值 → 涨跌榜 → 这里）：**新开一个扫全市场的榜，
    # 先把已有那张榜的过滤逐条抄过来，别只抄"看着相关"的那一条。**
    try:
        from handlers.klines import noncrypto_bases
        from handlers.dayrank import _BSTOCK
        skip = set(await noncrypto_bases()) | _BSTOCK
    except Exception as e:
        log.warning(f"取非加密品类失败，本轮不剔代币化美股: {e}")
        skip = set()

    async with httpx.AsyncClient(timeout=20) as client:
        uni = await _UNI[venue](client, "perp")
        cand = {}
        thin = stock = 0
        for inst, base, turnover in uni:
            if not _keep(base):
                continue
            if base in skip or norm_base(base) in skip:
                stock += 1
                continue
            if turnover < MIN_TURNOVER:
                thin += 1
                continue
            key = norm_base(base)
            cur = cand.get(key)
            if cur is None or turnover > cur[2]:
                cand[key] = (inst, base, turnover)
        top = sorted(cand.values(), key=lambda x: -x[2])[:MAX_SCAN]
        sem = asyncio.Semaphore(CONCURRENCY)
        got = await asyncio.gather(
            *[_one(client, sem, venue, i, b, t) for i, b, t in top])

    rows = sorted([g for g in got if g], key=lambda r: -r["ratio"])
    return rows, {"pool": len(cand), "asked": len(top), "ok": len(rows),
                  "failed": len(top) - len(rows), "thin": thin,
                  "stock": stock, "skip_ok": bool(skip)}


async def cached_scan(venue="binance", force=False):
    c = _cache.get(venue)
    if not force and c and time.time() - c["ts"] < CACHE_TTL:
        return c["rows"], c["stats"], int(time.time() - c["ts"])
    rows, stats = await scan(venue)
    _cache[venue] = {"ts": time.time(), "rows": rows, "stats": stats}
    return rows, stats, 0


# ── 解读 ────────────────────────────────────────────────────
def read(ratio):
    """一句话说清这个数字意味着什么。0.43 要说成"空头是多头的 2.3 倍"，
    因为小于 1 的比值人脑读不出量级——这正是他截图里问的那件事。"""
    if ratio >= 1:
        return f"多头是空头的 {ratio:.1f} 倍"
    return f"空头是多头的 {1 / ratio:.1f} 倍"


def tag(ratio):
    if ratio >= 2.5:
        return "多头极度拥挤"
    if ratio >= 1.5:
        return "多头拥挤"
    if ratio <= 0.4:
        return "空头极度拥挤"
    if ratio <= 0.67:
        return "空头拥挤"
    return "两边接近"


# ── 渲染 ────────────────────────────────────────────────────
def _block(items):
    out = []
    for r in items:
        out.append(f"{r['sym']:<10}{r['ratio']:>6.2f}   多头{r['long_share']*100:>4.0f}%"
                   f"   {read(r['ratio'])}")
    return "```\n" + "\n".join(out) + "\n```"


def build_text(rows, venue, stats, age=0):
    stats = stats or {}
    lines = [f"⚖️ *多空比极值榜* · {V_LABEL[venue]}永续",
             "散户账户数比（做多账户÷做空账户），常作反向参考"]
    if not rows:
        lines.append("")
        lines.append(f"这一轮一个都没取到（问了 {stats.get('asked', 0)} 个）。"
                     f"点 🔄 重扫，或换一家试试。")
        return "\n".join(lines)

    # 以 1 为界切开，不是"取头 3 + 取尾 3"。
    # 取头尾的话，池子里没有真正偏空的币时，`ZRO 1.01`（多头略多）会被列进
    # 「最被看空 Top3」——真机第一跑 Bybit 就是这样。
    # 这和涨跌榜那条"按正负切开"是同一个错，只是轴从 0 挪到了 1。
    longs = [r for r in rows if r["ratio"] > 1][:TOP_N]
    shorts = [r for r in rows if r["ratio"] < 1]
    shorts = list(reversed(shorts))[:TOP_N]      # 最空的在前
    lines.append("")
    if longs:
        lines.append(f"🟢 *最被看多 Top{len(longs)}*　多头拥挤 → 反向偏空")
        lines.append(_block(longs))
    else:
        lines.append("🟢 *最被看多*（0）这一轮没有一个币是多头占优的")
    if shorts:
        lines.append(f"🔴 *最被看空 Top{len(shorts)}*　空头拥挤 → 反向偏多")
        lines.append(_block(shorts))
    else:
        lines.append("🔴 *最被看空*（0）这一轮没有一个币是空头占优的"
                     "——全市场一边倒看多")

    btc = next((r for r in rows if r["sym"] == "BTC"), None)
    if btc:
        lines.append(f"参照：BTC {btc['ratio']:.2f}（多头 {btc['long_share']*100:.0f}%）"
                     f"　{tag(btc['ratio'])}")
    lines.append(f"{V_LABEL[venue]}永续·成交额前 {stats.get('ok', 0)} 个币里排的"
                 f"（≥{MIN_TURNOVER // 10000}万）｜口径点 ℹ️　👇 可换交易所")
    if stats.get("failed"):
        lines.append(f"⚠️ {stats['failed']} 个没取到，这轮没进榜")
    if age:
        lines.append(f"（{age} 秒前扫的，点 🔄 重扫）")
    return "\n".join(lines)


def build_detail(rows, venue, stats):
    stats = stats or {}
    return "\n".join([
        f"ℹ️ *多空比极值榜 · 口径*", "━━━━━━━━━━━━━━",
        "*这个数是什么*",
        "多空比 = 做多账户数 ÷ 做空账户数。注意是**账户数**，不是持仓金额——",
        "一个百万大户和一个一百块的散户，在这里各算一票。",
        "",
        "　2.5 → 每 10 个做空的对应 25 个做多的（多头拥挤）",
        "　0.4 → 空头是多头的 2.5 倍（空头拥挤）",
        "",
        "*怎么用*",
        "这是**散户情绪指标，常作反向参考**：一边倒的那侧往往是被收割的那侧。",
        "但它只是情绪不是信号——真要用，配合资金费率（/fex）和持仓量一起看：",
        "散户极度看多 + 资金费率高得离谱 = 多头在付钱硬扛，这种才有意义。",
        "",
        "*扫了什么*",
        f"{V_LABEL[venue]} USDT 永续，成交额 ≥{MIN_TURNOVER // 10000}万 的有 "
        f"{stats.get('pool', 0)} 个，",
        f"按成交额取前 {stats.get('asked', 0)} 个逐个查，拿到 {stats.get('ok', 0)} 个"
        + (f"（{stats['failed']} 个没取到）" if stats.get("failed") else ""),
        f"成交额不够被挡掉的：{stats.get('thin', 0)} 个",
        f"代币化美股/商品剔掉：{stats.get('stock', 0)} 个"
        + ("" if stats.get("skip_ok", True) else "　⚠️ 本轮品类表没取到，可能有漏网")
        + "（SOXL 这种 3 倍做多半导体 ETF 的多空比，和币圈情绪不是一回事）",
        "",
        "*为什么不合并三家*",
        "同一时刻实测 ETH：币安 2.69、OKX 1.33、Bybit 1.86。各家统计的是自己那批",
        "用户，人群不同结果就不同。合成一张榜的话，排出来的是「哪家用户最偏多」，",
        "不是「哪个币最被看多」。所以这里是**换所**，不是合并。",
        "",
        "*为什么没有 OKX*",
        "它的接口按币种收录、只有主流币（TUT / AGI 这些冷门币直接报错），",
        "而极值恰恰常出在冷门币上。单个币查 OKX 用 `/ratio BTC`，那条没受影响。",
        "",
        "⚠️ 情绪指标，不构成投资建议",
    ])


def kb(venue):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if v == venue else ''}{V_LABEL[v]}",
                              callback_data=f"ls:v:{v}") for v in VENUES],
        [InlineKeyboardButton("ℹ️ 口径", callback_data=f"ls:i:{venue}"),
         InlineKeyboardButton("🔄 重扫", callback_data=f"ls:r:{venue}"),
         InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")],
    ])


# ── 入口 ────────────────────────────────────────────────────
def parse_args(args):
    for a in args or []:
        al = str(a).lower()
        if al in ("bybit", "by"):
            return "bybit"
        if al in ("币安", "binance", "bn"):
            return "binance"
    return "binance"


async def lsr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lsr [币安|bybit] —— 全市场多空比最高/最低各 3 个。"""
    venue = parse_args(context.args)
    uid = update.effective_user.id
    c = _cache.get(venue)
    if c and time.time() - c["ts"] < CACHE_TTL:
        rows, stats, age = await cached_scan(venue)
        await safe_reply(update.message, build_text(rows, venue, stats, age),
                         reply_markup=kb(venue), parse_mode="Markdown")
        return
    async with busy.guard(uid, "lsratio") as ok:
        if not ok:
            await safe_reply(update.message, busy.busy_text(uid, "lsratio", "多空比扫描"))
            return
        await safe_reply(update.message,
            f"⚖️ 扫 {V_LABEL[venue]}永续的多空比…"
            f"（三家都没有批量接口，只能逐个查，十几秒；其他功能照常能用）")
        try:
            rows, stats, age = await cached_scan(venue)
        except Exception as e:
            log.error(f"多空比榜扫描出错: {e}")
            await safe_reply(update.message, f"扫描失败，稍后再试：{str(e)[:80]}")
            return
    await safe_reply(update.message, build_text(rows, venue, stats, age),
                     reply_markup=kb(venue), parse_mode="Markdown")


async def from_btn(query, context, venue, force=False, detail=False):
    uid = query.from_user.id
    c = _cache.get(venue)
    fresh = c and time.time() - c["ts"] < CACHE_TTL
    if force or not fresh:
        async with busy.guard(uid, "lsratio") as ok:
            if not ok:
                await query.answer(
                    f"上一次扫描还在跑（已 {busy.elapsed(uid, 'lsratio')} 秒）",
                    show_alert=True)
                return
            await safe_edit(query, f"⚖️ 扫 {V_LABEL[venue]}永续的多空比…（十几秒）")
            try:
                rows, stats, age = await cached_scan(venue, force=force)
            except Exception as e:
                log.error(f"多空比榜按钮扫描出错: {e}")
                await safe_edit(query, f"扫描失败：{str(e)[:80]}", reply_markup=kb(venue))
                return
    else:
        rows, stats, age = await cached_scan(venue)
    if detail:
        await safe_edit(query, build_detail(rows, venue, stats),
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            "⬅️ 回榜单", callback_data=f"ls:v:{venue}")]]),
                        parse_mode="Markdown")
        return
    await safe_edit(query, build_text(rows, venue, stats, age),
                    reply_markup=kb(venue), parse_mode="Markdown")
