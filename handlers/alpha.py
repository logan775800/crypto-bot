"""币安上币候选池 `/alpha` —— 哪些链上的币可能要上币安。

他问：「我可以提前知道币安哪些链上的币要上交易所吗？」

## 先说清楚这不是「预知」

币安不公布上币计划，任何声称能预测的都是编的。**能拿到的是官方候选池**：
Binance Alpha 是币安自己的上币前孵化位，免 key 一个接口给全部代币，
带链名、合约地址、流动性、市值、持币数。

实测（2026-09-02）：**池子里 666 个代币，其中 92 个已经毕业到币安现货**
—— 基础概率约 **14%**。所以这个功能给的是「把 666 个缩到十几个值得盯的」，
不是「这个会上」。这句话必须印在卡片上，否则它会被当成内幕消息用。

## 三个验过之后发现靠不住的东西（别再拿它们当判据）

**① `listingCex` 字段不是「币安要上」。** 117 个标 true，其中 32 个还没上
币安现货——但交叉核对 Gate / Bybit / 币安合约之后，**21 个其实已经上了别家**。
这字段的含义是「已上某个 CEX」。

**② 合约的 `PENDING_TRADING` 不是提前量。** 唯一那个 GAIBUSDT 的 onboardDate
是 287 天前，是卡住的旧条目；而且没有任何合约的 onboardDate 在未来。

**③ 币安自己给的 `score` 完全无区分度**（已毕业和没毕业的中位数都是 11）。

## 唯一真有区分度的是市值

已毕业的 92 个 vs 没毕业的 574 个，中位数对比：

    市值        $3348 万  vs  $315 万   10.6x   ← 只有这个能用
    24h 成交量    32,381  vs   14,301    2.3x
    流动性       $311k    vs   $162k     1.9x
    持币数       10,247   vs   13,182    0.8x   ← **反的**，凭直觉会用错
    币安 score       11   vs       11    1.0x

**持币人数是反的**（没毕业的反而更多），这条最反直觉，所以排序只用市值。

## 两条路，A 是底料 B 才是「提前」

    A `/alpha`     随时查：池子里还没上币安现货的，按市值排
    B 变动告警      每小时对比名单，**新进池的**立刻推

B 才是真正的提前量：市值排序只能说「这个已经够大了」，
而新进 Alpha 是「币安刚把它放进候选」——那是更早的一步。

## 代币化美股必须剔（同一个坑第五次了）

池子里有 80 个 Ondo 的代币化美股（XOMon / TSLAon / COINon…），
`stockState=True` 正好圈出全部 80 个，一个字段就够。
不剔的话它们会因为市值大而霸占榜首——而它们根本不会「上币安现货」。
"""
import logging
import re
import time

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from storage import data, save_data
from handlers.util import safe_reply, safe_edit, escape_md

log = logging.getLogger(__name__)

ALPHA = ("https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw"
         "/wallet/cex/alpha/all/token/list")
# 这个接口在浏览器里才有的头。缺 Referer 有时会被 WAF 挡下来。
HDRS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
        "Referer": "https://www.binance.com/"}

TOP_N = 12              # 榜单列几个。再多一屏放不下，按钮会被挤出去
NEW_PER_HOUR = 6        # 变动告警的兜底闸
MIN_MCAP = 1_000_000    # 市值低于这个的新进池不推——太小的进池是常态，不是信号
CACHE_TTL = 600

_cache = {"ts": 0, "rows": None}


def _cfg():
    return data.setdefault("alpha", {})


def subs():
    return [str(x) for x in (_cfg().get("on") or [])]


def is_on(chat_id):
    return str(chat_id) in subs()


def toggle(chat_id, on):
    c = _cfg()
    lst = subs()
    k = str(chat_id)
    if on and k not in lst:
        lst.append(k)
    if not on:
        lst = [x for x in lst if x != k]
    c["on"] = lst
    save_data()
    return on


def _f(x, k, dv=0.0):
    try:
        return float(x.get(k) or dv)
    except (TypeError, ValueError):
        return dv


def is_stock(x):
    """代币化美股/RWA。实测 `stockState` 和 `rwaInfo` 圈出的是**同一批 80 个**
    （交集 80、各自独有 0），任取一个即可，两个都判是为了将来某一个被改掉。

    不剔的话它们会因为市值大霸占榜首——而 Ondo 的代币化美股根本不是
    「等着上币安现货」的东西。同一个坑第五次了（微市值榜、涨跌榜、
    多空比榜、爆仓一边倒各踩过一次）。"""
    return bool(x.get("stockState") or x.get("rwaInfo"))


async def fetch(force=False):
    """→ Alpha 全量代币。缓存 10 分钟：名单一天也变不了几次。"""
    now = time.time()
    if not force and _cache["rows"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["rows"]
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get(ALPHA, headers=HDRS)
    if r.status_code != 200:
        raise RuntimeError(f"Alpha 名单取不到（HTTP {r.status_code}）")
    rows = (r.json() or {}).get("data")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Alpha 名单是空的，接口可能改了")
    _cache.update({"ts": now, "rows": rows})
    return rows


async def binance_spot():
    """币安现货已经在交易的币。判「毕业了没有」要靠它。"""
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get("https://api.binance.com/api/v3/exchangeInfo")
    if r.status_code != 200:
        return set()
    return {s["baseAsset"] for s in (r.json().get("symbols") or [])
            if s.get("status") == "TRADING"}


def sym_of(x):
    return str(x.get("symbol") or "").upper()


def candidates(rows, spot):
    """还没上币安现货、且不是代币化美股的。按**市值**排——
    实测那是唯一有区分度的指标（10.6x），流动性只有 1.9x、
    持币数 0.8x（反的）、币安自己的 score 1.0x（完全没用）。"""
    out = [x for x in rows
           if sym_of(x) and sym_of(x) not in spot
           and not is_stock(x) and not x.get("offline")]
    return sorted(out, key=lambda x: -_f(x, "marketCap"))


def graduated(rows, spot):
    """已经从 Alpha 毕业到币安现货的。用来报基础概率——
    **没有基础概率，"市值最大的十个"这句话没有意义**。"""
    return [x for x in rows if sym_of(x) in spot and not is_stock(x)]


def _money(v):
    from handlers.liqmap import _money as m
    return m(v)


def line(x, i=None):
    n = f"{i}. " if i else "　"
    return (f"{n}*{escape_md(sym_of(x))}*　{x.get('chainName') or '?'}　"
            f"市值 {_money(_f(x, 'marketCap'))}　"
            f"流动性 {_money(_f(x, 'liquidity'))}")


def build_text(rows, spot):
    cand = candidates(rows, spot)
    grad = graduated(rows, spot)
    total = len([x for x in rows if not is_stock(x)])
    rate = len(grad) / total * 100 if total else 0
    lines = [
        "🔮 *币安上币候选池*（Binance Alpha）",
        "",
        f"池子里 {total} 个链上代币，**已经毕业到币安现货的有 {len(grad)} 个**"
        f"（{rate:.0f}%）。下面是还没毕业、市值最大的 {min(TOP_N, len(cand))} 个：",
        "",
    ]
    for i, x in enumerate(cand[:TOP_N], 1):
        lines.append(line(x, i))
    # 细节收进 ℹ️。整张卡有 24 行预算（超了按钮会被挤出屏幕），
    # 第一版把那张对比表印在卡上，直接 32 行。
    lines += [
        "",
        "按**市值**排——实测那是唯一有区分度的指标（10.6x），"
        "流动性只有 1.9x、持币数 0.8x（反的）。口径点 ℹ️",
        f"⚠️ **这不是预知上币。** 币安不公布上币计划，基础概率就 {rate:.0f}%——"
        f"这里做的是把几百个缩到十几个值得盯的。",
    ]
    return "\n".join(lines)


def base_rate(rows, spot):
    """已毕业的占比。**算出来，不要写死**——第一版卡片上算的是 16%，
    而新进池那条卡片写死了 14%，两个数当场打架。"""
    total = len([x for x in rows if not is_stock(x)])
    return (len(graduated(rows, spot)) / total * 100) if total else 0.0


def detail_text():
    return "\n".join([
        "ℹ️ *上币候选池 · 口径*", "━━━━━━━━━━━━━━",
        "*这是什么*",
        "Binance Alpha 是币安自己的**上币前孵化位**。这里列的是池子里",
        "**还没上币安现货**的那些，按市值排。",
        "",
        "*为什么按市值排*",
        "已毕业的 92 个 vs 没毕业的 574 个，中位数一比：",
        "```",
        "市值      3348万 vs 315万   10.6x  ← 只有这个能用",
        "24h量     32,381 vs 14,301   2.3x",
        "流动性     $311k vs $162k    1.9x",
        "持币数    10,247 vs 13,182   0.8x  ← 反的",
        "币安score     11 vs     11   1.0x  ← 没用",
        "```",
        "**持币人数是反的**（没毕业的反而更多），这条最反直觉；",
        "币安自己给的 score 完全无区分度。两个凭直觉都会拿来用的指标，",
        "实测都没用。",
        "",
        "*三个验过之后靠不住的东西*",
        "· `listingCex` **不是**「币安要上」：117 个标 true，其中 32 个没上",
        "　币安现货，但交叉核对后 21 个已经上了 Gate / Bybit / 币安合约。",
        "　它的含义是「已上某个 CEX」。",
        "· 合约的 `PENDING_TRADING` **不是提前量**：唯一那个 GAIBUSDT 的",
        "　onboardDate 是 287 天前，卡住的旧条目；没有合约的时间在未来。",
        "· 公告接口能打通但很快被 WAF 挡，而且公告发出来时币已经在涨了。",
        "",
        "*剔掉了什么*",
        "80 个 Ondo 的代币化美股（XOMon / TSLAon / COINon…）。",
        "它们市值大，不剔会霸占榜首，而它们根本不是「等着上币安现货」的东西。",
        "",
        "*怎么用*",
        "命令这条是**底料**，真正的「提前」是**新进池告警**：",
        "市值排序只能说「这个已经够大了」，而新进 Alpha 是",
        "「币安刚把它放进候选」——那是更早的一步。",
        "",
        "⚠️ 候选不等于会上。不构成投资建议",
    ])


# ── B：变动告警 ─────────────────────────────────────────────
def _seen():
    return _cfg().setdefault("seen", {})


def diff_new(rows, now=None):
    """这一轮新进池的。→ [代币]。

    **首轮只建基线不告警**：第一次跑的时候整个池子都是"新"的，
    不挡的话会一次推 600 条。这个坑各处告警都踩过，写在最前面。
    """
    now = now or time.time()
    seen = _seen()
    first = not seen
    fresh = []
    for x in rows:
        tid = str(x.get("tokenId") or x.get("contractAddress") or "")
        if not tid:
            continue
        if tid not in seen:
            if not first:
                fresh.append(x)
            seen[tid] = int(now)
    # 封顶，否则 data.json 会一直长
    if len(seen) > 3000:
        for k in sorted(seen, key=lambda k: seen[k])[:len(seen) - 3000]:
            seen.pop(k, None)
    return fresh


def quota_left():
    c = _cfg()
    b = int(time.time() // 3600)
    if c.get("hour") != b:
        c["hour"] = b
        c["sent"] = 0
    return max(0, NEW_PER_HOUR - int(c.get("sent") or 0))


def _used(n=1):
    quota_left()
    c = _cfg()
    c["sent"] = int(c.get("sent") or 0) + n


def format_new(x, rate=16.0):
    addr = x.get("contractAddress") or ""
    lines = [
        "🔮 *进了币安上币候选池*",
        f"*{escape_md(sym_of(x))}*　{x.get('chainName') or '?'}",
        "",
        f"市值 {_money(_f(x, 'marketCap'))}　流动性 {_money(_f(x, 'liquidity'))}",
        f"持币 {x.get('holders') or '?'}　24h {_f(x, 'percentChange24h'):+.1f}%",
    ]
    if addr:
        lines += ["", f"合约 `{addr}`", f"查它：`/oc {addr}`"]
    lines += [
        "",
        "**新进 Alpha = 币安刚把它放进候选**，这比按市值排更早一步。",
        f"⚠️ 但候选不等于会上：池子里已毕业的只占 {rate:.0f}%。不构成投资建议",
    ]
    return "\n".join(lines)


async def scan(context: ContextTypes.DEFAULT_TYPE):
    chats = subs()
    if not chats:
        return
    try:
        rows = await fetch(force=True)
    except Exception as e:
        log.warning(f"[alpha] 取名单失败: {e}")
        return
    fresh = diff_new(rows)
    save_data()
    if not fresh:
        return
    # 太小的新进池是常态不是信号。**不设这个闸的话每天几十条**
    big = [x for x in fresh
           if not is_stock(x) and _f(x, "marketCap") >= MIN_MCAP]
    big.sort(key=lambda x: -_f(x, "marketCap"))
    try:
        rate = base_rate(rows, await binance_spot())
    except Exception as e:
        log.info(f"[alpha] 算基础概率失败，用上次量到的值: {e}")
        rate = 16.0
    for x in big:
        if quota_left() <= 0:
            break
        text = format_new(x, rate)
        for cid in chats:
            try:
                await context.bot.send_message(int(cid), text,
                                               parse_mode="Markdown")
            except Exception as e:
                log.warning(f"[alpha] 推送失败 {cid}: {e}")
        _used(1)
    if big:
        save_data()


# ── 入口 ────────────────────────────────────────────────────
def kb(chat_id):
    on = is_on(chat_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 关闭新进池告警" if on else "🟢 开启新进池告警",
                              callback_data="al:toggle")],
        [InlineKeyboardButton("ℹ️ 口径", callback_data="al:i"),
         InlineKeyboardButton("🔄 刷新", callback_data="al:r"),
         InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")],
    ])


async def render(chat_id, force=False):
    rows = await fetch(force=force)
    spot = await binance_spot()
    txt = build_text(rows, spot)
    if is_on(chat_id):
        txt += "\n\n✅ 已开新进池告警（有币新进 Alpha 就推给你）"
    else:
        txt += "\n\n⬜ 新进池告警没开——点下面开，那才是「提前」的部分"
    return txt


async def alpha_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alpha —— 币安上币候选池（Binance Alpha）。"""
    chat_id = update.effective_chat.id
    args = [str(a).lower() for a in (context.args or [])]
    if args and args[0] in ("on", "开", "订阅"):
        toggle(chat_id, True)
    elif args and args[0] in ("off", "关", "退订"):
        toggle(chat_id, False)
        await safe_reply(update.message, "🔕 已关闭新进池告警")
        return
    msg = await safe_reply(update.message, "🔮 拉币安候选池…")
    try:
        txt = await render(chat_id)
    except Exception as e:
        log.error(f"/alpha 出错: {e}", exc_info=True)
        await safe_reply(update.message, f"取不到候选池：{str(e)[:90]}")
        return
    if msg:
        try:
            await msg.edit_text(txt, parse_mode="Markdown",
                                reply_markup=kb(chat_id))
            return
        except Exception:
            pass
    await safe_reply(update.message, txt, parse_mode="Markdown",
                     reply_markup=kb(chat_id))


async def on_button(query, context):
    cid = query.message.chat_id
    d = query.data
    if d == "al:i":
        await safe_edit(query, detail_text(), parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            "⬅️ 回榜单", callback_data="al:back")]]))
        return
    if d == "al:toggle":
        on = toggle(cid, not is_on(cid))
        try:
            await query.answer("🔮 已开启" if on else "🔕 已关闭")
        except Exception:
            pass
    await safe_edit(query, "🔮 拉币安候选池…")
    try:
        txt = await render(cid, force=(d == "al:r"))
    except Exception as e:
        log.error(f"alpha 按钮出错: {e}", exc_info=True)
        await safe_edit(query, f"取不到候选池：{str(e)[:90]}",
                        reply_markup=kb(cid))
        return
    await safe_edit(query, txt, parse_mode="Markdown", reply_markup=kb(cid))
