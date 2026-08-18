"""微市值扫描 —— 四家交易所里**流通市值低于阈值**、而且真能下单的币。

为什么是「从交易所往回筛」而不是「从市值榜往下捞」：
实测（2026-08-18）市值 $300 万落在 CoinGecko 排名 **1831** 名，往下还有一万多个币，
绝大多数是没有任何交易所上架的灰尘——那一段随手抓一把，24h 成交量常常只有几千
美元，个别是 0。从那头捞，捞上来的九成下不了单。
交易所侧是**有限集合**：四家的 USDT 现货+永续加起来千把个基名，逐个配上市值，
剩下的才是「市值小 + 买得到」的交集，也就是他真正要的东西。

口径（他 2026-08-18 明确选的）：
  • **流通市值**（CoinGecko market_cap），不是 FDV。
    但低流通高 FDV 的币解锁就是砸盘，所以卡片上把 FDV 和流通率一并标出来，
    流通率过低的直接打警示——筛选口径可以简单，展示不能瞒着他。
  • 现货 + 永续都扫，结果里写明哪家能买、是现货还是合约。

**"没有市值数据" ≠ "市值很小"**：CoinGecko 查不到的币一律不进结果，
单独计数报出来。把未知当成 0 是这类扫描最容易犯、也最难发现的错。
"""
import asyncio
import logging
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.util import safe_reply, safe_edit, escape_md

log = logging.getLogger(__name__)

MAX_MCAP = 3_000_000        # 默认上限：流通市值 $300 万
MCAP_CEILING = 100_000_000  # /microcap 参数能设的最大值，再高就不叫微盘了
# 24h 成交额下限。微盘本来就没量，门槛不能照搬 /scan 的 2000 万；
# 但低于这个数连一笔像样的单都吃不掉，列出来只会浪费他的时间。
MIN_TURNOVER = 100_000
LOW_FLOAT_PCT = 30          # 流通率低于此 → 解锁砸盘风险，卡片上警示
TOP_SHOW = 12

# 24h 成交额 / 市值 的上限。超过就不是"微盘"，是**配错币了**。
# 实测（2026-08-18 首次真跑）：币安现货的 UTK 配到了 CoinGecko 的
# 「UNITE THE KINGDOM」，市值 $11,386 而当天成交 $1020 万——896 倍。
# BB 配到「BitBoard」而不是 BounceBit，9 倍。真微盘这个比值实测在 1~2 倍：
# 一天换手 5 次整个流通盘已经极端了，再高就是同名币或已迁移的旧合约。
MAX_TURNOVER_RATIO = 5

# 代币化美股的识别。klines.noncrypto_bases() 查的是**合约**接口的品类字段，
# 只在币安现货上市的这批（QQQB/SOXSB/MRVLB…）它一个都认不出来，
# 首次真跑时微市值榜前十有一半是美股 ETF。
# 好在 CoinGecko 那边名字和 id 里明写着，按标记词认最省事也最准。
STOCK_MARKERS = ("tokenized stock", "bstocks", "tokenized-stock",
                 "tokenized equity", "xstock")

# 市值表缓存：CoinGecko 免费配额很小（实测 2.5 秒间隔翻到第 6 页就 429），
# 这张表一天变不了几个百分点，缓存 6 小时足够，也让命令第二次是秒回。
MCAP_TTL = 6 * 3600
_mcap_cache = {"at": 0.0, "table": None, "pages": 0, "tried": set()}
_list_cache = {"at": 0.0, "map": None}
_LIST_TTL = 24 * 3600
# 市值榜翻多少页。**16 页 ≈ 排名 4000**，覆盖到几十万市值。
# 第一版只翻 8 页（排名 2000 ≈ $275 万），结果 300 万这一档的币几乎全落在榜外，
# 只能靠按代号回查——而回查两千个代号必然被限频打死，于是「300万档 0 个、
# 500万档却列着一堆 200 万市值的币」。翻页比按代号回查便宜得多：
# 一页 250 个币一次请求，回查是一个代号最多 12 个候选。
PAGES = 16
MIN_USEFUL_MCAP = 300_000   # 翻到这个市值就停：再往下的币没有交易所会上
# 翻页节奏。CoinGecko 免费档很小气，连着打必被 429；宁可慢，也不要缺页——
# 缺页缺的正好是微市值那一段。反正这活是后台干的（见 prebuild），没人等着。
PAGE_GAP = 3
PAGE_RETRY_WAIT = 20
# 榜外回查每轮最多补几个代号。回查是配额黑洞，必须封顶——
# 翻够页数之后，落在榜外的基本是 CoinGecko 根本没收录的币，补不补都一样。
LOOKUP_SYMS = 60

VENUE_CN = {("bybit", "spot"): "Bybit现货", ("bybit", "swap"): "Bybit永续",
            ("okx", "spot"): "OKX现货", ("okx", "swap"): "OKX永续",
            ("binance", "spot"): "币安现货", ("binance", "swap"): "币安永续",
            ("gate", "spot"): "Gate现货", ("gate", "swap"): "Gate永续"}


# ── 交易所侧：谁能买 ─────────────────────────────────────────
async def tradable():
    """四家 × 现货/永续 → {基名: {venues, turnover, change, price, crypto}}。

    turnover 取**各家之和**（总盘子有多大），另外记住最大的那一家——
    真下单是在一家下的，总量再大也不代表某一家吃得下。
    """
    from handlers import klines as kl
    jobs, keys = [], []
    for ex in ("bybit", "okx", "binance", "gate"):
        for market in (kl.SPOT, kl.SWAP):
            jobs.append(kl.universe(ex, market))
            keys.append((ex, market))
    results = await asyncio.gather(*jobs, return_exceptions=True)

    out = {}
    for (ex, market), rows in zip(keys, results):
        if isinstance(rows, Exception):
            log.warning(f"微市值扫描取 {ex}/{market} 失败: {rows}")
            continue
        for r in rows:
            sym = r["symbol"]
            e = out.setdefault(sym, {"venues": [], "turnover": 0.0, "change": 0.0,
                                     "price": 0.0, "crypto": True, "best": (None, 0.0)})
            tv = r.get("turnover") or 0.0
            e["venues"].append((ex, market))
            e["turnover"] += tv
            if tv > e["best"][1]:
                e["best"] = (VENUE_CN.get((ex, market), f"{ex}{market}"), tv)
            if r.get("price"):
                e["price"] = r["price"]
                e["change"] = r.get("change") or 0.0
            # 任一家标成非加密就当非加密（代币化股票/杠杆代币，同 klines.universe 的口径）
            if not r.get("crypto", True):
                e["crypto"] = False
    return out


# ── 市值侧：CoinGecko ────────────────────────────────────────
async def _top_table():
    """市值榜前 PAGES*250 名 → {代号: 行}。同代号保留**市值最高**的那个。

    保留最高的那个是有意为之：同一个代号常有一堆同名山寨（真 SUN 和垃圾 SUN），
    如果挑到了小的那个，就会把一个几亿市值的币误报成"微盘"——
    这种错比漏报危险得多，他会照着去买。

    返回 (表, 拿到几页)。**页数要往上报**：CoinGecko 免费配额很小，缺页会让一批
    真币掉进"榜外"分支去按代号猜，猜错就成了同名误判。缺页必须让用户看见。
    """
    from api import _get
    table, ok = {}, 0
    for page in range(1, PAGES + 1):
        rows = None
        for attempt in range(3):
            try:
                rows = await _get("/coins/markets", {
                    "vs_currency": "usd", "order": "market_cap_desc",
                    "per_page": 250, "page": page})
                break
            except Exception as e:
                log.warning(f"市值榜第 {page} 页第 {attempt+1} 次失败: {e}")
                await asyncio.sleep(PAGE_RETRY_WAIT * (attempt + 1))
        if rows is None:
            # 某一页拿不到就**跳过它继续下一页**，不要 break。
            # 表是按代号存的，缺一页只是少那 250 个币；break 会把后面十页
            # 一起丢掉，而微市值恰恰全在后面几页——首版就是这么扫出 0 个的。
            continue
        if not rows:
            break
        ok += 1
        for c in rows:
            sym = (c.get("symbol") or "").upper()
            if sym and sym not in table:
                table[sym] = c
        caps = [c.get("market_cap") or 0 for c in rows]
        if caps and min(caps) < MIN_USEFUL_MCAP:
            break                    # 已经翻到没人上交易所的尘埃段，再翻是浪费配额
        await asyncio.sleep(PAGE_GAP)
    return table, ok


async def _symbol_ids():
    """CoinGecko 全量币表 → {代号: [id, ...]}。一天一次，用来查榜外的币。"""
    now = time.monotonic()
    if _list_cache["map"] is not None and now - _list_cache["at"] < _LIST_TTL:
        return _list_cache["map"]
    from api import _get
    try:
        rows = await _get("/coins/list", {})
    except Exception as e:
        log.warning(f"取 CoinGecko 币表失败: {e}")
        return _list_cache["map"] or {}
    m = {}
    for c in rows:
        m.setdefault((c.get("symbol") or "").upper(), []).append(c.get("id"))
    _list_cache.update(at=now, map=m)
    return m


async def _lookup(symbols):
    """榜外代号 → 市值行。分块查 ids，同代号仍取市值最高的那个。"""
    if not symbols:
        return {}
    from api import _get
    id_map = await _symbol_ids()
    ids, owner = [], {}
    for s in symbols:
        # 候选给到 12 个：只试前几个会漏掉真币（实测 BB 配到了 BitBoard，
        # 因为 BounceBit 在 /coins/list 里排在更后面），漏掉就成了同名误判
        for cid in (id_map.get(s) or [])[:12]:
            ids.append(cid)
            owner[cid] = s
    out = {}
    for i in range(0, len(ids), 250):
        chunk = ids[i:i + 250]
        try:
            rows = await _get("/coins/markets", {
                "vs_currency": "usd", "ids": ",".join(chunk),
                "per_page": 250, "page": 1})
        except Exception as e:
            log.warning(f"查榜外市值失败({i}): {e}")
            continue
        for c in rows:
            sym = owner.get(c.get("id"))
            mc = c.get("market_cap") or 0
            if not sym:
                continue
            prev = out.get(sym)
            if prev is None or mc > (prev.get("market_cap") or 0):
                out[sym] = c
    return out


async def mcap_table(symbols):
    """返回 ({代号: 市值行}, 市值榜拿到几页)。整张表缓存 MCAP_TTL。

    **缓存命中条件不能是"所有代号都在表里"**：交易所上千个基名里总有一批
    CoinGecko 压根没收录，那个条件永远为假 → 缓存等于没有 → 每次扫描都重拉
    十几页再回查上千个代号 → 必被限频 → 同一批数据两次扫出来的结果对不上。
    （首版就是这个 bug：300万档 0 个、500万档却有一堆 200 万市值的币。）
    所以：表新鲜就直接用；只补**这轮才第一次见到**的代号，查过没有的记在 tried 里
    不再反复查。
    """
    now = time.monotonic()
    table = _mcap_cache["table"]
    if table is not None and now - _mcap_cache["at"] < MCAP_TTL:
        fresh = [s for s in symbols
                 if s not in table and s not in _mcap_cache["tried"]]
        if fresh:
            _mcap_cache["tried"].update(fresh[:LOOKUP_SYMS])
            table.update(await _lookup(fresh[:LOOKUP_SYMS]))
        return table, _mcap_cache["pages"]

    table, pages = await _top_table()
    outside = [s for s in symbols if s not in table][:LOOKUP_SYMS]
    _mcap_cache.update(at=now, table=table, pages=pages, tried=set(outside))
    table.update(await _lookup(outside))
    return table, pages


# ── 扫描 ────────────────────────────────────────────────────
def _float_pct(row):
    """流通率 %。拿不到就返回 None——别用 FDV/市值 硬凑，两个都可能是空。"""
    circ = row.get("circulating_supply") or 0
    total = row.get("total_supply") or 0
    if circ > 0 and total > 0:
        return circ / total * 100
    return None


def is_stock(row):
    """代币化美股/ETF？看 CoinGecko 的名字和 id。"""
    blob = f"{row.get('name') or ''} {row.get('id') or ''}".lower()
    return any(m in blob for m in STOCK_MARKERS)


async def run(max_mcap=MAX_MCAP, min_turnover=MIN_TURNOVER):
    """返回 (命中列表, 统计)。命中按 24h 成交额降序——先能下单，再谈便宜。"""
    tr = await tradable()
    table, pages = await mcap_table(list(tr.keys()))

    hits, no_data, too_thin, not_crypto, mismatched = [], 0, 0, 0, 0
    for sym, e in tr.items():
        if not e["crypto"]:
            not_crypto += 1
            continue
        row = table.get(sym)
        mc = (row or {}).get("market_cap") or 0
        if not row or mc <= 0:
            no_data += 1            # 查不到 ≠ 市值小，绝不算命中
            continue
        if is_stock(row):
            not_crypto += 1         # 代币化美股，交易所接口那层认不出来
            continue
        if mc >= max_mcap:
            continue
        if e["turnover"] < min_turnover:
            too_thin += 1
            continue
        if e["turnover"] > mc * MAX_TURNOVER_RATIO:
            # 一天换手好几倍流通盘 = 配错币了（同名/旧合约），不是发现了宝藏
            mismatched += 1
            continue
        hits.append({
            "symbol": sym, "mcap": mc, "rank": row.get("market_cap_rank"),
            "fdv": row.get("fully_diluted_valuation") or 0,
            "float_pct": _float_pct(row),
            "turnover": e["turnover"], "best": e["best"],
            "change": e["change"], "price": e["price"],
            "venues": [VENUE_CN.get(v, f"{v[0]}{v[1]}") for v in e["venues"]],
        })
    hits.sort(key=lambda h: -h["turnover"])
    return hits, {"universe": len(tr), "no_data": no_data,
                  "thin": too_thin, "noncrypto": not_crypto,
                  "mismatched": mismatched, "pages": pages}


def _m(x):
    """金额缩写：$2.93M / $412K。微盘的数字全写出来太长，一行放不下。"""
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.0f}K"
    return f"${x:.0f}"


BANDS = ((3_000_000, "300万以下"), (5_000_000, "300~500万"),
         (10_000_000, "500~1000万"), (float("inf"), "1000万以上"))


def band_line(hits, max_mcap):
    """分档统计。按成交额排序时，大的那档会把小的挤出显示区——
    他点「1000万」看到的全是 600~900 万的币，就以为 300 万档是空的。
    先把每档各有几个摆出来，再看名单。"""
    counts, lo = [], 0
    for hi, name in BANDS:
        if lo >= max_mcap:
            break
        n = sum(1 for h in hits if lo <= h["mcap"] < min(hi, max_mcap))
        counts.append(f"{name} {n} 个")
        lo = hi
    return "分档：" + "｜".join(counts)


def render(hits, stats, max_mcap=MAX_MCAP, min_turnover=MIN_TURNOVER, limit=TOP_SHOW):
    cap = f"{max_mcap/10_000:.0f}万" if max_mcap < 100_000_000 else _m(max_mcap)
    head = [f"💎 *微市值扫描*　流通市值 < ${cap}",
            "四家交易所的现货+永续，**能下单的**才列出来",
            f"扫了 {stats['universe']} 个基名｜"
            f"量太小剔除 {stats['thin']}｜查不到市值 {stats['no_data']}｜"
            f"代币化股票等 {stats['noncrypto']}｜"
            f"市值对不上剔除 {stats.get('mismatched', 0)}"]
    if stats.get("pages", PAGES) < PAGES:
        head.append(f"⚠️ 市值榜只取到 {stats['pages']}/{PAGES} 页（行情源限频），"
                    f"部分币是按代号回查的，同名币可能配错——过一会儿再刷一次")
    head.append("━━━━━━━━━━━━━━")
    if not hits:
        tail = [f"这一档现在**一个都没有**。"]
        if stats.get("pages", PAGES) < PAGES:
            # 空结果 + 数据没取全 = 大概率是限频漏了，不是真的没有。别让他白等
            tail.append("但市值表这次没取全，**很可能是漏了而不是真没有**，"
                        "过几分钟再点一次。")
        else:
            tail.append(f"交易所上架本身就是个门槛——市值低于 ${cap} 又还有 "
                        f"{_m(min_turnover)} 以上日成交额的币，本来就很少。")
        return "\n".join(head + tail + ["把上限放宽试试：`/microcap 1000`（1000万）"])

    lines = list(head)
    lines.append(band_line(hits, max_mcap))
    lines.append("━━━━━━━━━━━━━━")
    for i, h in enumerate(hits[:limit], 1):
        rank = f"#{h['rank']}" if h["rank"] else "无排名"
        lines.append(f"*{i}. {escape_md(h['symbol'])}*　市值 {_m(h['mcap'])}　{rank}")
        lines.append(f"　24h量 {_m(h['turnover'])}　{h['change']:+.1f}%"
                     f"　主场 {escape_md(h['best'][0] or '?')}")
        fdv_bits = []
        if h["fdv"] and h["fdv"] > h["mcap"] * 1.2:
            fdv_bits.append(f"FDV {_m(h['fdv'])}")
        if h["float_pct"] is not None:
            fdv_bits.append(f"流通 {h['float_pct']:.0f}%")
            if h["float_pct"] < LOW_FLOAT_PCT:
                fdv_bits.append("⚠️解锁砸盘风险")
        if fdv_bits:
            lines.append("　" + "　".join(escape_md(b) for b in fdv_bits))
        lines.append("　可买：" + escape_md("、".join(h["venues"][:4])))
    if len(hits) > limit:
        lines.append(f"还有 {len(hits)-limit} 个未显示")
    lines += ["━━━━━━━━━━━━━━",
              f"筛掉了 24h 量 < {_m(min_turnover)} 的：这个市值段大量币一天成交几千美元，"
              f"列出来也下不了单",
              f"也筛掉了成交额超市值 {MAX_TURNOVER_RATIO} 倍的：那是同名币配错了，"
              f"不是宝藏",
              "⚠️ 微盘波动和插针都极大，仓位按能全亏得起来定"]
    return "\n".join(lines)


def kb(max_mcap=MAX_MCAP):
    """当前档位打勾，刷新按钮沿用当前档位——原来刷新写死回 300 万，
    点了「1000万」再点刷新会莫名其妙跳回去。"""
    def one(wan):
        cur = abs(max_mcap - wan * 10_000) < 1
        return InlineKeyboardButton(("✅ " if cur else "") + f"{wan}万",
                                    callback_data=f"mc:{wan}")
    return InlineKeyboardMarkup([
        [one(300), one(500), one(1000)],
        [InlineKeyboardButton("🔄 刷新", callback_data=f"mc:{max_mcap/10_000:.0f}"),
         InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")]])


async def _scan_to(send, max_mcap):
    try:
        hits, stats = await run(max_mcap)
    except Exception as e:
        log.error(f"微市值扫描失败: {e}")
        await send(f"扫描失败：{str(e)[:80]}")
        return
    await send(render(hits, stats, max_mcap))


async def prebuild(context):
    """后台预建市值表。启动后跑一次，之后每 6 小时一次。

    为什么必须放后台：翻 16 页 + 限频退避要一两分钟，让用户对着"正在扫描"干等
    是一回事，**中途被限频截断**是更糟的一回事——截断少的正好是市值最小那几页，
    于是 300 万档扫出 0 个而 1000 万档一堆。后台慢慢建，命令直接读现成的。
    """
    try:
        tr = await tradable()
        table, pages = await mcap_table(list(tr.keys()))
        log.info(f"微市值：市值表预建完成 {len(table)} 个代号，{pages}/{PAGES} 页")
    except Exception as e:
        log.warning(f"微市值：市值表预建失败: {e}")


async def microcap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/microcap [市值上限(万)] —— 不带参数就是 300 万。"""
    max_mcap = MAX_MCAP
    if context.args:
        try:
            v = float(context.args[0]) * 10_000
            max_mcap = max(100_000, min(MCAP_CEILING, v))
        except ValueError:
            pass
    msg = await update.message.reply_text(
        f"💎 正在扫四家交易所（现货+永续）找市值 < "
        f"${max_mcap/10_000:.0f}万 的币…\n首次要拉市值表，约 20~40 秒")

    async def send(text):
        await safe_reply(update.message, text, reply_markup=kb(max_mcap),
                         parse_mode="Markdown")
        try:
            await msg.delete()
        except Exception:
            pass

    await _scan_to(send, max_mcap)


async def on_button(query, context):
    """处理 mc:* 回调。由 menu 转发。"""
    bits = query.data.split(":")
    try:
        max_mcap = float(bits[1]) * 10_000
    except (IndexError, ValueError):
        max_mcap = MAX_MCAP
    await query.answer(f"扫市值<{max_mcap/10_000:.0f}万…")
    await safe_edit(query, f"💎 正在扫市值 < ${max_mcap/10_000:.0f}万 的币…",
                    parse_mode="Markdown")

    async def send(text):
        await safe_edit(query, text, reply_markup=kb(max_mcap), parse_mode="Markdown")

    await _scan_to(send, max_mcap)
