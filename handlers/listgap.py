"""跨所上币缺口 `/gapspot` `/gapfut` —— 两家有、第三家还没上的。

他看完 `/alpha` 说：「不全面吧，这个只拉币安现货的候选池。我想要一个命令
把三家交易所的现货候选池展示出来，再来一个把三家的合约候选池展示出来。」

## 为什么不能照字面做

**只有币安有公开的孵化池。** 实测：
  · Gate 的 Alpha / launchpad 接口 → **403 Access Denied**
  · Bybit 的 alpha / launchpool 接口 → **404**
所以「Gate 的候选池」「Bybit 的候选池」这两个东西，免费拿不到，不存在。

## 换的算法：跨所缺口

某个币在两家有、第三家没有 —— 那就是第三家的候选。这个不需要谁公布
候选池，用三家各自的上币列表一减就出来。

**而且它是验证过的**（不是拍脑袋）：Binance Alpha 里已经毕业到币安现货的
92 个币，**98% 现在也在 Gate/Bybit 现货上，98% 也在币安合约上**。
「别的所先上」和「上币安」几乎总是同时成立。

⚠️ 老实说这个验证有个洞：我只能看到**现在**的共同上市，看不到**先后**。
理论上可能是币安先上、别家跟进。所以这是个**强先验，不是因果**，
卡片上按"候选"说，不按"预测"说。

## 池子大小（剔掉代币化美股后）

    现货   币安缺 126   Gate 缺   3   Bybit 缺 157
    合约   币安缺  22   Gate 缺  17   Bybit 缺  45

Gate 现货只缺 3 个是因为它本来就上了 2042 个（币安 464、Bybit 387）——
覆盖最广的那家自然没什么可缺的。这不是 bug，是它的策略。

## 排序按成交额

按"另外两家里成交额最大的那个"排。市值在这里不如成交额：
**缺口的意思是「这个所的用户买不到」，成交额直接衡量有多少人在买。**
"""
import asyncio
import logging

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.util import safe_reply, safe_edit, escape_md

log = logging.getLogger(__name__)

TOP_N = 4               # 每家列几个。三家 ×（表头+4行+省略行）+ 头尾 = 23 行，
                        # 卡在 24 行预算内（5 行时合约那张 26 行，按钮被挤出屏幕）
CACHE_TTL = 600

_cache = {"ts": 0, "data": None}

# 交易所显示名 -> (现货集合键, 合约集合键)
VENUES = ("币安", "Gate", "Bybit")


async def _j(c, u, p=None):
    try:
        r = await c.get(u, params=p, timeout=25)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.info(f"[listgap] 取数失败 {u.split('/')[2]}: {e}")
        return None


async def fetch_all(force=False):
    """三家的现货/合约上币列表 + 成交额。→ {"spot":{所:集合}, "fut":…, "vol":{币:额}}

    **成交额跨所取最大值**：同一个币在 Gate 冷清、在币安火爆是常事，
    取最大才能反映"有多少人真的在买它"。
    """
    import time
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    spot, fut, vol = {}, {}, {}

    def bump(sym, v):
        if v > vol.get(sym, 0):
            vol[sym] = v

    async with httpx.AsyncClient() as c:
        (bs, bf, bst, bft, gs, gf, gt, ys, yf, yt) = await asyncio.gather(
            _j(c, "https://api.binance.com/api/v3/exchangeInfo"),
            _j(c, "https://fapi.binance.com/fapi/v1/exchangeInfo"),
            _j(c, "https://api.binance.com/api/v3/ticker/24hr"),
            _j(c, "https://fapi.binance.com/fapi/v1/ticker/24hr"),
            _j(c, "https://api.gateio.ws/api/v4/spot/currency_pairs"),
            _j(c, "https://api.gateio.ws/api/v4/futures/usdt/contracts"),
            _j(c, "https://api.gateio.ws/api/v4/spot/tickers"),
            _j(c, "https://api.bybit.com/v5/market/instruments-info",
               {"category": "spot"}),
            _j(c, "https://api.bybit.com/v5/market/instruments-info",
               {"category": "linear", "limit": 1000}),
            _j(c, "https://api.bybit.com/v5/market/tickers", {"category": "spot"}),
            return_exceptions=True)

    def ok(x):
        return x if not isinstance(x, Exception) else None

    bs, bf, bst, bft, gs, gf, gt, ys, yf, yt = map(ok, (bs, bf, bst, bft, gs, gf, gt, ys, yf, yt))

    if bs:
        spot["币安"] = {s["baseAsset"] for s in (bs.get("symbols") or [])
                        if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"}
    if bf:
        fut["币安"] = {s["baseAsset"] for s in (bf.get("symbols") or [])
                       if s.get("status") == "TRADING"}
    if gs:
        spot["Gate"] = {x["base"] for x in gs if x.get("quote") == "USDT"
                        and x.get("trade_status") == "tradable"}
    if gf:
        fut["Gate"] = {x["name"][:-5] for x in gf
                       if str(x.get("name", "")).endswith("_USDT")}
    if ys:
        spot["Bybit"] = {x["symbol"][:-4] for x in
                         ((ys.get("result") or {}).get("list") or [])
                         if str(x.get("symbol", "")).endswith("USDT")}
    if yf:
        fut["Bybit"] = {x["symbol"][:-4] for x in
                        ((yf.get("result") or {}).get("list") or [])
                        if str(x.get("symbol", "")).endswith("USDT")}
    for rows, sym_k, vol_k, cut in (
            (bst, "symbol", "quoteVolume", 4), (bft, "symbol", "quoteVolume", 4)):
        for x in (rows or []):
            s = str(x.get(sym_k) or "")
            if s.endswith("USDT"):
                try:
                    bump(s[:-cut], float(x.get(vol_k) or 0))
                except (TypeError, ValueError):
                    pass
    for x in (gt or []):
        p = str(x.get("currency_pair") or "")
        if p.endswith("_USDT"):
            try:
                bump(p[:-5], float(x.get("quote_volume") or 0))
            except (TypeError, ValueError):
                pass
    for x in ((yt or {}).get("result") or {}).get("list") or []:
        s = str(x.get("symbol") or "")
        if s.endswith("USDT"):
            try:
                bump(s[:-4], float(x.get("turnover24h") or 0))
            except (TypeError, ValueError):
                pass

    # 代币化美股必须剔——同一个坑第六次了。它们成交额大，不剔会霸占榜首，
    # 而"某家没上代币化美股"根本不是有价值的缺口
    try:
        from handlers.klines import noncrypto_bases
        from handlers.dayrank import _BSTOCK
        skip = set(await noncrypto_bases()) | _BSTOCK
    except Exception as e:
        log.warning(f"[listgap] 取非加密品类失败，本轮不剔代币化美股: {e}")
        skip = set()

    # ⚠️ **面值前缀必须归一，否则缺口是假的。** 真机第一跑就抓到：
    # 「Gate 合约缺 1000PEPE / 1000BONK」——那是币安和 Bybit 的千倍合约命名，
    # Gate 上就叫 PEPE / BONK，它一点都不缺。
    # 不归一的话每个千倍合约都会在两家的缺口里各冒出来一次，全是假的。
    from handlers.dayrank import norm_base
    for book in (spot, fut):
        for k in book:
            book[k] = {norm_base(x) for x in book[k] if x not in skip}
    # 成交额也要跟着归一，否则归一后的币名在 vol 里查不到（显示成 0）
    for s in list(vol):
        n = norm_base(s)
        if n != s:
            bump(n, vol[s])

    out = {"spot": spot, "fut": fut, "vol": vol, "skipped": len(skip)}
    _cache.update({"ts": now, "data": out})
    return out


def gaps(book):
    """→ {所: [币]}，按成交额降序。只在**三家的列表都拿到**时才算——
    某一家取数失败的话，它的"缺口"会变成"另外两家的全部"，一眼假。"""
    if len(book) < 3:
        return None
    out = {}
    for v in VENUES:
        others = [book[o] for o in VENUES if o != v]
        out[v] = sorted((others[0] & others[1]) - book[v])
    return out


def build_text(data, kind):
    book = data["spot"] if kind == "spot" else data["fut"]
    cn = "现货" if kind == "spot" else "合约"
    g = gaps(book)
    if not g:
        missing = [v for v in VENUES if v not in book]
        return (f"❌ 三家的{cn}列表没取全（缺 {'/'.join(missing) or '未知'}），"
                f"这一轮不算——少一家的话，它的「缺口」会变成另外两家的全部。")
    vol = data.get("vol") or {}
    lines = [f"🕳 *跨所上币缺口 · {cn}*",
             "另外两家都上了、这家还没上的——那就是这家的候选", ""]
    for v in VENUES:
        rows = sorted(g[v], key=lambda s: -vol.get(s, 0))
        lines.append(f"*{v}{cn}缺 {len(rows)} 个*" + ("：" if rows else "（没有）"))
        for s in rows[:TOP_N]:
            money = vol.get(s, 0)
            from handlers.liqmap import _money
            lines.append(f"　{escape_md(s)}　别家 24h 成交 {_money(money)}")
        if len(rows) > TOP_N:
            lines.append(f"　…还有 {len(rows) - TOP_N} 个")
    lines += [
        "",
        "按**别家的成交额**排：缺口的意思是「这家的用户买不到」，"
        "成交额直接衡量有多少人在买。口径点 ℹ️",
    ]
    return "\n".join(lines)


def detail_text():
    return "\n".join([
        "ℹ️ *跨所上币缺口 · 口径*", "━━━━━━━━━━━━━━",
        "*为什么不是「三家的候选池」*",
        "**只有币安有公开的孵化池**（Binance Alpha，`/alpha` 看）。实测：",
        "　· Gate 的 Alpha / launchpad 接口 → **403 Access Denied**",
        "　· Bybit 的 alpha / launchpool 接口 → **404**",
        "所以「Gate 的候选池」「Bybit 的候选池」免费拿不到，不存在。",
        "",
        "*换的算法*",
        "某个币在两家有、第三家没有 → 那就是第三家的候选。",
        "不需要谁公布候选池，三家各自的上币列表一减就出来。",
        "",
        "*这个算法验证过*",
        "Binance Alpha 里已经毕业到币安现货的 92 个币：",
        "　**98% 现在也在 Gate/Bybit 现货上**",
        "　**98% 也在币安合约上**",
        "「别家先上」和「上币安」几乎总是同时成立。",
        "",
        "⚠️ 但这个验证有个洞：我只能看到**现在**的共同上市，看不到**先后**。",
        "理论上可能是币安先上、别家跟进。所以这是**强先验不是因果**，",
        "按「候选」看，别按「预测」看。",
        "",
        "*池子大小*（剔掉代币化美股后）",
        "```",
        "        币安缺   Gate缺   Bybit缺",
        "现货      126       3       157",
        "合约       22      17        45",
        "```",
        "Gate 现货只缺 3 个，是因为它本来就上了 2042 个",
        "（币安 464、Bybit 387）——覆盖最广的那家自然没什么可缺的。",
        "",
        "*为什么按成交额不按市值*",
        "缺口的意思是「这家的用户买不到」，**成交额直接衡量有多少人在买**。",
        "（`/alpha` 那边按市值排，因为那问的是「够不够格上」，判据不一样）",
        "",
        "*三家有一家取数失败就整个不算*",
        "少一家的话，它的「缺口」会变成另外两家的全部——一个看着很吓人",
        "但完全错误的数字。宁可这一轮不出。",
        "",
        "剔掉了代币化美股（同一个坑第六次了）。",
        "⚠️ 候选不等于会上。不构成投资建议",
    ])


def kb(kind):
    other = "fut" if kind == "spot" else "spot"
    other_cn = "合约" if kind == "spot" else "现货"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔄 看{other_cn}缺口", callback_data=f"gp:{other}"),
         InlineKeyboardButton("🔮 币安候选池", callback_data="al:r")],
        [InlineKeyboardButton("ℹ️ 口径", callback_data="gp:i"),
         InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")],
    ])


async def render(kind, force=False):
    data = await fetch_all(force=force)
    return build_text(data, kind)


async def _cmd(update, kind):
    msg = await safe_reply(update.message, "🕳 比对三家的上币列表…")
    try:
        txt = await render(kind)
    except Exception as e:
        log.error(f"跨所缺口出错: {e}", exc_info=True)
        await safe_reply(update.message, f"取不到：{str(e)[:90]}")
        return
    if msg:
        try:
            await msg.edit_text(txt, parse_mode="Markdown", reply_markup=kb(kind))
            return
        except Exception:
            pass
    await safe_reply(update.message, txt, parse_mode="Markdown",
                     reply_markup=kb(kind))


async def gapspot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gapspot —— 三家现货的上币缺口。"""
    await _cmd(update, "spot")


async def gapfut_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gapfut —— 三家合约的上币缺口。"""
    await _cmd(update, "fut")


async def on_button(query, context):
    d = query.data
    if d == "gp:i":
        await safe_edit(query, detail_text(), parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            "⬅️ 回缺口表", callback_data="gp:spot")]]))
        return
    kind = "fut" if d.endswith(":fut") else "spot"
    await safe_edit(query, "🕳 比对三家的上币列表…")
    try:
        txt = await render(kind)
    except Exception as e:
        log.error(f"跨所缺口按钮出错: {e}", exc_info=True)
        await safe_edit(query, f"取不到：{str(e)[:90]}", reply_markup=kb(kind))
        return
    await safe_edit(query, txt, parse_mode="Markdown", reply_markup=kb(kind))
