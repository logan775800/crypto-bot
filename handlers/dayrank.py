"""多日涨跌榜 —— N 日累计涨跌幅排行（币安 / Bybit / OKX × 永续 + 现货）。

**为什么要单独做**：现有的榜全是 24h（`/top`、四个交易所专区、合约异动面板），
而 `/upstreak` 是"连续 N 天同向"——那是另一件事。一个币可以 3 天累计涨 30%
却只有 2 根阳线（中间回调一天），在连涨榜里它根本不存在。

**口径**（卡片上会写出来，因为不写就没人知道是哪个口径）：

    N 日涨跌幅 = (现价 − N 根日线之前那根的收盘价) ÷ 那个收盘价

终点用**现价**（也就是今天那根未收盘 K 线的最新价），不是昨天的收盘价——
否则今天的行情完全不体现，读出来是一笔隔夜的旧账。

⚠️ 这和 v1.33.1 那条「量比必须用已收盘那根」**不冲突**，别顺手"统一"了：
未收盘 K 线的**成交量**只累积了一部分（所以拿它算量比会系统性偏低），
但它的**收盘价就是现价**，恰恰是这里要的终点。

**先去重再拉日线**（v1.37.0 改的，这是这个模块最重要的结构）：
六个盘子（3 家 × 永续/现货）加起来两千多个交易对，同一个币在里面出现四五次。
先用 6 个便宜的 ticker 请求把「有哪些币 + 成交额」拿全、按底层资产去重、
只留成交额最大的那个盘子，**再**只对去重后的前 MAX_SCAN 个拉日线。
反过来做（先拉日线再去重）等于把四分之三的请求花在重复的币上。

**一次扫描答所有窗口**：日线一次拉 MAX_WIN+1 根，3/7/14 日都从同一批数据算。
换窗口的按钮读缓存不重扫——重扫十几秒，而那十几秒里他只是想换个窗口看看。
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

log = logging.getLogger(__name__)

OKX = "https://www.okx.com"
BYBIT = "https://api.bybit.com"
BN_FAPI = "https://fapi.binance.com"      # 币安 USDT 本位合约
BN_SPOT = "https://api.binance.com"

MAX_WIN = 14              # 最长窗口。日线拉 MAX_WIN+1 根就够算所有窗口
DEFAULT_WIN = 3
WIN_BUTTONS = (3, 7, 14)
MIN_TURNOVER_M = 5        # 百万美元：滤掉僵尸盘，否则榜首全是没人交易的币
MAX_SCAN = 300            # 去重之后按成交额取前 N 个拉日线
# 🔥 热榜：只在成交额前 N 名的币里排。
# 群里的原话是「最好只看热榜的」——全量榜一屏全是没听过的代号（AGI、USELESS…），
# 涨得再多也不知道该不该碰。**这是个纯显示过滤**，用的还是那批缓存数据，
# 点一下就切，不重扫。
# 50 是实测出来的下限：前 30 名（门槛 1 亿）时**跌幅榜整个是空的**，
# 一张没有跌幅榜的涨跌榜没有意义；前 50（门槛 4800 万）才有 5 个。
HOT_N = 50
CONCURRENCY = 12
TOP_SHOW = 8             # 每组显示几个。卡片一长，底下的按钮就被挤出屏幕
CACHE_TTL = 300           # 缓存 5 分钟：换窗口/换所/换市场的按钮走这里，不重扫

VENUES = ("bybit", "binance", "okx")
MARKETS = ("perp", "spot")
V_LABEL = {"all": "三家", "bybit": "Bybit", "binance": "币安", "okx": "OKX"}
M_LABEL = {"all": "永续+现货", "perp": "永续", "spot": "现货"}

# 合约面值前缀：1000PEPE / 10000SATS / 1MBABYDOGE 都是"底层资产 × 倍数"
_DENOM = re.compile(r"^(?:1000000|100000|10000|1000|1M)([A-Z][A-Z0-9]*)$")
# 杠杆代币 BTC3L / ETH3S：跟着标的走还带损耗，进榜没有意义
_LEV_TOKEN = re.compile(r"\d+[LS]$")
# 稳定币之间的兑换对：涨跌永远贴着 0，占位置。
# 名单只是快一步，真正兜底的是 _is_peg()——按价格判，不用维护名单
_STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDP", "USDD", "USDE",
           "PYUSD", "EURI", "AEUR", "USD1", "XUSD", "USDG", "RLUSD"}
# 币安**现货**独有的代币化股票。实测 SPCXB(SpaceX) 和 BTCUSDT 的
# permissionSets 一模一样——币安现货接口里**没有任何品类字段**，
# noncrypto_bases() 那套（Bybit/币安合约/Gate 的标注）认不出这一批。
# 硬名单一定会滞后，所以 ℹ️ 卡片上明说了这一类可能有漏网。
_BSTOCK = {"SPCXB", "QQQB", "SOXSB", "MRVLB", "LITEB", "SNXXB", "KORUB",
           "NVDAB", "TSLAB", "AAPLB", "METAB", "MSTRB", "COINB", "AMZNB",
           "GOOGLB", "HOODB", "CRCLB", "PLTRB", "AMDB", "IBITB"}

_cache = {}               # (venue, market) -> {"ts","rows","stats"}


# ── 便宜的一层：有哪些币、成交额多少（每个盘子一个请求）─────────
def _keep(base):
    return bool(base) and base not in _STABLE and not _LEV_TOKEN.search(base)


async def _uni_bybit(client, market):
    cat = "linear" if market == "perp" else "spot"
    r = await client.get(f"{BYBIT}/v5/market/tickers", params={"category": cat})
    d = r.json()
    if d.get("retCode") != 0:
        return []
    out = []
    for t in d.get("result", {}).get("list", []):
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        try:
            out.append((s, s[:-4], float(t.get("turnover24h") or 0)))
        except (TypeError, ValueError):
            continue
    return out


async def _uni_binance(client, market):
    host = BN_FAPI if market == "perp" else BN_SPOT
    path = "/fapi/v1/ticker/24hr" if market == "perp" else "/api/v3/ticker/24hr"
    r = await client.get(host + path)
    out = []
    for t in r.json() or []:
        s = t.get("symbol", "")
        # BTCUSDT_240329 这类交割合约不是永续，跳过
        if not s.endswith("USDT") or "_" in s:
            continue
        try:
            out.append((s, s[:-4], float(t.get("quoteVolume") or 0)))
        except (TypeError, ValueError):
            continue
    return out


async def _uni_okx(client, market):
    itype = "SWAP" if market == "perp" else "SPOT"
    r = await client.get(f"{OKX}/api/v5/market/tickers", params={"instType": itype})
    d = r.json()
    if d.get("code") != "0":
        return []
    out = []
    for t in d.get("data", []):
        iid = t.get("instId", "")
        try:
            last = float(t.get("last") or 0)
            if market == "perp":
                if not iid.endswith("-USDT-SWAP"):
                    continue
                base = iid[:-len("-USDT-SWAP")]
                # 永续的 volCcy24h 是**币的数量**，要乘价格才是成交额；
                # 现货的 volCcy24h 已经是计价币金额，再乘一次就翻了几万倍
                turnover = float(t.get("volCcy24h") or 0) * last
            else:
                if not iid.endswith("-USDT"):
                    continue
                base = iid[:-len("-USDT")]
                turnover = float(t.get("volCcy24h") or 0)
            out.append((iid, base, turnover))
        except (TypeError, ValueError):
            continue
    return out


_UNI = {"bybit": _uni_bybit, "binance": _uni_binance, "okx": _uni_okx}


# ── 贵的一层：日线（只对去重后的候选拉）─────────────────────
async def _kl_bybit(client, market, inst):
    cat = "linear" if market == "perp" else "spot"
    r = await client.get(f"{BYBIT}/v5/market/kline", params={
        "category": cat, "symbol": inst, "interval": "D", "limit": str(MAX_WIN + 1)})
    d = r.json()
    if d.get("retCode") != 0:
        return None
    return [float(c[4]) for c in d.get("result", {}).get("list", [])]   # 新→旧


async def _kl_okx(client, market, inst):
    r = await client.get(f"{OKX}/api/v5/market/candles", params={
        "instId": inst, "bar": "1D", "limit": str(MAX_WIN + 1)})
    d = r.json()
    if d.get("code") != "0":
        return None
    return [float(c[4]) for c in d.get("data", [])]                     # 新→旧


async def _kl_binance(client, market, inst):
    host = BN_FAPI if market == "perp" else BN_SPOT
    path = "/fapi/v1/klines" if market == "perp" else "/api/v3/klines"
    r = await client.get(host + path, params={
        "symbol": inst, "interval": "1d", "limit": MAX_WIN + 1})
    rows = r.json()
    if not isinstance(rows, list):
        return None
    # ⚠️ 币安是**旧→新**，和 Bybit/OKX 正好相反。不反转的话算出来的是
    # "N 天前 vs 更早"，符号还经常对，肉眼根本看不出错
    return [float(c[4]) for c in reversed(rows)]


_KL = {"bybit": _kl_bybit, "binance": _kl_binance, "okx": _kl_okx}


def norm_base(base):
    """把合约面值前缀归一到底层资产，只用于去重。

    `1000PEPE` 和 `PEPE` 是**同一个币**，只是合约面值不同（1000 倍报价）。
    不归一的话它们在榜上并排出现两次、涨幅几乎一样，白占两个名额。

    只认纯面值前缀（1000/10000/1M…）。别顺手扩成"名字像就合并"：
    PUMP 和 PUMPFUN 看着像一对，合错了就是把两个币的行情算到一起，
    微市值那边的同名误判就是这么来的——宁可少合一个，不能合错一个。
    """
    m = _DENOM.match(base)
    return m.group(1) if m else base


async def _fetch_one(client, sems, cand):
    """拉一个币的日线，失败退避重试一次。

    ⚠️ 这里原来是 `except Exception: return None` 一把兜住——OKX 单独扫时
    94 个候选只回来 40 个，**54 个被静默丢掉**，卡片上还若无其事地排了个榜。
    限流是常态不是异常，必须退避重试，而且丢了多少要报出来。
    """
    sem = sems[cand["venue"]]
    for attempt in (0, 1):
        async with sem:
            try:
                closes = await _KL[cand["venue"]](client, cand["market"], cand["inst"])
                if closes and len(closes) >= 2:
                    return {**cand, "closes": closes}
            except Exception as e:
                if attempt:
                    log.debug(f"日线取失败 {cand['venue']}/{cand['inst']}: {e}")
        if not attempt:
            await asyncio.sleep(0.5)
    return None


def _is_peg(closes):
    """整段都钉在 1 美元附近 = 稳定币。

    硬编码名单永远追不上新发的稳定币（这一版就漏了 USDG / RLUSD / U，
    它们以 -0.1% 的成绩占了跌幅榜三个位置）。用价格判就不用维护名单了：
    连续十几天贴着 1 元不动的，不会是别的东西。
    """
    return bool(closes) and all(0.995 <= c <= 1.005 for c in closes)


async def scan(venue="all", market="all"):
    """扫一遍，返回 (rows, stats)。stats 里每个数都会印在卡片上。

    **剔了什么必须报出来**：一个上周才上的暴涨币在 7 日榜里凭空消失，
    看榜的人不会知道是它没资格，只会以为这张榜漏了。
    """
    venues = VENUES if venue == "all" else (venue,)
    markets = MARKETS if market == "all" else (market,)
    min_turnover = MIN_TURNOVER_M * 1_000_000

    # 代币化美股：OKX 的**合约接口没有品类字段**，而它上了一批代币化美股，
    # 成交额还排得很前（SNDK 就是那个模块 docstring 里点名的例子）。
    # 判据是现成的，直接用。
    try:
        from handlers.klines import noncrypto_bases
        skip = await noncrypto_bases()
    except Exception as e:
        log.warning(f"取非加密品类失败，本轮不剔代币化美股: {e}")
        skip = set()

    async with httpx.AsyncClient(timeout=20) as client:
        # ① 六个便宜请求，拿全"有哪些币 + 成交额"
        jobs = [(v, m) for v in venues for m in markets]
        unis = await asyncio.gather(
            *[_UNI[v](client, m) for v, m in jobs], return_exceptions=True)

        cands, raw, thin, stock, dead = {}, 0, 0, 0, []
        for (v, m), res in zip(jobs, unis):
            if isinstance(res, Exception) or res is None:
                dead.append(f"{V_LABEL[v]}{M_LABEL[m]}")
                log.warning(f"取 {v}/{m} 交易对失败: {res}")
                continue
            for inst, base, turnover in res:
                raw += 1
                if not _keep(base):
                    continue
                if turnover < min_turnover:
                    thin += 1
                    continue
                key = norm_base(base)
                if base in skip or key in skip or base in _BSTOCK:
                    stock += 1
                    continue
                cur = cands.get(key)
                # 同一个币在多个盘子都有时留**成交额最大**的那个：
                # 那是流动性最好、最有代表性的报价。别学连涨榜"留涨得最多的
                # 那家"——那会系统性地把榜往极端值上拉
                if cur is None or turnover > cur["turnover"]:
                    cands[key] = {"sym": base, "key": key, "venue": v,
                                  "market": m, "inst": inst, "turnover": turnover}
        unique = len(cands)

        # ② 只对去重后的前 N 个拉日线。
        # 每家一个独立的闸：OKX 的 K 线接口比另外两家紧得多，
        # 用一个全局信号量的话，OKX 被限流会连累不相干的请求排队
        top = sorted(cands.values(), key=lambda c: -c["turnover"])[:MAX_SCAN]
        sems = {"okx": asyncio.Semaphore(6), "bybit": asyncio.Semaphore(10),
                "binance": asyncio.Semaphore(16)}
        results = await asyncio.gather(*[_fetch_one(client, sems, c) for c in top])

    rows = [r for r in results if r]
    peg = [r for r in rows if _is_peg(r["closes"])]
    rows = [r for r in rows if r not in peg]
    short = sum(1 for r in rows if len(r["closes"]) < MAX_WIN + 1)
    return rows, {
        "raw": raw, "unique": unique, "fetched": len(top), "ok": len(rows),
        "failed": len(top) - len(results) + sum(1 for r in results if not r),
        "thin": thin, "stock": stock, "short": short, "peg": len(peg),
        "skip_ok": bool(skip), "dead": dead,
        "venues": len(venues), "markets": len(markets),
    }


async def cached_scan(venue="all", market="all", force=False):
    k = (venue, market)
    c = _cache.get(k)
    if not force and c and time.time() - c["ts"] < CACHE_TTL:
        return c["rows"], c["stats"], int(time.time() - c["ts"])
    rows, stats = await scan(venue, market)
    _cache[k] = {"ts": time.time(), "rows": rows, "stats": stats}
    return rows, stats, 0


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


def ranked(rows, win, hot=False):
    """→ (涨幅榜, 跌幅榜, 统计)。**按正负切开**，不是各取头尾。

    一开始写成"前 N + 后 N"，币少的时候同一个币会同时出现在涨幅榜和跌幅榜里
    （-30% 的币堂而皇之列在涨幅榜第三名）。按正负切才是这张榜真正的定义。

    hot=True 只在成交额前 HOT_N 名里排。**在这里过滤而不是在扫描里**，
    是因为热榜和全量用的是同一批数据——切换只该是一次重排，不该是一次重扫。
    """
    pool = sorted(rows, key=lambda r: -r.get("turnover", 0))[:HOT_N] if hot else rows
    scored = []
    for r in pool:
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
V_TAG = {"bybit": "By", "binance": "BN", "okx": "OK"}


def _block(items, show_src):
    out = []
    for p, r in items:
        tag = f"{V_TAG[r['venue']]}{'现' if r['market'] == 'spot' else '永'} " if show_src else ""
        out.append(f"{tag}{r['sym']:<10}{p:+7.1f}%  ${r['closes'][0]:,.6g}")
    return "```\n" + "\n".join(out) + "\n```"


def _one(item):
    p, r = item
    return f"{r['sym']} {p:+.1f}%"


def build_text(rows, win, venue, market, stats, age=0, hot=True):
    """短卡片。**长度本身是个功能**：v1.36.0 那版一屏 27 行，
    底下的按钮被挤出屏幕，他直接问「有做功能按钮吗」——
    Telegram 的按钮永远在消息末尾，消息太长就等于没有按钮。
    详细口径收进【ℹ️ 口径】按钮。"""
    stats = stats or {}
    up, down, st = ranked(rows, win, hot)
    show_src = venue == "all" or market == "all"
    # 每一行都要挣它的位置：分隔线好看，但它换来的是按钮往下沉一行
    scope = f"🔥热榜前{HOT_N}" if hot else "全部"
    lines = [f"📅 *{win} 日涨跌榜* · {scope} · {V_LABEL[venue]}{M_LABEL[market]}"]

    if not st["n"]:
        lines.append(f"没有一个币凑得齐 {win} 根日线（扫了 {stats.get('ok', 0)} 个）。"
                     f"换个短一点的窗口试试。")
        return "\n".join(lines)

    # 空的分组也要印出来：整段消失读起来像"根本没扫这一边"，
    # 而"这 3 天一个上涨的都没有"本身就是最重要的那条信息
    if up:
        lines.append(f"🚀 *涨幅榜*（{st['n_up']}）")
        lines.append(_block(up, show_src))
    else:
        lines.append(f"🚀 *涨幅榜*（0）这 {win} 天没有一个币是涨的")
        if st["least_bad"]:
            lines.append(f"　跌得最少：{_one(st['least_bad'])}")
    if down:
        lines.append(f"📉 *跌幅榜*（{st['n_down']}）")
        lines.append(_block(down, show_src))
    else:
        lines.append(f"📉 *跌幅榜*（0）这 {win} 天没有一个币是跌的")
        if st["least_good"]:
            lines.append(f"　涨得最少：{_one(st['least_good'])}")

    # 一行说清覆盖范围（不写的话这张榜看起来像"全市场就这些"），细账收按钮
    pool_txt = (f"成交额前 {HOT_N} 名里排的（共 {len(rows)} 个币，点【全部】看全量）"
                if hot else f"{len(rows)} 个币全排")
    lines.append(f"{V_LABEL[venue]}{M_LABEL[market]}·{pool_txt}｜每组前 "
                 f"{TOP_SHOW}｜口径点 ℹ️　👇 可换窗口/交易所/现货")
    if stats.get("dead"):
        lines.append(f"⚠️ 取不到：{'、'.join(stats['dead'])}（这部分没进榜）")
    # 丢了数据必须说。不说的话这张榜看起来一样完整，而它少了一截
    fail = stats.get("failed", 0)
    if fail and stats.get("fetched"):
        lines.append(f"⚠️ {fail} 个币的日线没取到（多半是限流），这轮没进榜")
    if age:
        lines.append(f"（{age} 秒前扫的，点 🔄 重扫）")
    return "\n".join(lines)


def build_detail(rows, win, venue, market, stats):
    """【ℹ️ 口径】按钮的内容：口径、覆盖、剔了什么、为什么。"""
    stats = stats or {}
    _u, _d, st = ranked(rows, win)
    lines = [
        f"ℹ️ *{win} 日涨跌榜 · 口径与覆盖*", "━━━━━━━━━━━━━━",
        f"*怎么算的*",
        f"{win} 日涨跌幅 = (现价 − {win} 天前的日线收盘) ÷ 那个收盘价",
        f"终点用**现价**不是昨收，否则今天的行情完全不体现。"
        f"日线按 UTC 0 点切。",
        "",
        f"*扫了什么*",
        f"{V_LABEL[venue]} × {M_LABEL[market]}"
        f"（{stats.get('venues', 0)} 家 × {stats.get('markets', 0)} 个市场）",
        f"原始交易对 {stats.get('raw', 0)} 个 → 去重后 {stats.get('unique', 0)} 个币"
        f" → 按成交额取前 {stats.get('fetched', 0)} 个拉日线"
        f" → {stats.get('ok', 0)} 个拿到数据、{st['n']} 个够 {win} 天",
        "",
        f"*剔掉了什么*",
        f"· 成交额 < {MIN_TURNOVER_M * 100:g}万：{stats.get('thin', 0)} 个"
        f"（没人交易的盘子，涨跌幅没有意义）",
        f"· 代币化美股：{stats.get('stock', 0)} 个"
        + ("" if stats.get("skip_ok", True) else "　⚠️ 本轮品类表没取到，可能有漏网"),
        f"　⚠️ **只在币安现货上市**的那批（SPCXB 这类）可能漏网——"
        f"币安现货接口里没有任何品类字段，认不出来，只能靠名单挡",
        f"· 稳定币：{stats.get('peg', 0)} 个（整段贴着 1 美元的按价格判掉，不靠名单）",
        f"· 杠杆代币（BTC3L 这类）：不计数，直接不看",
        f"· 日线没取到：{stats.get('failed', 0)} 个（限流，已退避重试过一次）",
        f"· 同一个币在多个盘子出现时只留成交额最大的那个；"
        f"`1000PEPE` 和 `PEPE` 算同一个币",
        f"· {stats.get('short', 0)} 个币日线不够 {MAX_WIN} 根（新上市）——"
        f"不是被剔，是窗口越长它们越算不出来",
    ]
    if stats.get("dead"):
        lines.append(f"· ⚠️ 这一轮取不到：{'、'.join(stats['dead'])}")
    lines.append("")
    lines.append("⚠️ 涨得多≠还会涨，不构成投资建议")
    return "\n".join(lines)


def _h(hot):
    return "hot" if hot else "full"


def kb(win, venue, market, hot=True):
    def cb(w=None, v=None, m=None, h=None):
        return (f"dr:w:{w or win}:{v or venue}:{m or market}:"
                f"{_h(hot if h is None else h)}")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if w == win else ''}{w}日", callback_data=cb(w=w))
         for w in WIN_BUTTONS],
        [InlineKeyboardButton(f"{'✅' if hot else ''}🔥 热榜前{HOT_N}",
                              callback_data=cb(h=True)),
         InlineKeyboardButton(f"{'✅' if not hot else ''}全部币",
                              callback_data=cb(h=False))],
        [InlineKeyboardButton(f"{'✅' if v == venue else ''}{V_LABEL[v]}",
                              callback_data=cb(v=v))
         for v in ("all", "bybit", "binance", "okx")],
        [InlineKeyboardButton(f"{'✅' if m == market else ''}{M_LABEL[m]}",
                              callback_data=cb(m=m))
         for m in ("all", "perp", "spot")],
        [InlineKeyboardButton(
            "ℹ️ 口径", callback_data=f"dr:i:{win}:{venue}:{market}:{_h(hot)}"),
         InlineKeyboardButton(
            "🔄 重扫", callback_data=f"dr:r:{win}:{venue}:{market}:{_h(hot)}"),
         InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")],
    ])


# ── 入口 ────────────────────────────────────────────────────
def parse_args(args):
    # 默认只看热榜——群里的原话是「最好只看热榜的」。要全量点【全部币】或加「全部」
    win, venue, market, hot = DEFAULT_WIN, "all", "all", True
    for a in args or []:
        al = str(a).lower()
        if al in ("全部", "全量", "all币", "full"):
            hot = False
            continue
        if al in ("热榜", "热门", "hot"):
            hot = True
            continue
        if al in VENUES or al == "all":
            venue = al
            continue
        if al in ("币安", "binance", "bn"):
            venue = "binance"
            continue
        if al in ("现货", "spot"):
            market = "spot"
            continue
        if al in ("永续", "合约", "perp", "swap"):
            market = "perp"
            continue
        # 「3日」「7天」这么打也认——他就是这么说话的
        try:
            win = int(al.rstrip("日天dD"))
        except ValueError:
            pass
    return max(1, min(win, MAX_WIN)), venue, market, hot


async def rank_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rank [天数] [bybit|binance|okx] [现货|永续] —— N 日累计涨跌幅排行。"""
    win, venue, market, hot = parse_args(context.args)
    uid = update.effective_user.id
    c = _cache.get((venue, market))
    if c and time.time() - c["ts"] < CACHE_TTL:
        rows, stats, age = await cached_scan(venue, market)
        await safe_reply(update.message, build_text(rows, win, venue, market, stats, age, hot),
                         reply_markup=kb(win, venue, market, hot), parse_mode="Markdown")
        return
    async with busy.guard(uid, "dayrank") as ok:
        if not ok:
            await safe_reply(update.message, busy.busy_text(uid, "dayrank", "涨跌榜扫描"))
            return
        await safe_reply(update.message,
            f"📅 扫 {V_LABEL[venue]}{M_LABEL[market]} 的 {win} 日涨跌幅…"
            f"（要逐个拉日线，十几到几十秒，币多的时候更久；这期间其他功能照常能用）")
        try:
            rows, stats, age = await cached_scan(venue, market)
        except Exception as e:
            log.error(f"多日涨跌榜扫描出错: {e}")
            await safe_reply(update.message, f"扫描失败，稍后再试：{str(e)[:80]}")
            return
    await safe_reply(update.message, build_text(rows, win, venue, market, stats, age, hot),
                     reply_markup=kb(win, venue, market, hot), parse_mode="Markdown")


async def from_btn(query, context, win, venue, market, hot=True, force=False, detail=False):
    """按钮：换窗口/换所/换市场读缓存，重扫和看口径各走各的。"""
    uid = query.from_user.id
    c = _cache.get((venue, market))
    fresh = c and time.time() - c["ts"] < CACHE_TTL
    if detail and fresh:
        rows, stats, _age = await cached_scan(venue, market)
        await safe_edit(query, build_detail(rows, win, venue, market, stats),
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            "⬅️ 回榜单", callback_data=f"dr:w:{win}:{venue}:{market}:{_h(hot)}")]]),
                        parse_mode="Markdown")
        return
    if force or not fresh:
        async with busy.guard(uid, "dayrank") as ok:
            if not ok:
                await query.answer(
                    f"上一次扫描还在跑（已 {busy.elapsed(uid, 'dayrank')} 秒）",
                    show_alert=True)
                return
            await safe_edit(query, f"📅 扫 {V_LABEL[venue]}{M_LABEL[market]}…（十几到几十秒）")
            try:
                rows, stats, age = await cached_scan(venue, market, force=force)
            except Exception as e:
                log.error(f"多日涨跌榜按钮扫描出错: {e}")
                await safe_edit(query, f"扫描失败：{str(e)[:80]}",
                                reply_markup=kb(win, venue, market, hot))
                return
    else:
        rows, stats, age = await cached_scan(venue, market)
    body = (build_detail(rows, win, venue, market, stats) if detail
            else build_text(rows, win, venue, market, stats, age, hot))
    mk = (InlineKeyboardMarkup([[InlineKeyboardButton(
        "⬅️ 回榜单", callback_data=f"dr:w:{win}:{venue}:{market}:{_h(hot)}")]]) if detail
        else kb(win, venue, market, hot))
    await safe_edit(query, body, reply_markup=mk, parse_mode="Markdown")
