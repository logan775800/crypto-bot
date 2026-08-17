"""链上代币查询：没上交易所、只在 DEX 交易的那些币（币安钱包/Alpha 区那类）。

四家交易所的现货合约都能查了，缺的是**链上**——新币先在链上跑几天甚至几周，
等上了交易所往往已经涨完。这个模块补的就是那一段。

⚠️ 这块和交易所行情有一个本质区别，整个模块的设计都围着它转：
**链上没有上币审核，同名假币是常态。** 实测搜 "PEPE" 返回 30 个交易对、跨 6 条链，
按接口默认顺序排第一的是流动性 2.4 万美元的「Pepe in Hood」，而真 PEPE 的池子有
2048 万——差 800 倍。所以：
  • 名字搜索一律**按流动性排序**，并明说"同名还有 N 个，认准合约地址"；
  • 合约地址查询才是精确的，鼓励用户用地址；
  • 每个结果都带风险标注，池子浅/新池/刷量/市值虚高都要写在脸上。

数据源（都免费、无需 key）：
  • DexScreener  按名字/合约地址查，覆盖最全
  • GeckoTerminal 各链**真实**热门池
⚠️ DexScreener 还有个 token-boosts 接口，那是**付费推广位**（返回里直接带营销文案），
不是热门榜。拿它当榜单等于给用户推广告，坚决不用。
"""
import logging
import re
import time

from handlers import source as src_mod
from handlers.util import escape_md

log = logging.getLogger(__name__)

DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"

# chainId 各家写法不同：DexScreener 用 ethereum/solana，GeckoTerminal 用 eth/solana
CHAINS = {
    "bsc":  {"ds": "bsc",       "gt": "bsc",         "cn": "BNB链"},
    "eth":  {"ds": "ethereum",  "gt": "eth",         "cn": "以太坊"},
    "sol":  {"ds": "solana",    "gt": "solana",      "cn": "Solana"},
    "base": {"ds": "base",      "gt": "base",        "cn": "Base"},
    "arb":  {"ds": "arbitrum",  "gt": "arbitrum",    "cn": "Arbitrum"},
    "tron": {"ds": "tron",      "gt": "tron",        "cn": "波场"},
}
DS2KEY = {v["ds"]: k for k, v in CHAINS.items()}
DEFAULT_CHAIN = "bsc"          # 币安钱包的主战场

# 链名一堆写法。认不出必须**明确拒绝**，不能悄悄回落到默认链——
# 「/onchain 热门 solana」给你一份 BNB 链的榜，比报错难查得多。
CHAIN_ALIASES = {
    "bsc": "bsc", "bnb": "bsc", "binance": "bsc", "币安": "bsc", "币安链": "bsc",
    "eth": "eth", "ethereum": "eth", "erc20": "eth", "以太": "eth", "以太坊": "eth",
    "sol": "sol", "solana": "sol", "索拉纳": "sol",
    "base": "base",
    "arb": "arb", "arbitrum": "arb",
    "tron": "tron", "trx": "tron", "波场": "tron",
}


def resolve_chain(s):
    """链名 → 内部 key；认不出返回 None（由调用方如实告诉用户支持哪些）。"""
    return CHAIN_ALIASES.get((s or "").strip().lower())


# 风险门槛（链上币的坑基本都能被这几条抓住）
LIQ_DANGER = 50_000            # 池子低于这个数，几千美元的单子就能把价格打飞
LIQ_THIN = 200_000
NEW_POOL_HOURS = 24
FDV_LIQ_CRAZY = 200            # 市值/流动性，超过这个数说明盘子是空的
VOL_LIQ_WASH = 30              # 24h量/流动性，异常高多半在刷量

PUMP_WARN = 300.0              # 24h 涨这么多的链上币，几乎都是"给别人出货用的流动性"

# 死池/假池：**标称流动性是可以造的**（拿个垃圾币和自己配对就行），但成交量造不了。
# 实测搜 AKE，排第一的池子标称 2.2 亿美元，24h 只成交 4 美元、2 笔——
# 纯按流动性排序会把这种假池推到第一位，比不做排序还糟。
DEAD_VOL_RATIO = 0.001         # 24h量/流动性 低于此 = 这个池子没有人在交易
DEAD_MIN_LIQ = 100_000         # 小池子成交少很正常，这条只用来抓"大而空"的
DEAD_MAX_TX = 20
# 排序时给流动性打的折：要拿到全额信用，24h 成交额至少得有池子的 1/50
LIQ_CREDIT = 50


def effective_liq(t):
    """排序用的"可信流动性"：标称值和成交量互相印证后取小的那个。

    只按标称流动性排，假池必然霸榜；只按成交量排，又会把刚建的真池埋掉。
    取 min(标称, 成交额×50) 是让两者互相背书——造得出数字，造不出交易。
    """
    liq = t.get("liq") or 0.0
    vol = t.get("vol24") or 0.0
    return min(liq, max(vol, 1.0) * LIQ_CREDIT)


def is_dead_pool(t):
    """大而空：标称流动性很大，却几乎没有成交。"""
    liq = t.get("liq") or 0.0
    vol = t.get("vol24") or 0.0
    tx = (t.get("buys") or 0) + (t.get("sells") or 0)
    return liq >= DEAD_MIN_LIQ and vol < liq * DEAD_VOL_RATIO and tx < DEAD_MAX_TX


TOP_N = 5
TREND_N = 8
_CACHE_TTL = 60                # 榜单缓存，GeckoTerminal 免费额度 30 次/分钟
_cache = {}


def flag(liq, chg=0.0):
    """一行前面的标记。**深度和涨幅要分开看**：

    第一版只看池子深度，于是 24h +4750% 的 MarsCoin 因为池子有 39 万被标成 ✅——
    绿标在这种币上等于背书。池子够深不代表这个价能拿，暴涨本身就是最大的风险。
    """
    if liq < LIQ_DANGER:
        return "⛔"
    if chg >= PUMP_WARN:
        return "🚀"
    if liq < LIQ_THIN:
        return "⚠️"
    return "✅"


def flag_of(t):
    """按整条记录判标记——死池要先于深度判断，它标称的深度本来就是假的。"""
    if is_dead_pool(t):
        return "💀"
    return flag(t.get("liq") or 0.0, t.get("chg24") or 0.0)


EVM_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOL_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def is_address(text):
    """看起来像合约地址吗（EVM 0x… 或 Solana base58）。"""
    s = (text or "").strip()
    return bool(EVM_ADDR.match(s) or SOL_ADDR.match(s))


async def _get(url, params=None):
    r = await src_mod.client().get(url, params=params or {},
                                   headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def _f(d, *path, default=0.0):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    try:
        return float(cur)
    except (TypeError, ValueError):
        return default


def _pair(p):
    """DexScreener 的一个交易对 → 归一后的字典。"""
    base = p.get("baseToken") or {}
    created = p.get("pairCreatedAt")
    return {
        "chain": p.get("chainId") or "",
        "chain_cn": CHAINS.get(DS2KEY.get(p.get("chainId"), ""), {}).get(
            "cn", p.get("chainId") or "?"),
        "dex": p.get("dexId") or "",
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or "",
        "address": base.get("address") or "",
        "price": _f(p, "priceUsd"),
        "liq": _f(p, "liquidity", "usd"),
        "vol24": _f(p, "volume", "h24"),
        "chg24": _f(p, "priceChange", "h24"),
        "chg1h": _f(p, "priceChange", "h1"),
        "fdv": _f(p, "fdv"),
        "buys": _f(p, "txns", "h24", "buys"),
        "sells": _f(p, "txns", "h24", "sells"),
        "created_ms": int(created) if created else 0,
        "url": p.get("url") or "",
        # 画 K 线要的是**池子地址**（不是代币地址）——GeckoTerminal 的 OHLCV 按池子给
        "pool": p.get("pairAddress") or "",
        "chain_key": DS2KEY.get(p.get("chainId"), ""),
    }


def risks(t):
    """一个代币的风险标注。链上币的钱基本都亏在这几条上，所以写在脸上而不是脚注。"""
    out = []
    if is_dead_pool(t):
        tx = t["buys"] + t["sells"]
        out.append(f"💀 池子标称 ${t['liq']:,.0f}，但 24h 只成交 ${t['vol24']:,.0f}"
                   f"（{tx:.0f} 笔）——标称流动性可以用垃圾币配对造出来，"
                   f"成交量造不了。这个数字不可信")
    if t["liq"] < LIQ_DANGER:
        out.append(f"⛔ 池子只有 ${t['liq']:,.0f}——几千美元的单就能把价格打飞，"
                   f"进得去出不来")
    elif t["liq"] < LIQ_THIN:
        out.append(f"⚠️ 池子偏浅 ${t['liq']:,.0f}，滑点会很难看")
    if t["created_ms"]:
        age_h = (time.time() * 1000 - t["created_ms"]) / 3_600_000
        if age_h < NEW_POOL_HOURS:
            out.append(f"🆕 池子建了才 {age_h:.0f} 小时，新池风险最高")
    if t["liq"] > 0 and t["fdv"] / t["liq"] > FDV_LIQ_CRAZY:
        out.append(f"⚠️ 市值 ${t['fdv']:,.0f} 是池子的 {t['fdv']/t['liq']:.0f} 倍，"
                   f"盘子是空的，砸下来没有承接")
    if t["chg24"] >= PUMP_WARN:
        out.append(f"🚀 24h 已涨 {t['chg24']:+.0f}%——链上这种涨幅通常意味着"
                   f"你会是接盘的那一边")
    if t["liq"] > 0 and t["vol24"] / t["liq"] > VOL_LIQ_WASH:
        out.append(f"⚠️ 24h成交额是池子的 {t['vol24']/t['liq']:.0f} 倍，"
                   f"这种量价关系多半在刷量")
    tx = t["buys"] + t["sells"]
    if tx >= 50 and t["sells"] > 0 and t["buys"] / max(t["sells"], 1) > 5:
        out.append(f"⚠️ 买 {t['buys']:.0f} 笔 / 卖 {t['sells']:.0f} 笔，"
                   f"卖出异常少——留意能不能卖出去")
    return out


def fmt_price(p):
    if p <= 0:
        return "-"
    if p >= 1:
        return f"{p:,.4f}".rstrip("0").rstrip(".")
    s = f"{p:.12f}".rstrip("0")
    return s if len(s) <= 14 else f"{p:.4g}"


# ---------- 查询 ----------
def token_id(t):
    """代币的唯一身份：**链 + 合约地址 + 池地址**，三者缺一不可。

    只用名字会撞上同名假币（搜 PEPE 出 30 个）；只用合约地址还不够——
    同一个代币在同一条链上有多个池子，价格和深度都不一样（实测「牛来」12 个池），
    报价说的是哪个池、监控盯的是哪个池，必须能说清楚。
    """
    return f"{t.get('chain_key') or t.get('chain')}:{t.get('address')}:{t.get('pool')}"


def price_spread(pools, min_liq=LIQ_DANGER):
    """多池价格偏差 → (偏差%, 最低价, 最高价, 参与比较的池子数)。

    偏差大意味着某个池子正在被拉或被砸，此刻的"价格"取决于你用哪个池成交——
    不说清楚就等于给了一个不存在的价。
    """
    ps = [p for p in pools if (p.get("liq") or 0) >= min_liq and (p.get("price") or 0) > 0]
    if len(ps) < 2:
        return 0.0, 0.0, 0.0, len(ps)
    lo = min(p["price"] for p in ps)
    hi = max(p["price"] for p in ps)
    return ((hi - lo) / lo * 100 if lo else 0.0), lo, hi, len(ps)


async def by_address(addr, chain=None):
    """按合约地址查 → (代币, 该代币的池子总数)。这是**精确**的查法。

    返回的代币里带上 pools（各池的价/深度）和 fetched_at（取数时刻），
    调用方要展示价格来源和多池偏差都靠它。chain 传了就只认那条链上的池子——
    同一个地址在多条链上可能都有（桥过去的），不指定就用可信流动性最大的。
    """
    d = await _get(f"{DS}/latest/dex/tokens/{addr.strip()}")
    pairs = d.get("pairs") or []
    norm = [_pair(x) for x in pairs]
    if chain:
        norm = [p for p in norm if p.get("chain_key") == chain] or norm
    if not norm:
        return None, 0
    # 用可信流动性挑主池：同一个代币下也会有大而空的假池
    best = max(norm, key=effective_liq)
    same_chain = [p for p in norm if p["chain_key"] == best["chain_key"]]
    best["pools"] = sorted(same_chain, key=effective_liq, reverse=True)[:8]
    best["pool_count"] = len(same_chain)
    best["fetched_at"] = time.time()
    return best, len(pairs)


async def by_name(q):
    """按名字/符号搜 → (按流动性排序的列表, 命中总数)。

    排序方式是这个函数唯一重要的事：接口默认顺序会把山寨币排在真币前面，
    而纯按标称流动性排又会把「大而空」的假池顶上来（见 effective_liq）。
    """
    d = await _get(f"{DS}/latest/dex/search", {"q": q.strip()})
    pairs = d.get("pairs") or []
    if not pairs:
        return [], 0
    # 一个代币可能有多个池子，按合约地址去重，每个地址留流动性最大的那个池
    best = {}
    for p in pairs:
        t = _pair(p)
        if not t["address"]:
            continue
        cur = best.get((t["chain"], t["address"]))
        if not cur or effective_liq(t) > effective_liq(cur):
            best[(t["chain"], t["address"])] = t
    out = sorted(best.values(), key=lambda x: -effective_liq(x))
    return out, len(out)


async def trending(chain=DEFAULT_CHAIN):
    """某条链的真实热门池（GeckoTerminal）。带 60 秒缓存，免费额度 30 次/分钟。"""
    if chain not in CHAINS:
        raise ValueError(f"不认识的链 {chain}")
    key = ("trend", chain)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    net = CHAINS[chain]["gt"]
    d = await _get(f"{GT}/networks/{net}/trending_pools", {"page": 1})
    out = []
    for x in (d.get("data") or []):
        a = x.get("attributes") or {}
        out.append({
            "name": a.get("name") or "",
            "price": _f(a, "base_token_price_usd"),
            "chg24": _f(a, "price_change_percentage", "h24"),
            "liq": _f(a, "reserve_in_usd"),
            "vol24": _f(a, "volume_usd", "h24"),
            "address": ((x.get("relationships") or {}).get("base_token") or {})
            .get("data", {}).get("id", "").split("_")[-1],
        })
    _cache[key] = (time.monotonic(), out)
    return out


# ---------- 渲染 ----------
# ---------- K 线 ----------
# GeckoTerminal 的 OHLCV 按**池子**给，不是按代币；周期用 timeframe+aggregate 组合表达
TF = {"15m": ("minute", 15), "1h": ("hour", 1), "4h": ("hour", 4), "1d": ("day", 1)}


async def ohlcv(chain_key, pool, tf="1h", limit=200):
    """链上 K 线 → [[毫秒, 开, 高, 低, 收, 量], ...] 旧→新。取不到返回 []。"""
    if chain_key not in CHAINS or not pool:
        return []
    frame, agg = TF.get(tf, TF["1h"])
    net = CHAINS[chain_key]["gt"]
    try:
        d = await _get(f"{GT}/networks/{net}/pools/{pool}/ohlcv/{frame}",
                       {"aggregate": agg, "limit": min(limit, 1000)})
    except Exception as e:
        log.warning(f"链上K线失败 {chain_key}/{pool} {tf}: {e}")
        return []
    rows = ((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    out = [[int(r[0]) * 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]),
            float(r[5] or 0)] for r in rows if r and r[0]]
    out.sort(key=lambda x: x[0])          # 接口给的是新→旧，指标和画图都要旧→新
    return out


TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
WICK_BODY_RATIO = 3.0      # 影线是实体的几倍才算插针
WICK_MIN_PCT = 3.0         # 且影线本身要占价格这么多百分比，否则只是正常波动
# 还要超过**这个币自己**的影线中位数这么多倍。只用固定百分比不行：
# 实测「牛来」200 根 15m 里有 48 根（四分之一）被判成插针——标记一密就等于没标。
# 插针的定义本来就是"比这个币平时的噪音突出得多"，所以基准要用它自己的噪音。
WICK_MEDIAN_RATIO = 3.0
MAX_MARKS = 8              # 图上最多标几个，只留最极端的
VOL_SPIKE_RATIO = 4.0      # 成交量是中位数的几倍算异常


def kline_marks(rows, tf):
    """标出「这根不能当正常K线看」的地方。返回 dict。

    链上池子浅，一笔大单就能画出一根插针，而插针那个价**根本成交不到量**——
    照着影线的高低点去设止损或判突破，等于按一个不存在的价格做决策。
    最后一根还没收盘也必须说：它随时会变，用它做判断是在追一个还在动的数。
    """
    out = {"unclosed": False, "wicks": [], "vol_spikes": [], "bar_ms": TF_MS.get(tf, 0)}
    if not rows:
        return out
    bar_ms = TF_MS.get(tf, 0)
    if bar_ms:
        # 最后一根的开始时间 + 一根的长度 还没走到现在 → 它还在形成中
        out["unclosed"] = (rows[-1][0] + bar_ms) > time.time() * 1000

    vols = sorted(r[5] for r in rows if r[5] > 0)
    med_vol = vols[len(vols) // 2] if vols else 0.0
    # 这个币自己的影线噪音水平：拿它当基准，才分得清"波动大"和"插针"
    all_wicks = []
    for _ts, o, h, lo, c, _v in rows:
        all_wicks.append(h - max(o, c))
        all_wicks.append(min(o, c) - lo)
    all_wicks = sorted(w for w in all_wicks if w > 0)
    med_wick = all_wicks[len(all_wicks) // 2] if all_wicks else 0.0

    for i, (ts, o, h, lo, c, v) in enumerate(rows):
        body = abs(c - o)
        ref = c or o or 1.0
        for wick, side in ((h - max(o, c), "上"), (min(o, c) - lo, "下")):
            if wick <= 0 or ref <= 0:
                continue
            # 实体接近 0 时用价格自身做分母，避免除零把普通K线判成插针
            if (wick >= WICK_BODY_RATIO * max(body, ref * 0.001)
                    and wick / ref * 100 >= WICK_MIN_PCT
                    and (med_wick <= 0 or wick >= WICK_MEDIAN_RATIO * med_wick)):
                out["wicks"].append({"i": i, "ts": ts, "side": side,
                                     "pct": wick / ref * 100,
                                     "price": h if side == "上" else lo})
        if med_vol > 0 and v >= VOL_SPIKE_RATIO * med_vol:
            out["vol_spikes"].append({"i": i, "ts": ts, "x": v / med_vol})

    # 只留最极端的几个：标记一密就没人看了，也画得满图都是
    out["wicks"] = sorted(out["wicks"], key=lambda w: -w["pct"])[:MAX_MARKS]
    out["vol_spikes"] = sorted(out["vol_spikes"], key=lambda s: -s["x"])[:MAX_MARKS]
    return out


def marks_text(marks, tf):
    """把标记讲成人话。没有异常就返回空列表，不制造噪音。"""
    out = []
    if marks.get("unclosed"):
        out.append(f"⏳ 最后一根 {tf} K线**还没收盘**，它还会变——"
                   f"别拿一根没走完的K线做判断")
    ws = marks.get("wicks") or []
    if ws:
        worst = max(ws, key=lambda w: w["pct"])
        when = time.strftime("%m-%d %H:%M", time.localtime(worst["ts"] / 1000))
        out.append(f"📌 {len(ws)} 根插针（最长 {when} {worst['side']}影 "
                   f"{worst['pct']:.0f}%，到 ${fmt_price(worst['price'])}）——"
                   f"池子浅，一笔单就能戳出来，那个价成交不到量")
    vs = marks.get("vol_spikes") or []
    if vs:
        worst = max(vs, key=lambda v: v["x"])
        when = time.strftime("%m-%d %H:%M", time.localtime(worst["ts"] / 1000))
        out.append(f"📊 {len(vs)} 根异常放量（最大 {when} 是中位量的 "
                   f"{worst['x']:.0f} 倍）")
    return out


def _ascii_title(t, tf):
    """图表标题只能是 ASCII——镜像里没有中文字体，「牛来」会渲染成豆腐块。
    所以中文名一律退回合约地址开头，图上认得出是哪个币就行，中文说明放在图下面。"""
    sym = "".join(c for c in (t.get("symbol") or "") if c.isascii() and c.isprintable())
    sym = sym.strip() or (t.get("address") or "")[:10]
    return f"[{sym}] {tf} {t.get('chain', '')}"


async def build_chart(t, tf="1h"):
    """链上代币的 K 线图。返回 (图, 说明) 或 None。"""
    rows = await ohlcv(t.get("chain_key"), t.get("pool"), tf)
    if len(rows) < 10:
        return None
    try:
        import datetime
        import io as _io
        import pandas as pd
        import mplfinance as mpf
    except Exception as e:
        log.error(f"链上K线绘图库缺失: {e}")
        return None

    closes = [r[4] for r in rows]
    idx = [datetime.datetime.utcfromtimestamp(r[0] / 1000) for r in rows]
    data = {"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows], "Close": closes,
            "Volume": [r[5] for r in rows]}
    aps = []
    for n, color in ((20, "#2962ff"), (50, "#ff6d00")):
        if len(closes) > n:
            k = 2 / (n + 1)
            e = sum(closes[:n]) / n
            ser = [None] * (n - 1) + [e]
            for v in closes[n:]:
                e = v * k + e * (1 - k)
                ser.append(e)
            data[f"E{n}"] = ser
    df = pd.DataFrame(data, index=pd.DatetimeIndex(idx))
    for n, color in ((20, "#2962ff"), (50, "#ff6d00")):
        if f"E{n}" in df:
            aps.append(mpf.make_addplot(df[f"E{n}"], color=color, width=0.9))

    # 异常标记也画到图上：光在文字里说，看图的时候还是会把插针当成真实高低点
    marks = kline_marks(rows, tf)
    if marks["wicks"]:
        ser = [None] * len(rows)
        for w in marks["wicks"]:
            ser[w["i"]] = w["price"]
        if any(x is not None for x in ser):
            aps.append(mpf.make_addplot(pd.Series(ser, index=df.index), type="scatter",
                                        marker="x", markersize=60, color="#d500f9"))
    if marks["vol_spikes"]:
        ser = [None] * len(rows)
        for s in marks["vol_spikes"]:
            ser[s["i"]] = rows[s["i"]][2]
        if any(x is not None for x in ser):
            aps.append(mpf.make_addplot(pd.Series(ser, index=df.index), type="scatter",
                                        marker="^", markersize=40, color="#ff9100"))

    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit",
                               wick="inherit", volume="in")
    style = mpf.make_mpf_style(base_mpf_style="charles", marketcolors=mc,
                               gridstyle=":", gridcolor="#e0e0e0")
    buf = _io.BytesIO()
    try:
        mpf.plot(df, type="candle", volume=True, style=style, addplot=aps,
                 title=_ascii_title(t, tf), figsize=(11, 6.5), tight_layout=True,
                 savefig=dict(fname=buf, dpi=90, format="png"))
    except Exception as e:
        log.error(f"链上K线绘图失败: {e}")
        return None
    buf.seek(0)
    hi, lo = max(r[2] for r in rows), min(r[3] for r in rows)
    head = [f"📈 *{escape_md(t['symbol'])}*　{tf}　{t['chain_cn']}",
            f"现价 *${fmt_price(closes[-1])}*　区间 ${fmt_price(lo)}~${fmt_price(hi)}",
            f"{len(rows)} 根K线　池 ${t['liq']:,.0f}"]
    if t.get("pool"):
        head.append(f"池子 `{_short(t['pool'])}`——K线是**这一个池**的，不是全链均价")
    body = marks_text(marks, tf)
    tail = ["蓝线 EMA20／橙线 EMA50"]
    if marks["wicks"]:
        tail.append("紫 ✕ = 插针")
    if marks["vol_spikes"]:
        tail.append("橙 ▲ = 异常放量")
    cap = "\n".join(head + (["━━━━━━━━━━━━━━"] + body if body else []) +
                    ["　".join(tail),
                     "⚠️ 链上池子浅，单笔大单就能画出长影线——"
                     "K线形态的参考价值远低于交易所"])
    return buf, cap


def kline_kb(t, tf="1h"):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    row = [InlineKeyboardButton(("•" if k == tf else "") + k,
                                callback_data=f"oc:k:{t['chain_key']}:{t['pool']}:{k}")
           for k in TF]
    return InlineKeyboardMarkup([row, [
        InlineKeyboardButton("📄 代币详情",
                             callback_data=f"oc:d:{t['chain_key']}:{t['address']}")]])


# 链上的默认阈值比交易所高一档：链上池子浅，±5% 在小池子上一天能响几十次
WATCH_PCTS = (10, 20, 50)


def detail_kb(t):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    if t.get("pool") and t.get("chain_key"):
        rows.append([InlineKeyboardButton(
            f"📈 {k} K线", callback_data=f"oc:k:{t['chain_key']}:{t['pool']}:{k}")
            for k in ("1h", "4h", "1d")])
    if t.get("address"):
        # 监控入口必须在这里：查完了才知道要不要盯，让他退出去打命令等于不会用
        rows.append([InlineKeyboardButton(
            f"🔔 涨跌±{p}% 提醒", callback_data=f"oc:w:{t['address']}:{p}")
            for p in WATCH_PCTS])
    if t.get("url"):
        rows.append([InlineKeyboardButton("🌐 在 DexScreener 打开", url=t["url"])])
    return InlineKeyboardMarkup(rows) if rows else None


def render_token(t, pools=1, same_name=0):
    lines = [f"🔗 *{escape_md(t['symbol'])}*"
             + (f"　{escape_md(t['name'])}" if t["name"] else ""),
             f"{t['chain_cn']}　{escape_md(t['dex'])}",
             "━━━━━━━━━━━━━━",
             f"💵 *当前价 ${fmt_price(t['price'])}*",
             f"24h {t['chg24']:+.1f}%　1h {t['chg1h']:+.1f}%",
             f"流动性 ${t['liq']:,.0f}　24h量 ${t['vol24']:,.0f}"]
    if t["fdv"]:
        lines.append(f"市值(FDV) ${t['fdv']:,.0f}")
    if t["buys"] or t["sells"]:
        lines.append(f"24h 买 {t['buys']:.0f} 笔／卖 {t['sells']:.0f} 笔")
    if t["created_ms"]:
        age_h = (time.time() * 1000 - t["created_ms"]) / 3_600_000
        age = f"{age_h:.0f} 小时" if age_h < 48 else f"{age_h/24:.0f} 天"
        lines.append(f"池子年龄 {age}")

    # ── 价格是从哪来的：来源池 / 取数时刻 / 多池偏差 ──
    # 不说清楚就等于给了一个"不存在的价"——同一个代币不同池子的价能差出几个点，
    # 你按哪个池成交拿到的就是哪个价。
    lines.append("━━━━━━━━━━━━━━")
    src_pool = t.get("pool") or ""
    lines.append(f"📍 价格来源　{escape_md(t['dex'])}　池 `{_short(src_pool)}`")
    lines.append(f"　 该池深度 ${t['liq']:,.0f}")
    if t.get("fetched_at"):
        lines.append(f"🕐 取数时刻　{time.strftime('%H:%M:%S', time.localtime(t['fetched_at']))}"
                     f"（接口不给更新时间戳，这是我取到它的时刻）")
    pool_list = t.get("pools") or []
    dev, lo, hi, n = price_spread(pool_list)
    if n >= 2:
        tag = "⚠️ " if dev >= 3 else ""
        lines.append(f"{tag}多池偏差　{dev:.2f}%　（{n} 个≥${LIQ_DANGER//1000}k 的池子："
                     f"${fmt_price(lo)} ~ ${fmt_price(hi)}）")
        if dev >= 3:
            lines.append("　 偏差这么大说明某个池子正在被拉或被砸，"
                         "成交前先确认你走的是哪个池")
    elif t.get("pool_count", 1) > 1:
        lines.append(f"多池偏差　只有 {n} 个池子够深，其余太浅不参与比较")

    rk = risks(t)
    if rk:
        lines.append("━━━━━━━━━━━━━━")
        lines.extend(rk)
    if pools > 1:
        lines.append(f"（该代币共 {pools} 个池子，上面是可信流动性最大的那个）")
    if same_name:
        lines.append(f"⚠️ 链上有 *{same_name}* 个同名代币——链上没有上币审核，"
                     f"同名假币是常态。**认准合约地址**：")
    lines.append("━━━━━━━━━━━━━━")
    lines.append(f"🔗 身份　{t['chain_cn']}")
    lines.append(f"　 代币 `{t['address']}`")
    if src_pool:
        lines.append(f"　 池子 `{src_pool}`")
    lines.append("\n⚠️ 链上代币风险远高于交易所币，不构成投资建议")
    return "\n".join(lines)


def _short(addr):
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 14 else (addr or "?")


def render_list(items, q, total):
    lines = [f"🔗 *链上搜「{escape_md(q)}」*　命中 {total} 个",
             "按**可信流动性**排序（标称池子 × 真实成交互相印证）——"
             "接口默认顺序会把山寨和假池排在真币前面",
             "━━━━━━━━━━━━━━"]
    for i, t in enumerate(items[:TOP_N], 1):
        lines.append(f"{i}. {flag_of(t)} *{escape_md(t['symbol'])}*"
                     f"（{escape_md(t['name'][:18])}）{t['chain_cn']}")
        lines.append(f"　 现价 *${fmt_price(t['price'])}*　24h {t['chg24']:+.1f}%　"
                     f"池 ${t['liq']:,.0f}")
        lines.append(f"　 `{t['address']}`")
    if total > TOP_N:
        lines.append(f"\n还有 {total - TOP_N} 个没列——同名太多正是链上的常态，"
                     f"用合约地址查才准")
    lines.append("\n👇 点下面的按钮看详情和 K 线图")
    lines.append("⚠️ 链上代币风险远高于交易所币，不构成投资建议")
    return "\n".join(lines)


def list_kb(items):
    """搜索结果每条给一个按钮。

    只给一串文字的话，用户想看详情就得手工把合约地址复制出来再发一遍——
    中间那段手工搬运正是「看完就算了」的原因（分析结果闭环那次已经吃过一回教训）。
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    for i, t in enumerate(items[:TOP_N], 1):
        if not t.get("chain_key") or not t.get("address"):
            continue
        rows.append([InlineKeyboardButton(
            f"{i}. {t['symbol'][:8]} {t['chain_cn']}　详情+K线",
            callback_data=f"oc:d:{t['chain_key']}:{t['address']}")])
    rows.append([InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def render_trending(items, chain):
    cn = CHAINS.get(chain, {}).get("cn", chain)
    lines = [f"🔥 *{cn} 链上热门*",
             "来自 GeckoTerminal 的真实交易热度，"
             "**不是**付费推广位（那种榜单是广告）",
             "━━━━━━━━━━━━━━"]
    shown = 0
    for t in items:
        if shown >= TREND_N:
            break
        shown += 1
        lines.append(f"{flag_of(t)} *{escape_md(t['name'][:26])}*")
        lines.append(f"　 ${fmt_price(t['price'])}　24h {t['chg24']:+.1f}%　"
                     f"池 ${t['liq']:,.0f}")
    if not shown:
        return f"🔥 {cn} 暂时取不到热门池，稍后再试"
    lines.append(f"\n⛔ 池子<${LIQ_DANGER // 1000}k，进得去出不来"
                 f"｜⚠️ 池子<${LIQ_THIN // 1000}k，滑点难看"
                 f"｜🚀 24h涨超{PUMP_WARN:.0f}%，多半是给人出货的流动性"
                 f"｜💀 池子大但没人交易，标称流动性不可信")
    lines.append("⚠️ 热门 ≠ 该买。链上新池 24h 归零很常见，不构成投资建议")
    return "\n".join(lines)


def chain_kb(current=DEFAULT_CHAIN):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keys = list(CHAINS)
    rows, row = [], []
    for k in keys:
        row.append(InlineKeyboardButton(
            ("✅" if k == current else "") + CHAINS[k]["cn"],
            callback_data=f"oc:t:{k}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔍 查某个币（发名字或合约地址）",
                                      callback_data="oc:ask")])
    rows.append([InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


# ---------- 命令 / 按钮 ----------
USAGE = (
    "🔗 *链上代币查询*\n\n"
    "交易所没上的币，在链上先跑——这里查的就是那一段。\n\n"
    "`/onchain pepe`　按名字搜（按流动性排序）\n"
    "`/onchain 0x6982…1933`　按合约地址查（**精确**，推荐）\n"
    "`/onchain 热门 bsc`　看某条链的真实热门\n\n"
    "支持：" + "、".join(v["cn"] for v in CHAINS.values()) + "\n"
    "⚠️ 链上没有上币审核，同名假币是常态——认合约地址，别认名字。"
)


async def onchain_cmd(update, context):
    from handlers.util import safe_reply
    args = context.args or []
    if not args:
        await safe_reply(update.message, USAGE, reply_markup=chain_kb(),
                         parse_mode="Markdown")
        return
    q = " ".join(args).strip()
    if args[0] in ("热门", "trending", "hot"):
        if len(args) > 1:
            chain = resolve_chain(args[1])
            if not chain:
                await safe_reply(
                    update.message,
                    f"不认识链「{escape_md(args[1])}」。支持："
                    + "、".join(f"{v['cn']}({k})" for k, v in CHAINS.items()),
                    parse_mode="Markdown")
                return
        else:
            chain = DEFAULT_CHAIN
        await _send_trending(update.message, chain)
        return
    await _send_query(update.message, q)


async def _send_query(message, q):
    from handlers.util import safe_reply
    try:
        if is_address(q):
            t, pools = await by_address(q)
            if not t:
                await safe_reply(message,
                                 f"链上没查到这个地址的交易对。\n"
                                 f"可能是还没有 DEX 池子，或地址抄错了。")
                return
            await safe_reply(message, render_token(t, pools),
                             reply_markup=detail_kb(t), parse_mode="Markdown")
            return
        items, total = await by_name(q)
        if not items:
            await safe_reply(message, f"链上没搜到「{q}」。换个写法，或直接发合约地址。")
            return
        if total == 1:
            await safe_reply(message, render_token(items[0]),
                             reply_markup=detail_kb(items[0]), parse_mode="Markdown")
            return
        await safe_reply(message, render_list(items, q, total),
                         reply_markup=list_kb(items), parse_mode="Markdown")
    except Exception as e:
        log.error(f"链上查询失败 {q}: {e}")
        await safe_reply(message, f"查询失败：{str(e)[:80]}")


async def _send_trending(message, chain):
    from handlers.util import safe_reply
    try:
        items = await trending(chain)
    except Exception as e:
        log.error(f"链上热门失败 {chain}: {e}")
        await safe_reply(message, f"取热门失败：{str(e)[:80]}")
        return
    await safe_reply(message, render_trending(items, chain),
                     reply_markup=chain_kb(chain), parse_mode="Markdown")


async def on_button(query, context):
    """处理 oc:* 回调。由 menu 转发。"""
    from handlers.util import safe_edit
    bits = query.data.split(":")
    what = bits[1] if len(bits) > 1 else ""
    if what == "ask":
        context.user_data["await_onchain"] = True
        await query.answer()
        await safe_edit(query,
                        "🔗 *查链上代币*\n\n直接发**合约地址**最准（`0x…` 或 Solana 地址）；\n"
                        "发名字也行，但同名假币很多，我会按流动性排序给你。\n\n"
                        "取消发 /menu",
                        parse_mode="Markdown")
        return
    if what == "t":
        chain = bits[2] if len(bits) > 2 else DEFAULT_CHAIN
        await query.answer(f"取 {CHAINS.get(chain, {}).get('cn', chain)} 热门…")
        try:
            items = await trending(chain)
        except Exception as e:
            log.error(f"链上热门失败 {chain}: {e}")
            await safe_edit(query, f"取热门失败：{str(e)[:80]}",
                            reply_markup=chain_kb(chain))
            return
        await safe_edit(query, render_trending(items, chain),
                        reply_markup=chain_kb(chain), parse_mode="Markdown")
        return
    if what == "d":                       # 详情：oc:d:<链>:<代币地址>
        chain, addr = (bits[2] if len(bits) > 2 else ""), (bits[3] if len(bits) > 3 else "")
        await query.answer("查详情…")
        try:
            t, pools = await by_address(addr)
        except Exception as e:
            log.error(f"链上详情失败 {addr}: {e}")
            await query.message.reply_text(f"查询失败：{str(e)[:80]}")
            return
        if not t:
            await query.message.reply_text("这个地址查不到交易对了")
            return
        # 用 reply 而不是 edit：列表那条要留着，否则想看第二个候选还得重搜
        await _reply_md(query.message, render_token(t, pools), detail_kb(t))
        return

    if what == "w":                       # 监控：oc:w:<代币地址>:<百分比>
        addr = bits[2] if len(bits) > 2 else ""
        try:
            pct = float(bits[3]) if len(bits) > 3 else 10.0
        except ValueError:
            pct = 10.0
        await query.answer("建立监控…")
        from handlers.watchpct import add_watch
        chat_id = query.message.chat_id if query.message else 0
        who = query.from_user.first_name if query.from_user else "我"
        ok, msg = await add_watch(chat_id, addr, pct, who)
        await _reply_md(query.message, msg + ("\n\n取消发 /watchpcts 看列表" if ok else ""))
        return

    if what == "k":                       # K线：oc:k:<链>:<池子地址>:<周期>
        chain = bits[2] if len(bits) > 2 else ""
        pool = bits[3] if len(bits) > 3 else ""
        tf = bits[4] if len(bits) > 4 else "1h"
        await query.answer(f"画 {tf} K线…")
        t = {"chain_key": chain, "pool": pool, "symbol": "", "address": pool,
             "chain": chain, "chain_cn": CHAINS.get(chain, {}).get("cn", chain),
             "liq": 0.0}
        # 池子地址反查一下代币信息，图下面的说明才有币名和池子大小
        try:
            d = await _get(f"{DS}/latest/dex/pairs/{CHAINS.get(chain, {}).get('ds', chain)}/{pool}")
            ps = d.get("pairs") or ([d.get("pair")] if d.get("pair") else [])
            if ps and ps[0]:
                t = _pair(ps[0])
        except Exception as e:
            log.debug(f"反查池子信息失败 {pool}: {e}")
        r = await build_chart(t, tf)
        if not r:
            # 建议往**小**周期换：新池子在大周期上根本没几根
            # （实测 3 天大的池子，1d 只有 4 根，而 15m 有 200 根）
            smaller = [k for k in TF if list(TF).index(k) < list(TF).index(tf)] \
                if tf in TF else list(TF)[:1]
            tip = f"试试 {'／'.join(smaller)}" if smaller else "这个池子太新，还没有K线"
            await query.message.reply_text(
                f"画不出 {tf} K线——这个池子建得太新，大周期上还凑不够 10 根。{tip}")
            return
        buf, cap = r
        try:
            await query.message.reply_photo(photo=buf, caption=cap,
                                            parse_mode="Markdown",
                                            reply_markup=kline_kb(t, tf))
        except Exception as e:
            log.warning(f"链上K线发图失败，降级: {e}")
            buf.seek(0)
            await query.message.reply_photo(photo=buf, caption=cap.replace("*", ""),
                                            reply_markup=kline_kb(t, tf))
        return

    await query.answer("不认识的操作")


async def _reply_md(message, text, kb=None):
    """回消息，Markdown 挂了就降级——链上代币名什么字符都有，别为了排版把内容丢了。"""
    try:
        await message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        log.warning(f"链上详情 Markdown 失败，降级: {e}")
        await message.reply_text(text.replace("*", "").replace("`", ""),
                                 reply_markup=kb)


async def price_of(addr):
    """按合约地址取现价（监控/预警轮询用）。返回 (价格, 代币) 或 (None, None)。"""
    try:
        t, _pools = await by_address(addr)
    except Exception as e:
        log.warning(f"链上取价失败 {addr[:12]}: {e}")
        return None, None
    if not t or not t.get("price"):
        return None, None
    return t["price"], t
