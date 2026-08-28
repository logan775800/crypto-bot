"""持仓结构变化 `/pos` —— 这一波是谁推的：大户在加，还是散户在接。

告警只告诉你「涨了 30%」，清算地图告诉你「上下还堆着多少爆仓单」。
**都没回答最该问的那句：这波涨是谁买上去的，还有没有人接力。**
这个模块补的就是那一层。

## 三个数，各管一件事（口径不同，别混着读）

    持仓量 OI          仓位总额       钱是进来了还是走了
    大户持仓多空比      按**持仓金额** 大钱站哪边
    人数多空比          按**账户个数** 人头站哪边（一个大户和一个一百块的散户各算一票）

后两个口径不同正是**它们要分开列**的原因：金额比升 + 人头比降 =
大钱在加多、人头在转空，这是两拨人在对赌，不是同一件事的两种说法。

## 主判据是 OI，不是那两个比值（实测排的序，不是拍的）

`tools\\probe_posflow.py` 同一时刻量了两组币（涨跌最大 25 个 vs 成交额最大
40 个作基线），看 24 小时里各自动多少：

                    大涨大跌那批    平静的基线      比值
      |ΔOI|            23.4%          6.6%        3.5x
      |Δ人数比|         14.0%          5.7%        2.5x
      |Δ大户比|          9.5%          6.9%        1.4x   ← 几乎分不开

**大户持仓比日常自己就在飘 7%**，所以它单独看不算证据，只能用来给 OI 的
结论补方向。判读的主干必须是 OI：

    价涨 + OI 增  → 新资金进场，涨势有人接力
    价涨 + OI 减  → 空头平仓推上去的（轧空），**上方燃料正在被烧掉**，
                    烧完就没人接力了 —— 这是最关键的转折信号
    价跌 + OI 增  → 新空在推
    价跌 + OI 减  → 多头认赔离场，抛压在释放

**门槛统一取 10%**：基线组的中位数是 6~7%，75 分位 9~15%。低于 10% 的变动
在不涨不跌的币上天天发生，当成"变化"来解读就是在读噪音。

## 为什么"上方清算燃料清完了"不等于见顶

永续里空头持仓不会归零——价格越高做空的诱惑越大，几小时就有新空补进来。
所以多头拉盘要的不是"现存的空头"，而是源源不断的新空头。
上方清空之后多头的四个获利来源里，爆空（强制买入、几乎零成本推高）归零，
资金费从收钱变成付钱，只剩"卖给追高盘"和"高位派发"——
边际成本升、边际收益降，理性选择往往是**停在高位横盘，等散户逆势做空
把上方燃料重新堆起来，再拉一次**。

区分"补给中"和"真见顶"的就是 OI：燃料清空 + OI 不降 = 多头没走在等新空；
燃料清空 + OI 开始降 = 多头在派发。这也是本模块存在的理由。

## 覆盖范围（必须写在卡片上）

大户持仓比**只有币安有**，Bybit / OKX 都不提供。人数比币安和 Bybit 都有
（口径不同不能混算，所以是二选一不是合并，理由同 `/lsr`）。
币安没有这个合约的币，这一段直接不出现——不编，也不写"数据正常"。
"""
import asyncio
import logging

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.util import safe_reply, safe_edit, escape_md

log = logging.getLogger(__name__)

BN = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"

# 算不算"变了"。基线组（没涨没跌的大盘币）中位数 6~7%、75 分位 9~15%，
# 所以 10% 以下当噪音。三个指标共用一个门槛是实测支持的，不是图省事。
MOVE = 10.0
# 资金费率年化多高算"多头在付大钱扛"。复用 precheck 的口径，别两处各定一个。
from handlers.precheck import funding_apr, FUNDING_COSTLY_APR      # noqa: E402

DEFAULT_HOURS = 24


# ── 取数 ────────────────────────────────────────────────────
async def _bn_series(client, ep, inst, limit):
    r = await client.get(f"{BN}/futures/data/{ep}",
                         params={"symbol": inst, "period": "1h", "limit": limit})
    if r.status_code != 200:
        return []
    d = r.json()
    return d if isinstance(d, list) else []


async def _bybit_retail(client, inst, limit):
    """Bybit 的人数多空比。**它给的是两个占比（和为 1），不是比值**，
    要自己相除——和 lsratio 里那条同一个坑。"""
    r = await client.get(f"{BYBIT}/v5/market/account-ratio",
                         params={"category": "linear", "symbol": inst,
                                 "period": "1h", "limit": limit})
    d = r.json()
    if d.get("retCode") != 0:
        return []
    lst = (d.get("result") or {}).get("list") or []
    out = []
    for x in lst:
        buy, sell = float(x["buyRatio"]), float(x["sellRatio"])
        if sell > 0:
            out.append(buy / sell)
    # Bybit 返回的是**倒序**（最新在前），翻过来对齐币安
    return list(reversed(out))


def _delta(v0, v1):
    """相对变化（%）。用相对值而不是差值：大户比基数 0.8 和 3.0 的币，
    同样 +0.3 的意义差好几倍，绝对差没法跨币比较。"""
    if v0 is None or v1 is None or v0 <= 0:
        return None
    return (v1 - v0) / v0 * 100


async def fetch(symbol, hours=DEFAULT_HOURS):
    """→ dict 或 None。取不到的字段留 None，**不猜不补**。"""
    base = symbol.upper().replace("USDT", "") or symbol.upper()
    inst = f"{base}USDT"
    n = max(2, int(hours) + 1)
    out = {"sym": base, "hours": hours, "top_src": None, "retail_src": None}

    async with httpx.AsyncClient(timeout=12) as client:
        top, retail, oi, prem, tick = await asyncio.gather(
            _bn_series(client, "topLongShortPositionRatio", inst, n),
            _bn_series(client, "globalLongShortAccountRatio", inst, n),
            _bn_series(client, "openInterestHist", inst, n),
            client.get(f"{BN}/fapi/v1/premiumIndex", params={"symbol": inst}),
            client.get(f"{BN}/fapi/v1/ticker/24hr", params={"symbol": inst}),
            return_exceptions=True)

        def ser(x, key):
            if isinstance(x, Exception) or not x or len(x) < 2:
                return None, None
            try:
                return float(x[0][key]), float(x[-1][key])
            except (KeyError, TypeError, ValueError) as e:
                log.info(f"[posflow] {inst} 字段解析失败 {key}: {e}")
                return None, None

        t0, t1 = ser(top, "longShortRatio")
        r0, r1 = ser(retail, "longShortRatio")
        o0, o1 = ser(oi, "sumOpenInterestValue")
        if t1 is not None:
            out["top_src"] = "币安"
        if r1 is not None:
            out["retail_src"] = "币安"
        else:
            # 币安没有这个币（Bybit 独占的币不少，见 /source 那份覆盖表），
            # 人数比还能从 Bybit 拿到。大户持仓比就真的没有——三家只有币安做。
            try:
                bb = await _bybit_retail(client, inst, n)
                if len(bb) >= 2:
                    r0, r1 = bb[0], bb[-1]
                    out["retail_src"] = "Bybit"
            except Exception as e:
                log.info(f"[posflow] {inst} Bybit 人数比取数失败: {e}")

        rate = None
        if not isinstance(prem, Exception) and prem.status_code == 200:
            try:
                rate = float(prem.json().get("lastFundingRate")) * 100
            except (TypeError, ValueError):
                rate = None
        # 24h 涨跌幅：告警那边本来就有，会直接传进来；`/pos` 手查时要自己拿一次。
        # 判「价涨+仓增」还是「价涨+仓减」全靠它，缺了就只能列数字不下结论。
        chg = None
        if not isinstance(tick, Exception) and tick.status_code == 200:
            try:
                chg = float(tick.json().get("priceChangePercent"))
            except (TypeError, ValueError):
                chg = None

    interval_h = None
    if rate is not None:
        try:
            from handlers import detail as _d
            fi = await _d.get_funding_interval(base)
            if isinstance(fi, dict):
                interval_h = fi.get("hours")
        except Exception as e:
            log.info(f"[posflow] {inst} 结算周期取数失败: {e}")

    out.update({
        "top": t1, "top_prev": t0, "top_pct": _delta(t0, t1),
        "retail": r1, "retail_prev": r0, "retail_pct": _delta(r0, r1),
        "oi": o1, "oi_pct": _delta(o0, o1),
        "funding": rate, "funding_h": interval_h, "chg": chg,
        "funding_apr": funding_apr(rate, interval_h) if rate is not None else None,
    })
    if out["top"] is None and out["retail"] is None and out["oi"] is None:
        return None
    return out


# ── 判读 ────────────────────────────────────────────────────
def _dir(pct):
    """→ 1 涨 / -1 跌 / 0 没怎么动 / None 没数据。"""
    if pct is None:
        return None
    if pct >= MOVE:
        return 1
    if pct <= -MOVE:
        return -1
    return 0


def who_line(top_pct, retail_pct, up=True):
    """大户和散户各往哪边挪。**两个口径不同，所以分开说**：
    金额比升 + 人头比降 = 大钱加多、人头转空，这是两拨人在对赌。

    实测（涨跌最大的 25 个币）大户与散户方向相反的有 13 个——
    不是罕见现象，所以值得单列一行；也不是常态，所以出现时确实有信息。

    ⚠️ **必须知道价格往哪边走才能解释这两个数**。第一版没传 `up`，
    真机一跑就露馅：MVLL 跌了 25%、两个比值都在涨，卡片却印
    「多头拥挤度在上升，回调时容易多杀多」——那是涨势里的话术，
    放在正在砸的币上完全是另一回事（那是抄底盘在往下接）。
    同一组数字配不同的价格方向，讲的是两个故事。
    """
    t, r = _dir(top_pct), _dir(retail_pct)
    if t in (None, 0) and r in (None, 0):
        return ""
    if up:
        # 涨势里比值**下降**＝有人在往里加空。空头是多头的燃料，
        # 所以这在他那套框架里是"补给中"，不是利空。
        if t == 1 and r == -1:
            return "大户在加多、散户在逆势转空 → 上方燃料在重建，像蓄力不像出货"
        if t == -1 and r == 1:
            return ("大户在减多、散户在追多 → 典型的派发形态，接盘的是散户"
                    "（这一栏出现时最该警惕）")
        if t == 1 and r == 1:
            return "大户散户一起追多 → 上方空单在被消耗，拥挤度升高，回调容易多杀多"
        if t == -1 and r == -1:
            return "拉上去的同时两边都在往空头挪 → 新空在冲进来，上方燃料在重建"
        if t == 1:
            return "大户在加多，散户人数比没明显变化"
        if t == -1:
            return "大户在减多（价格还在涨）→ 大钱在借这波出货，散户人数比没动"
        if r == 1:
            return "散户在追多，大户仓位没怎么动 → 上方空单在被消耗"
        return "散户在逆势转空，大户仓位没怎么动 → 上方燃料在重建"
    # 跌势：比值**上升**＝有人在往下接。下方多单越堆越多，是继续下跌的燃料。
    if t == 1 and r == -1:
        return "大户在抄底、散户在追空 → 大钱和人头站在了对立面"
    if t == -1 and r == 1:
        return ("大户在减多、散户在抄底 → 大钱先走人头后接，最差的一种组合"
                "（这一栏出现时最该警惕）")
    if t == 1 and r == 1:
        return "砸下来的同时两边都在加多 → 抄底盘在往下接，下方清算位越堆越厚"
    if t == -1 and r == -1:
        return "两边都在减多 → 多头在认赔离场，没人急着接"
    if t == 1:
        return "大户在抄底，散户人数比没明显变化"
    if t == -1:
        return "大户在减多 → 大钱在往外撤，散户人数比没动"
    if r == 1:
        return "散户在抄底，大户仓位没怎么动 → 下方多单越堆越厚"
    return "散户在追空，大户仓位没怎么动"


def verdict(price_chg, oi_pct, top_pct=None, retail_pct=None, apr=None):
    """一句话结论。**主干是 OI**（实测里它是唯一能把大涨大跌和平静行情
    分得开的指标，3.5 倍），那两个比值只用来给方向补注解。

    返回 [] 而不是"结构正常"——取不到数和真的没事是两回事。
    """
    out = []
    if price_chg is None:
        return out
    o = _dir(oi_pct)
    up = price_chg > 0
    if o is None:
        out.append("持仓量没取到，这波是新资金进场还是平仓推的判不了")
    elif o == 0:
        out.append(f"持仓量几乎没动（{oi_pct:+.1f}%）：这波是场内换手，"
                   f"没有明显的新钱进出")
    elif up and o == 1:
        out.append(f"价涨 + 持仓增（{oi_pct:+.1f}%）：**新资金在进场**，涨势有人接力")
    elif up and o == -1:
        out.append(f"价涨 + 持仓减（{oi_pct:+.1f}%）：**是空头平仓推上去的（轧空）**，"
                   f"不是新资金。上方燃料正在被烧掉，烧完就没人接力了")
    elif not up and o == 1:
        out.append(f"价跌 + 持仓增（{oi_pct:+.1f}%）：**新空在推**，跌势有人接力")
    else:
        out.append(f"价跌 + 持仓减（{oi_pct:+.1f}%）：多头在认赔离场，"
                   f"抛压在释放，跌势可能接近尾声")

    w = who_line(top_pct, retail_pct, up)
    if w:
        out.append(w)

    # 资金费只在贵到影响持仓成本时才说。日常 0.01%/8h ≈ 年化 11%，说了是废话。
    if apr is not None and abs(apr) >= FUNDING_COSTLY_APR:
        payer = "多头" if apr > 0 else "空头"
        out.append(f"资金费年化 {apr:+.0f}%，{payer}在为持仓付大钱——"
                   f"时间不站在他们那边，拖越久越容易先松手")
    return out


# ── 渲染 ────────────────────────────────────────────────────
def _ratio(v, prev, pct, src):
    """`2.09（24h 前 1.62，+29%）`。前值一定要印：只给一个当前值的话，
    "变化"这件事根本看不出来——而他要的就是变化。"""
    if v is None:
        return None
    s = f"{v:.2f}"
    if prev is not None and pct is not None:
        s += f"（{prev:.2f} → {v:.2f}，{pct:+.0f}%）"
    if src and src != "币安":
        s += f" · {src}"
    return s


def lines(f, price_chg=None, compact=False):
    """→ list[str]。compact=True 给告警配图的 caption 用（1024 字上限，
    每多一行都在挤别的内容），完整版给 /pos。"""
    if not f:
        return []
    out = []
    t = _ratio(f.get("top"), f.get("top_prev"), f.get("top_pct"), f.get("top_src"))
    r = _ratio(f.get("retail"), f.get("retail_prev"), f.get("retail_pct"),
               f.get("retail_src"))
    h = f.get("hours", DEFAULT_HOURS)
    if t:
        out.append(f"大户持仓比 {t}")
    if r:
        out.append(f"人数多空比 {r}")
    if f.get("oi") is not None and f.get("oi_pct") is not None:
        from handlers.liqmap import _money
        out.append(f"持仓量 {_money(f['oi'])} U（{h}h {f['oi_pct']:+.1f}%）")
    if not compact:
        out.append("　└ 大户比按**持仓金额**，人数比按**账户个数**——"
                   "两个口径不同，分歧本身就是信息")
    for v in verdict(price_chg, f.get("oi_pct"), f.get("top_pct"),
                     f.get("retail_pct"), f.get("funding_apr")):
        out.append(f"→ {v}")
    return out


def block(f, price_chg=None, compact=False):
    """拼成可以直接贴进别的卡片的一段。没内容返回空串。"""
    ls = lines(f, price_chg, compact)
    if not ls:
        return ""
    return f"\n\n📊 *这波是谁推的*（近 {f.get('hours', DEFAULT_HOURS)} 小时）\n" \
           + "\n".join(ls)


_cache = {}          # sym -> (ts, block)
CACHE_TTL = 120


async def attach(symbol, price_chg, hours=DEFAULT_HOURS, compact=True):
    """给告警用的一步到位版本。**任何失败都返回空串**——
    告警本身已经有价值，不能因为补一段结构分析取不到数就整条发不出去。
    但失败要进日志和心跳（配清算图那次静默失败，图消失了几个版本没人发现）。

    带 2 分钟缓存：告警是**逐个订阅群**发的，同一轮同一个币会被问 N 次。
    没缓存的话订阅群越多打的接口越多，而那 N 次答案完全一样。
    """
    import time
    key = f"{symbol}:{hours}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        f = await fetch(symbol, hours)
        out = block(f, price_chg, compact)
        _cache[key] = (time.time(), out)
        for k in [k for k, v in _cache.items() if time.time() - v[0] > CACHE_TTL * 4]:
            _cache.pop(k, None)
        return out
    except Exception as e:
        log.error(f"[posflow] {symbol} 持仓结构取数失败: {e}", exc_info=True)
        if isinstance(e, (TypeError, ValueError, AttributeError, KeyError)):
            try:
                from handlers import monitor as _m
                _m.beat("告警配持仓结构", False, 300, f"{type(e).__name__}: {e}")
            except Exception:
                pass
        return ""


# ── 入口 ────────────────────────────────────────────────────
def kb(sym):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💣 清算地图", callback_data=f"lq:w:{sym}:7日"),
         InlineKeyboardButton("🔄 刷新", callback_data=f"pf:r:{sym}")],
        [InlineKeyboardButton("ℹ️ 口径", callback_data=f"pf:i:{sym}"),
         InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")],
    ])


async def build_text(sym, hours=DEFAULT_HOURS):
    f = await fetch(sym, hours)
    if not f:
        return (f"📊 *{escape_md(sym)} 持仓结构*\n\n"
                f"币安和 Bybit 都没有 {escape_md(sym)} 的永续合约数据。\n"
                f"大户持仓比**只有币安做**（Bybit / OKX 都不提供），"
                f"所以币安没上的币这一段拿不到——不是出错。")
    chg = f.get("chg")
    head = f"📊 *{escape_md(sym)} 持仓结构*　近 {hours} 小时"
    if chg is not None:
        head += f"　价格 {chg:+.1f}%"
    body = lines(f, chg)
    tail = ("\n变化门槛 10%（基线组日常就飘 6~7%，低于这个数是噪音）"
            "｜大户比仅币安有｜⚠️ 结构分析不构成投资建议")
    return head + "\n\n" + "\n".join(body) + "\n" + tail


def detail_text(sym):
    return "\n".join([
        "ℹ️ *持仓结构 · 口径*", "━━━━━━━━━━━━━━",
        "*三个数各管一件事*",
        "　持仓量 OI　　　合约仓位总额　　钱是进来了还是走了",
        "　大户持仓比　　按**持仓金额**　大钱站哪边",
        "　人数多空比　　按**账户个数**　人头站哪边",
        "",
        "后两个口径不同，所以**分开列**：一个百万大户和一个一百块的散户，",
        "在人数比里各算一票，在大户比里差一万倍。金额比升 + 人头比降 =",
        "大钱在加多、人头在转空，这是两拨人在对赌，不是同一件事的两种说法。",
        "",
        "*为什么主判据是持仓量，不是那两个比值*",
        "同一时刻量了两组币（涨跌最大 25 个 vs 成交额最大 40 个作基线），",
        "看 24 小时里各自动多少：",
        "```",
        "               大涨大跌那批   平静基线   比值",
        "  |ΔOI|            23.4%       6.6%    3.5x",
        "  |Δ人数比|         14.0%       5.7%    2.5x",
        "  |Δ大户比|          9.5%       6.9%    1.4x",
        "```",
        "大户持仓比日常自己就在飘 7%，单独看不算证据，只能给结论补方向。",
        "**变化门槛统一取 10%**，低于它的变动在不涨不跌的币上天天发生。",
        "",
        "*四种组合怎么读*",
        "　价涨+仓增 → 新资金进场，涨势有人接力",
        "　价涨+仓减 → 空头平仓推的（轧空），上方燃料在被烧掉",
        "　价跌+仓增 → 新空在推",
        "　价跌+仓减 → 多头认赔离场，抛压在释放",
        "",
        "*上方清算燃料清完了 ≠ 见顶*",
        "永续里空头持仓不会归零——价格越高做空诱惑越大，几小时就有新空补进来。",
        "所以多头要的不是「现存的空头」，而是**源源不断的新空头**。",
        "上方清空之后，爆空（强制买入、几乎零成本推高）这一项归零，",
        "资金费从收钱变成付钱，只剩「卖给追高盘」和「高位派发」——",
        "边际成本升、边际收益降，理性选择往往是停在高位横盘，",
        "等散户逆势做空把上方燃料重新堆起来，再拉一次。",
        "",
        "区分「补给中」和「真见顶」的就是持仓量：",
        "　燃料清空 + 仓不降 → 多头没走，在等新空进场（蓄力）",
        "　燃料清空 + 仓开始降 → 多头在派发平仓（见顶概率高）",
        "　燃料清空 + 费率极高 → 多头在付大钱扛，拖越久越容易先崩",
        "",
        "另外：上方清空也是**多头风险最大的位置**。此时多头浮盈最大、",
        "占比最高，下方全是自己人的清算位，没有空头当垫背——",
        "一旦有人获利了结，接不住就变成多杀多。",
        "",
        "*覆盖范围*",
        "大户持仓比**只有币安提供**，Bybit / OKX 都没有这个接口。",
        "人数比币安和 Bybit 都有，但两家统计的是各自的用户，数字对不上，",
        "所以是二选一不是合并（同 /lsr 那条）。币安没有的币优先走 Bybit，",
        "两家都没有就整段不出现——不编数据。",
        "",
        "⚠️ 结构分析不构成投资建议",
    ])


async def pos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pos <币> —— 这波是谁推的：大户加仓还是散户接盘。"""
    if not context.args:
        await safe_reply(update.message,
                         "用法：`/pos BTC`\n\n看这个币近 24 小时里大户仓位、"
                         "散户人数比、持仓量各挪了多少，判「新资金进场」还是"
                         "「轧空推的」。", parse_mode="Markdown")
        return
    sym = str(context.args[0]).upper().replace("USDT", "")
    msg = await safe_reply(update.message, f"📊 拉 {sym} 的持仓结构…")
    try:
        text = await build_text(sym)
    except Exception as e:
        log.error(f"/pos {sym} 出错: {e}", exc_info=True)
        await safe_reply(update.message, f"取数失败，稍后再试：{str(e)[:80]}")
        return
    if msg:
        try:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb(sym))
            return
        except Exception:
            pass
    await safe_reply(update.message, text, parse_mode="Markdown",
                     reply_markup=kb(sym))


async def from_btn(query, context, sym, detail=False):
    if detail:
        await safe_edit(query, detail_text(sym), parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            "⬅️ 回结构卡", callback_data=f"pf:r:{sym}")]]))
        return
    await safe_edit(query, f"📊 拉 {sym} 的持仓结构…")
    try:
        text = await build_text(sym)
    except Exception as e:
        log.error(f"持仓结构按钮出错 {sym}: {e}", exc_info=True)
        await safe_edit(query, f"取数失败：{str(e)[:80]}", reply_markup=kb(sym))
        return
    await safe_edit(query, text, parse_mode="Markdown", reply_markup=kb(sym))
