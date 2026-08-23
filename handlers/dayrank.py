"""多日涨跌榜 —— N 日累计涨跌幅排行（Bybit + OKX 永续）。

**为什么要单独做**：现有的榜全是 24h（`/top`、四个交易所专区、合约异动面板），
而 `/upstreak` 是"连续 N 天同向"——那是另一件事。一个币可以 3 天累计涨 30%
却只有 2 根阳线（中间回调一天），在连涨榜里它根本不存在。
「这三天谁涨得最多」这个问题以前答不了。

**口径**（卡片上会写出来，因为不写就没人知道是哪个口径）：

    N 日涨跌幅 = (现价 − N 根日线之前那根的收盘价) ÷ 那个收盘价

终点用**现价**（也就是今天那根未收盘 K 线的最新价），不是昨天的收盘价——
否则今天的行情完全不体现，读出来是一笔隔夜的旧账。

⚠️ 这和 v1.33.1 那条「量比必须用已收盘那根」**不冲突**，别顺手"统一"了：
未收盘 K 线的**成交量**只累积了一部分（所以拿它算量比会系统性偏低），
但它的**收盘价就是现价**，恰恰是这里要的终点。

**一次扫描答所有窗口**：日线一次拉 MAX_WIN+1 根，3 日/7 日/14 日都从同一批
数据算。换窗口的按钮读缓存、不重扫——重扫要十几秒，而那十几秒里他只是想
换个窗口看看。
"""
import asyncio
import logging
import re
import time

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.util import safe_reply, safe_edit
from handlers import busy
# 交易所的"有哪些永续对 + 成交额"这层 streak 已经写好了，共用它。
# 复制一份的话，哪天加第三家交易所就要改两个地方，而漏掉的那处不会报错。
from handlers.streak import _okx_universe, _bybit_universe, OKX, BYBIT

log = logging.getLogger(__name__)

MAX_WIN = 14              # 最长窗口。日线拉 MAX_WIN+1 根就够算所有窗口
DEFAULT_WIN = 3
WIN_BUTTONS = (3, 7, 14)
MIN_TURNOVER_M = 5        # 百万美元：滤掉僵尸合约，否则榜首全是没人交易的币
MAX_SCAN = 150            # 每所按成交额取前 N 个扫（和 streak 同一个量级）
CONCURRENCY = 8
TOP_SHOW = 15
CACHE_TTL = 300           # 缓存 5 分钟：换窗口/换所的按钮走这里，不重扫

EX_LABEL = {"okx": "OKX", "bybit": "Bybit", "all": "Bybit+OKX"}
# 合约面值前缀：1000PEPE / 10000SATS / 1MBABYDOGE 都是"底层资产 × 倍数"
_DENOM = re.compile(r"^(?:1000000|100000|10000|1000|1M)([A-Z][A-Z0-9]*)$")

_cache = {}               # ex -> {"ts": float, "rows": [...], "scanned": int, "stats": dict}


# ── 取数 ────────────────────────────────────────────────────
async def _klines(client, sem, ex, inst, base, turnover):
    """拉一个币的日线，返回 {"sym","ex","closes"(新→旧),"turnover"}。失败返回 None。"""
    async with sem:
        try:
            if ex == "Bybit":
                r = await client.get(f"{BYBIT}/v5/market/kline", params={
                    "category": "linear", "symbol": inst,
                    "interval": "D", "limit": str(MAX_WIN + 1)})
                d = r.json()
                if d.get("retCode") != 0:
                    return None
                rows = d.get("result", {}).get("list", [])
            else:
                r = await client.get(f"{OKX}/api/v5/market/candles", params={
                    "instId": inst, "bar": "1D", "limit": str(MAX_WIN + 1)})
                d = r.json()
                if d.get("code") != "0":
                    return None
                rows = d.get("data", [])
            closes = []
            for c in rows:                      # 两家都是新→旧，收盘价都在第 5 列
                try:
                    closes.append(float(c[4]))
                except (ValueError, IndexError, TypeError):
                    return None
            if len(closes) < 2:
                return None
            return {"sym": base, "ex": ex, "closes": closes, "turnover": turnover}
        except Exception:
            return None


def norm_base(base):
    """把合约面值前缀归一到底层资产，只用于去重。

    `1000PEPE` 和 `PEPE` 是**同一个币**，只是合约面值不同（1000 倍报价）。
    不归一的话它们在榜上并排出现两次、涨幅几乎一样，白占两个名额——
    真机第一次冒烟，3 日榜第 9、10 名就是 1000PEPE 和 PEPE。

    只认纯面值前缀（1000/10000/1M…）。别顺手扩成"名字像就合并"：
    PUMP 和 PUMPFUN 看着像一对，合错了就是把两个币的行情算到一起，
    微市值那边的同名误判就是这么来的——宁可少合一个，不能合错一个。
    """
    m = _DENOM.match(base)
    return m.group(1) if m else base


async def scan(ex="all"):
    """扫一遍，返回 (rows, scanned, stats)。

    stats 里的每个数都会印在卡片上。**剔了什么必须报出来**：
    一个上周才上的暴涨币在 7 日榜里凭空消失，看榜的人不会知道是它没资格，
    只会以为这张榜漏了。
    """
    min_turnover = MIN_TURNOVER_M * 1_000_000
    # 代币化美股：OKX 的**合约接口没有品类字段**，而它上了一批代币化美股，
    # 成交额还排得很前（SNDK 就是那个模块 docstring 里点名的例子）。
    # 不剔的话榜单会被美股占位——这个坑缓涨扫描上踩过，判据现成的，直接用。
    try:
        from handlers.klines import noncrypto_bases
        skip = await noncrypto_bases()
    except Exception as e:
        log.warning(f"取非加密品类失败，本轮不剔代币化美股: {e}")
        skip = set()

    async with httpx.AsyncClient(timeout=15) as client:
        sem = asyncio.Semaphore(CONCURRENCY)
        tasks = []
        if ex in ("bybit", "all"):
            uni = (await _bybit_universe(client, min_turnover))[:MAX_SCAN]
            tasks += [_klines(client, sem, "Bybit", s, b, t) for s, b, t in uni]
        if ex in ("okx", "all"):
            uni = (await _okx_universe(client, min_turnover))[:MAX_SCAN]
            tasks += [_klines(client, sem, "OKX", i, b, t) for i, b, t in uni]
        scanned = len(tasks)
        results = await asyncio.gather(*tasks)

    best, short, stock, merged = {}, 0, 0, 0
    for r in results:
        if not r:
            continue
        key = norm_base(r["sym"])
        if r["sym"] in skip or key in skip:
            stock += 1
            continue
        if len(r["closes"]) < MAX_WIN + 1:
            short += 1
        # 同一个币两家都有时，留**成交额大**的那家。
        # 别学连涨榜那样"留涨得最多的那家"——那会系统性地把榜单往极端值上拉，
        # 而两家的差价只是流动性差异，不是行情差异
        cur = best.get(key)
        if cur is None:
            best[key] = r
        else:
            merged += 1
            if r["turnover"] > cur["turnover"]:
                best[key] = r
    return list(best.values()), scanned, {
        "short": short, "stock": stock, "merged": merged,
        "skip_ok": bool(skip)}


async def cached_scan(ex="all", force=False):
    c = _cache.get(ex)
    if not force and c and time.time() - c["ts"] < CACHE_TTL:
        return c["rows"], c["scanned"], c["stats"], int(time.time() - c["ts"])
    rows, scanned, stats = await scan(ex)
    _cache[ex] = {"ts": time.time(), "rows": rows, "scanned": scanned, "stats": stats}
    return rows, scanned, stats, 0


# ── 计算 ────────────────────────────────────────────────────
def pct(row, win):
    """N 日涨跌幅。日线根数不够就返回 None（不是 0——0 会假装它没涨没跌）。"""
    cl = row["closes"]
    if len(cl) <= win:
        return None
    base = cl[win]
    if not base:
        return None
    return (cl[0] - base) / base * 100


def ranked(rows, win):
    """→ (涨幅榜, 跌幅榜, 统计)。**按正负切开**，不是各取头尾。

    一开始写成"前 15 + 后 15"，币少的时候同一个币会同时出现在涨幅榜和跌幅榜里
    （-30% 的币堂而皇之列在涨幅榜第三名）。全市场三百个币时看不出来，
    换个所或者窗口一长就露馅——按正负切才是这张榜真正的定义。
    """
    scored = []
    for r in rows:
        p = pct(r, win)
        if p is not None:
            scored.append((p, r))
    scored.sort(key=lambda x: -x[0])
    ups = [x for x in scored if x[0] > 0]
    downs = list(reversed([x for x in scored if x[0] < 0]))   # 跌得最狠的在前
    stat = {"n": len(scored), "n_up": len(ups), "n_down": len(downs),
            "least_bad": scored[0] if scored else None,
            "least_good": scored[-1] if scored else None}
    return ups[:TOP_SHOW], downs[:TOP_SHOW], stat


# ── 渲染 ────────────────────────────────────────────────────
def _fmt_price(p):
    return f"{p:,.6g}"


def _block(items, show_ex):
    out = []
    for p, r in items:
        tag = f"[{r['ex'][:2]}] " if show_ex else ""
        out.append(f"{tag}{r['sym']:<9}{p:+8.1f}%  ${_fmt_price(r['closes'][0])}")
    return "```\n" + "\n".join(out) + "\n```"


def _one_line(item, show_ex):
    p, r = item
    tag = f"[{r['ex']}] " if show_ex else ""
    return f"{tag}{r['sym']} {p:+.1f}%"


def build_text(rows, win, ex, scanned, stats, age=0):
    stats = stats or {}
    up, down, st = ranked(rows, win)
    show_ex = ex == "all"
    lines = [f"📅 *{win} 日涨跌榜* · {EX_LABEL[ex]}永续", "━━━━━━━━━━━━━━"]

    if not st["n"]:
        lines.append(f"没有一个币凑得齐 {win} 根日线——"
                     f"扫了 {scanned} 个，多半是窗口开太长了，试试更短的天数。")
        return "\n".join(lines)

    # 空的分组也要印出来：整段消失读起来像"根本没扫这一边"，
    # 而"这 3 天一个上涨的都没有"本身就是最重要的那条信息（市场一边倒）
    if up:
        lines.append(f"🚀 *涨幅榜*（{st['n_up']}）")
        lines.append(_block(up, show_ex))
    else:
        lines.append(f"🚀 *涨幅榜*（0）这 {win} 天**没有一个币是涨的**")
        if st["least_bad"]:
            lines.append(f"　跌得最少：{_one_line(st['least_bad'], show_ex)}")
    if down:
        lines.append(f"📉 *跌幅榜*（{st['n_down']}）")
        lines.append(_block(down, show_ex))
    else:
        lines.append(f"📉 *跌幅榜*（0）这 {win} 天**没有一个币是跌的**")
        if st["least_good"]:
            lines.append(f"　涨得最少：{_one_line(st['least_good'], show_ex)}")

    # 口径和覆盖范围写在脸上：一张 15 行的名单不写这些，看起来就像"全市场就这些"
    lines.append("━━━━━━━━━━━━━━")
    lines.append(f"口径：现价 vs {win} 天前的日线收盘（日线按 UTC 0 点切）")
    lines.append(f"覆盖：{EX_LABEL[ex]} 永续，成交额≥{MIN_TURNOVER_M * 100:g}万"
                 f"，每所取成交额前 {MAX_SCAN} 个｜实扫 {scanned} 个、"
                 f"合并后 {len(rows)} 个币（{st['n']} 个够 {win} 天）")
    drops = []
    if stats.get("stock"):
        drops.append(f"代币化美股 {stats['stock']} 个")
    if stats.get("merged"):
        drops.append(f"跨所/面值重复 {stats['merged']} 个（1000PEPE 和 PEPE 算一个）")
    if drops:
        lines.append("剔除：" + "、".join(drops))
    if not stats.get("skip_ok", True):
        lines.append("⚠️ 这轮没取到品类表，代币化美股可能混在榜里")
    if stats.get("short"):
        lines.append(f"{stats['short']} 个币日线不够 {MAX_WIN} 根（新上市），"
                     f"窗口越长它们越算不出来——不是被剔，是没资格进这个窗口")
    hidden = max(0, st["n_up"] - len(up)) + max(0, st["n_down"] - len(down))
    if hidden:
        lines.append(f"两组合计还有 {hidden} 个没显示（每组只列前 {TOP_SHOW}）")
    lines.append("不含现货、不含其它交易所")
    if age:
        lines.append(f"数据来自 {age} 秒前那次扫描（点【🔄 重扫】拉最新）")
    lines.append("⚠️ 涨得多≠还会涨，不构成投资建议")
    return "\n".join(lines)


def kb(win, ex):
    wins = [InlineKeyboardButton(f"{'●' if w == win else ''}{w}日",
                                 callback_data=f"dr:w:{w}:{ex}")
            for w in WIN_BUTTONS]
    exs = [InlineKeyboardButton(f"{'●' if e == ex else ''}{EX_LABEL[e]}",
                                callback_data=f"dr:w:{win}:{e}")
           for e in ("all", "bybit", "okx")]
    return InlineKeyboardMarkup([
        wins, exs,
        [InlineKeyboardButton("🔄 重扫", callback_data=f"dr:r:{win}:{ex}"),
         InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")],
    ])


# ── 入口 ────────────────────────────────────────────────────
def parse_args(args):
    win, ex = DEFAULT_WIN, "all"
    for a in args or []:
        al = str(a).lower()
        if al in ("okx", "bybit", "all"):
            ex = al
            continue
        # 「3日」「7天」这么打也认——他就是这么说话的
        num = al.rstrip("日天dD")
        try:
            win = int(num)
        except ValueError:
            pass
    return max(1, min(win, MAX_WIN)), ex


async def rank_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rank [天数] [bybit|okx|all] —— N 日累计涨跌幅排行。"""
    win, ex = parse_args(context.args)
    uid = update.effective_user.id
    async with busy.guard(uid, "dayrank") as ok:
        if not ok:
            await safe_reply(update.message, busy.busy_text(uid, "dayrank", "涨跌榜扫描"))
            return
        cached = _cache.get(ex)
        fresh = cached and time.time() - cached["ts"] < CACHE_TTL
        if not fresh:
            await safe_reply(update.message,
                f"📅 扫 {EX_LABEL[ex]} 永续的 {win} 日涨跌幅…"
                f"（要逐个拉日线，约十几秒；这期间其他功能照常能用）")
        try:
            rows, scanned, stats, age = await cached_scan(ex)
        except Exception as e:
            log.error(f"多日涨跌榜扫描出错: {e}")
            await safe_reply(update.message, f"扫描失败，稍后再试：{str(e)[:80]}")
            return
    await safe_reply(update.message, build_text(rows, win, ex, scanned, stats, age),
                     reply_markup=kb(win, ex), parse_mode="Markdown")


async def from_btn(query, context, win, ex, force=False):
    """按钮换窗口/换所/重扫。换窗口读缓存——重扫要十几秒，而他只是想换个窗口看。"""
    uid = query.from_user.id
    cached = _cache.get(ex)
    need_scan = force or not (cached and time.time() - cached["ts"] < CACHE_TTL)
    if need_scan:
        async with busy.guard(uid, "dayrank") as ok:
            if not ok:
                await query.answer(f"上一次扫描还在跑（已 {busy.elapsed(uid, 'dayrank')} 秒）",
                                   show_alert=True)
                return
            await safe_edit(query, f"📅 扫 {EX_LABEL[ex]} 永续…（约十几秒）")
            try:
                rows, scanned, stats, age = await cached_scan(ex, force=force)
            except Exception as e:
                log.error(f"多日涨跌榜按钮扫描出错: {e}")
                await safe_edit(query, f"扫描失败：{str(e)[:80]}", reply_markup=kb(win, ex))
                return
    else:
        rows, scanned, stats, age = await cached_scan(ex)
    await safe_edit(query, build_text(rows, win, ex, scanned, stats, age),
                    reply_markup=kb(win, ex), parse_mode="Markdown")
