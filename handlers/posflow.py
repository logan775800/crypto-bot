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

# ── Gate contract_stats：一个接口给六样，而且比币安覆盖广 ──────
#
# 发现经过：他贴了一份 BTR 的 13 小时分析（爆仓量、账户数原值、大户比稳定性），
# 我照着去核数，发现每个数字都能在 Gate 的 `contract_stats` 里对上
# —— `lsr_account` 0.3151、`top_lsr_size` 1.3225，和截图一字不差。
#
# 实测对比（2026-08-28）：
#
#                     Gate            币安
#     USDT 永续       942（独占 300）  703（独占 61）
#     爆仓金额分多空   ✅ 逐小时        ❌ allForceOrders 已 404
#     账户数**原值**   ✅ 837→680      ❌ 只给占比，比值反推不出人数
#     大户持仓比       ✅              ✅
#     历史深度         1h × 2000 根    30 天
#
# 所以 Gate 有就走 Gate，没有才退回币安。**这不是"换个源"，是多了两类数据**：
# 爆仓量和账户数原值——而"轧空引擎已经熄火"这种结论只能从爆仓量的
# 两段对比里得出，任何单点快照都给不出来。
GATE = "https://api.gateio.ws/api/v4"
GATE_MAX_BARS = 2000       # 1h 粒度能拉 83 天

# OI 在窗口里的振幅低于这个数，且涨跌次数接近对半 → 判"横盘，没有一方在净建仓"。
# 10% 这条来自之前量过的基线：平静的大盘币 24 小时 |ΔOI| 中位数 6.6%、75 分位 9.4%。
FLAT_AMP = 10.0


async def _gate_stats(client, sym, bars):
    """Gate 的逐小时合约统计。→ list（旧→新）或 []。"""
    r = await client.get(f"{GATE}/futures/usdt/contract_stats",
                         params={"contract": f"{sym}_USDT", "interval": "1h",
                                 "limit": min(bars, GATE_MAX_BARS)})
    if r.status_code != 200:
        return []
    d = r.json()
    return d if isinstance(d, list) else []


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def seg_stats(rows):
    """一段时间里的持仓结构。rows 是连续的逐小时统计（旧→新）。

    **爆仓量是"这段时间里实际被打掉了多少"，和持仓量是两回事**：
    持仓量说的是现在还剩多少仓位，爆仓量说的是过程中烧掉了多少燃料。
    两段爆仓量一比，才看得出"轧空引擎还着着没有"。
    """
    if not rows or len(rows) < 2:
        return None
    oi = [_f(x.get("open_interest")) for x in rows]
    oi = [v for v in oi if v > 0]
    if len(oi) < 2:
        return None
    up = sum(1 for a, b in zip(oi, oi[1:]) if b > a)
    dn = sum(1 for a, b in zip(oi, oi[1:]) if b < a)
    # 这段时间**一共平掉了多少名义金额**（只累计减少的那些根）。
    #
    # ⚠️ 必须**逐根按当时的价格**折算，不能拿总张数变化乘期末价。
    # 真机撞到的：BTR 那段价格跌了 52%，用期末价折算的话，早先在高位
    # 平掉的仓被严重低估 → 分母偏小 → "强平占比"被算成 76%（虚高）。
    #
    # 也**不能直接拿 open_interest_usd 的首尾差**：那个数同时包含
    # "有人平仓"和"价格跌了"两件事——价格腰斩而没人动手时它也会腰斩。
    closed = 0.0
    for a, b in zip(rows, rows[1:]):
        da = _f(a.get("open_interest")) - _f(b.get("open_interest"))
        if da <= 0:
            continue
        px = _f(b.get("mark_price"))
        oa, ua = _f(a.get("open_interest")), _f(a.get("open_interest_usd"))
        # 优先用这根自己的张→美元换算率，取不到再退回标记价
        rate = (ua / oa) if (oa > 0 and ua > 0) else px
        if rate > 0:
            closed += da * rate
    return {
        "bars": len(rows),
        "closed_usd": closed,
        "oi_first": oi[0], "oi_last": oi[-1],
        "oi_pct": (oi[-1] - oi[0]) / oi[0] * 100,
        "oi_usd": _f(rows[-1].get("open_interest_usd")),
        "amp": (max(oi) / min(oi) - 1) * 100,
        "up": up, "dn": dn,
        "long_liq": sum(_f(x.get("long_liq_usd")) for x in rows),
        "short_liq": sum(_f(x.get("short_liq_usd")) for x in rows),
        "retail_first": _f(rows[0].get("lsr_account")),
        "retail_last": _f(rows[-1].get("lsr_account")),
        "top_first": _f(rows[0].get("top_lsr_size")),
        "top_last": _f(rows[-1].get("top_lsr_size")),
        "long_users_first": int(_f(rows[0].get("long_users"))),
        "long_users_last": int(_f(rows[-1].get("long_users"))),
        "short_users_first": int(_f(rows[0].get("short_users"))),
        "short_users_last": int(_f(rows[-1].get("short_users"))),
        "funding": _f(rows[-1].get("last_funding_rate")) * 100,
    }


def is_flat(s):
    """这段是横盘还是趋势。

    **只看首尾差会把横盘读成"没动"，把来回震荡读成"平静"**——
    真正区分的是：振幅小 *且* 上升下降次数接近对半（= 没有一方在净建仓）。
    他贴的那份分析第一条就是这个：13 小时 OI 振幅 5.7%、上升 6 次下降 7 次，
    价格却振了 8.6% —— 价格在动而仓位没净变化，那就是横盘不是趋势。
    """
    if not s or s["bars"] < 4:
        return False
    n = s["up"] + s["dn"]
    if n == 0:
        return True
    return s["amp"] < FLAT_AMP and abs(s["up"] - s["dn"]) <= max(1, n // 4)


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


# ── 两段对比：这一段 vs 上一段同样长的时间 ────────────────────
async def fetch_segments(symbol, hours=DEFAULT_HOURS, prev_mult=None):
    """→ {"now": seg, "prev": seg, "hours":…, "prev_hours":…} 或 None。

    **对比段默认取"到目前为止的整段行情"，不是等长**：他那份分析里
    13 小时横盘的前面是 47 小时单边扩张，两段长度不一样但都是完整的一段。
    等长对比会把 47 小时的单边硬切成 13 小时，"OI +450%"这个量级就没了。
    prev_mult 给几就往前取几倍长度（默认 3 倍，够覆盖上一段行情）。
    """
    import httpx
    sym = symbol.upper().replace("USDT", "")
    hours = max(2, int(hours))
    mult = prev_mult if prev_mult is not None else 3
    want = hours * (1 + mult) + 1
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            rows = await _gate_stats(c, sym, want)
        except Exception as e:
            log.info(f"[posflow] {sym} Gate 统计取数失败: {e}")
            return None
    if len(rows) < hours + 2:
        return None
    cur = rows[-hours:]
    prev = rows[:-hours][-hours * mult:]
    return {"sym": sym, "hours": hours,
            "prev_hours": len(prev),
            "now": seg_stats(cur), "prev": seg_stats(prev),
            "src": "Gate"}


def squeeze_verdict(now, prev):
    """轧空引擎还着着没有。**这条只能从两段爆仓量的对比里得出**。

    他那份分析里最有分量的一句就是这个：13 小时总爆仓 多 $11.2k / 空 $12.0k，
    而前 47 小时是 多 $264k / 空 $1.17M（爆空占 82%）—— 塌了两个数量级。
    上方那批贴身的空头燃料昨天就烧完了，现在两边都没有可点的东西。

    单看当前那一段是**看不出来的**：$12k 的爆空量本身既不高也不低，
    只有和上一段一比才知道是"熄火"还是"从来就没着过"。
    """
    if not now or not prev:
        return None
    a = now["long_liq"] + now["short_liq"]
    b = prev["long_liq"] + prev["short_liq"]
    # 换算成每小时，两段长度不一样时才可比
    ra = a / max(now["bars"], 1)
    rb = b / max(prev["bars"], 1)
    if rb <= 0:
        return None
    if b < 1000:
        return None                    # 上一段本来就没什么爆仓，没有可比性
    drop = ra / rb
    if drop >= 0.5:
        return None
    # 上一段是靠爆空推的还是爆多砸的，决定了熄火意味着什么
    side = ""
    share = prev["short_liq"] / b * 100
    if share >= 70:
        side = f"（上一段 {share:.0f}% 是爆空，那波是轧空推上去的）"
    elif share <= 30:
        side = f"（上一段 {100 - share:.0f}% 是爆多，那波是多杀多砸下来的）"
    # 这一段一笔爆仓都没有是**很常见的**（冷门币、纯横盘），而不是异常。
    # 第一版直接算 1/drop，除零当场崩——测试抓到的。
    how = "**一笔都没有了**" if a <= 0 else f"塌了 {1 / drop:.0f} 倍"
    return (f"**爆仓引擎熄火了**{side}：这一段每小时爆仓 ${ra:,.0f}，"
            f"上一段 ${rb:,.0f}，{how}。"
            f"贴身的那批燃料已经烧完，现在两边都没有可点的东西")


# ── 持仓掉的那部分：自己跑的，还是被打爆的 ────────────────────
#
# 起因是另一份 BTR 分析里那句「24h 全网爆仓只有 $52 万，爆仓量很小，
# 说明大部分是主动平仓，不是被强平」。这个除法本身是对的——
# |ΔOI| 是"少了多少仓位"，爆仓额是"其中被强制平掉的"，同口径（都是名义金额）。
#
# 但去量了之后结论**和那句话相反**（`tools\\probe_forced.py`，
# 35 个币 / 30 天 / 1107 个 24h 减仓窗口）：
#
#     强平占比 < 5%（几乎全是主动平仓）   73.7%   ← 中位数只有 1.9%
#              5~20%                    19.5%
#             20~50%                     5.1%
#              ≥50%（以被强平为主）        1.7%
#
# **主动平仓才是减仓的常态**，四分之三的窗口都这样。所以"爆仓量小说明
# 是主动平仓"是在描述默认状态，不是洞察，每次都印等于每次都说废话。
#
# 真正有信息量的是**反过来**——强平占比高的那少数窗口跌得明显更狠：
#
#     强平占比 ≥20% 的窗口：价格中位 -2.77%（6h 尺度，只占 3~7%）
#     强平占比 <5%  的窗口：价格中位 -0.97%
#
# 所以这一栏做成**异常时才出声**：常态闭嘴，被强平主导时才报，并且带上
# "这种情况只占百分之几"，让人知道自己看见的是不是稀有事件。
FORCED_NOTABLE = 20.0     # 超过这个算"强平占相当一部分"（实测只占 6.8%）
FORCED_DOMINANT = 50.0    # 超过这个算"以被强平为主"（实测只占 1.7%）


def forced_share(s):
    """这段时间平掉的仓位里，被强平的占几成。→ % 或 None。

    分母用 `closed_usd`（逐根按当时价格累计的平仓名义额），
    不用首尾差——理由见 `seg_stats` 里那段注释：价格大幅波动时，
    首尾差会把"价格跌了"算成"有人平仓"，或者反过来。

    只在**净减仓**时有意义：加仓的窗口没什么可分的。
    减得太少也不算（分母小，比值全是噪声）。

    ⚠️ 分子分母都是 Gate 一家的（爆仓额只有它给逐根的），所以这是
    **Gate 内部的比例**，不是全网。跨源相除会得出一个谁都不是的数。
    """
    if not s or s["oi_first"] <= 0:
        return None
    if s["oi_last"] >= s["oi_first"] * 0.97:
        return None                    # 没怎么净减仓
    closed = s.get("closed_usd") or 0
    if closed <= 0:
        return None
    return (s["long_liq"] + s["short_liq"]) / closed * 100


def forced_words(pct):
    """**常态闭嘴。** 73.7% 的减仓窗口强平占比都在 5% 以下，
    每次都印一句"主要是主动平仓"等于每次都说废话。"""
    if pct is None or pct < FORCED_NOTABLE:
        return None
    if pct >= FORCED_DOMINANT:
        return (f"**这波减仓 {pct:.0f}% 是被强平的，不是自己走的** —— "
                f"实测这种窗口只占 1.7%，是强制去杠杆。"
                f"同类窗口价格中位 -2.8%，比主动平仓那类（-1.0%）狠得多")
    return (f"这波减仓里有 **{pct:.0f}% 是被强平的** —— 实测只占 6.8% 的窗口"
            f"到得了这个程度。强平主导的去杠杆通常更猛，"
            f"同类窗口价格中位 -2.8%（主动平仓那类 -1.0%）")


def who_left(s):
    """账户数原值告诉你**是谁在离场**，比值告诉不了。

    比值从 0.37 掉到 0.32 可能是多头跑了，也可能是空头进得更多——
    两个原值一摆出来就没有歧义了。这是 Gate 有而币安没有的那一栏
    （币安只给占比，反推不出人数）。
    """
    if not s:
        return None
    dl = s["long_users_last"] - s["long_users_first"]
    ds = s["short_users_last"] - s["short_users_first"]
    if abs(dl) < 5 and abs(ds) < 5:
        return None
    txt = (f"多头账户 {s['long_users_first']}→{s['long_users_last']}（{dl:+d}）　"
           f"空头账户 {s['short_users_first']}→{s['short_users_last']}（{ds:+d}）")
    if dl < 0 and ds < 0:
        faster = "多头" if abs(dl) > abs(ds) else "空头"
        txt += f"\n　└ 两边同时离场，{faster}跑得更快"
    elif dl > 0 and ds > 0:
        # 真机漏掉的分支：龙虾那波多头 +230、空头 +737，两边都在进场，
        # 结果这一行整个消失了。**四种组合要凑齐**，不然看的人以为没数据
        faster = "多头" if dl > ds else "空头"
        txt += f"\n　└ 两边都在进场（新人在涌入），{faster}进得更猛"
    elif dl > 0 and ds < 0:
        txt += "\n　└ 多头在进、空头在退"
    elif dl < 0 and ds > 0:
        txt += "\n　└ 多头在退、空头在进"
    return txt


def big_vs_small(s):
    """大户比稳、散户比乱动 = 大钱没走，走的是小账户。

    这是那份分析第 4 条的落点：账户多空比从 0.3705 掉到 0.3151（散户在跑），
    但大户持仓量多空比稳在 1.32~1.38 没动 —— 结论是"大户资金没走"。
    两个口径不同（金额 vs 人头），所以它们**可以同时成立且不矛盾**。
    """
    if not s or s["top_first"] <= 0 or s["retail_first"] <= 0:
        return None
    dt = abs(s["top_last"] - s["top_first"]) / s["top_first"] * 100
    dr = abs(s["retail_last"] - s["retail_first"]) / s["retail_first"] * 100
    if dt < 5 and dr >= MOVE:
        return (f"大户持仓比稳在 {min(s['top_first'], s['top_last']):.2f}~"
                f"{max(s['top_first'], s['top_last']):.2f} 没动，散户比却挪了 "
                f"{dr:.0f}% → **大户资金没走，动的是小账户**")
    if dt >= MOVE and dr < 5:
        return (f"散户没怎么动，大户比挪了 {dt:.0f}% → 大钱在调仓，"
                f"这一栏通常比散户那栏先动")
    return None


# ── 跨所持仓分歧：实测里区分度最强的一个 ─────────────────────
#
# 他问「关键数据还够不够详细」，我把六个候选维度全量过一遍
# （tools\\probe_gaps.py / probe_gaps2.py，异动组 vs 成交额基线组）：
#
#     跨所持仓分歧      20.3pt  vs   6.2pt  →  3.27x  ✅ 加
#     爆仓量30天分位    92.7%   vs  72.5%   →  1.28x  ⚠️ 当参照系用，不当判据
#     大单占比(相对)    28.7%   vs  49.7%   →  0.58x  ❌
#     主动买卖盘失衡     7.0    vs   9.2    →  0.76x  ❌
#     合约/现货倍数      5.6    vs   7.3    →  0.77x  ❌
#     现货主动买入占比   48.9%  vs  49.3%   →  0.99x  ❌
#
# **六个里四个是噪音**，包括本来最看好的主动买卖盘——它自己就贴着 1.0，
# ±80% 的币也只在 0.80~1.01 之间晃。加进去只会挤走真信号。
#
# 分歧这一条为什么强，看明细就懂：
#     龙虾  +81.7%   币安 +117.5%   Gate  +2.5%                  一家独走
#     BICO -14.2%   币安 -10.2%   Bybit +7.0%   Gate -28.3%     三家反向
# 龙虾那波涨 82%，**全是币安一家的杠杆在推**，Gate 那边根本没跟。
# 这和三家一起加仓是完全不同的东西：前者是单边杠杆事件，后者才是共识。
VENUE_SPREAD_HIGH = 20.0    # 极差超过这个数算"分歧显著"（基线中位数 6.2pt）
VENUE_SOLO = 50.0           # 超过这个数算"一家独走"
VENUE_CN = {"bn": "币安", "by": "Bybit", "gate": "Gate"}


async def venue_oi(symbol, hours=DEFAULT_HOURS):
    """三家各自的持仓变化率。→ {"bn":…, "by":…, "gate":…}，取不到的不放。

    **三家的持仓量单位和口径都不一样，所以只能比变化率不能比绝对值**
    （币安给美元名义值、Bybit 给张数、Gate 给张数）。
    """
    import httpx
    sym = symbol.upper().replace("USDT", "")
    inst = f"{sym}USDT"
    n = max(3, int(hours) + 1)
    out = {}
    async with httpx.AsyncClient(timeout=12) as c:
        async def _bn():
            d = await _bn_series(c, "openInterestHist", inst, n)
            if len(d) >= 3:
                a, b = _f(d[0]["sumOpenInterestValue"]), _f(d[-1]["sumOpenInterestValue"])
                if a > 0:
                    out["bn"] = (b - a) / a * 100

        async def _by():
            r = await c.get(f"{BYBIT}/v5/market/open-interest",
                            params={"category": "linear", "symbol": inst,
                                    "intervalTime": "1h", "limit": n})
            lst = ((r.json() or {}).get("result") or {}).get("list") or []
            if len(lst) >= 3:
                # Bybit 是**倒序**（最新在前）——和别家反着，别照抄索引
                b, a = _f(lst[0]["openInterest"]), _f(lst[-1]["openInterest"])
                if a > 0:
                    out["by"] = (b - a) / a * 100

        async def _gate():
            d = await _gate_stats(c, sym, n)
            if len(d) >= 3:
                a, b = _f(d[0]["open_interest"]), _f(d[-1]["open_interest"])
                if a > 0:
                    out["gate"] = (b - a) / a * 100

        await asyncio.gather(_bn(), _by(), _gate(), return_exceptions=True)
    return out


def venue_verdict(ch, hours=DEFAULT_HOURS):
    """三家在不在做同一件事。→ (一句话, 明细行) 或 (None, None)。

    少于两家就闭嘴：一家的数据谈不上"分歧"。
    """
    if not ch or len(ch) < 2:
        return None, None
    detail = "　".join(f"{VENUE_CN.get(k, k)} {v:+.0f}%"
                       for k, v in sorted(ch.items(), key=lambda x: -x[1]))
    hi_k = max(ch, key=ch.get)
    lo_k = min(ch, key=ch.get)
    spread = ch[hi_k] - ch[lo_k]
    if spread < VENUE_SPREAD_HIGH:
        return (f"三家持仓变化一致（极差 {spread:.0f} 个点）→ "
                f"不是某一家的局部行情"), detail
    ups = [k for k, v in ch.items() if v > 5]
    dns = [k for k, v in ch.items() if v < -5]
    if ups and dns:
        return (f"**三家方向都不一致**：{VENUE_CN.get(hi_k, hi_k)}在加仓、"
                f"{VENUE_CN.get(lo_k, lo_k)}在减仓（差 {spread:.0f} 个点）→ "
                f"不是同一批人在做同一件事，这种行情的持续性通常很差"), detail
    if spread >= VENUE_SOLO:
        other = "、".join(VENUE_CN.get(k, k) for k in ch if k != hi_k)
        return (f"**这波基本是 {VENUE_CN.get(hi_k, hi_k)} 一家的杠杆在推**"
                f"（{ch[hi_k]:+.0f}% vs {other} 最低 {ch[lo_k]:+.0f}%）→ "
                f"单边杠杆事件，比三家一起动脆得多"), detail
    return (f"{VENUE_CN.get(hi_k, hi_k)}那边动得明显更多"
            f"（差 {spread:.0f} 个点）→ 加仓主要发生在一家"), detail


async def liq_percentile(symbol, hours=DEFAULT_HOURS, days=30):
    """这一段的每小时爆仓量，在这个币过去 30 天里排第几分位。

    **「这段爆了 $11k」本身读不出多少**——对 BTC 是零头，对小币是天量。
    只有跟这个币自己的历史比才有意义。
    实测这一项区分度只有 1.28x，所以**它是参照系不是判据**：
    用来把一个没法读的美元数字翻译成"高/低"，不用来下结论。
    """
    import httpx
    sym = symbol.upper().replace("USDT", "")
    async with httpx.AsyncClient(timeout=15) as c:
        rows = await _gate_stats(c, sym, min(days * 24, GATE_MAX_BARS))
    h = max(2, int(hours))
    if len(rows) < h + 48:
        return None
    liq = [_f(x.get("long_liq_usd")) + _f(x.get("short_liq_usd")) for x in rows]
    recent = sum(liq[-h:]) / h
    hist = sorted(liq[:-h])
    if not hist:
        return None
    return {"pct": sum(1 for v in hist if v < recent) / len(hist) * 100,
            "rate": recent, "days": len(rows) / 24}


def liq_words(p):
    if not p:
        return None
    v = p["pct"]
    if v >= 95:
        w = "**过去一个月里最高的那 5%**——现在正在大规模强平"
    elif v >= 80:
        w = "明显高于这个币的日常水平"
    elif v <= 20:
        w = "低于日常水平，两边都没什么仓位在被打掉"
    elif v <= 5:
        w = "**几乎停了**，这个币最近一个月里最安静的 5%"
    else:
        w = "跟这个币的日常水平差不多"
    return (f"爆仓强度在自己 {p['days']:.0f} 天历史里排 **{v:.0f} 分位**（{w}）")


# ── 给币种卡片用的一行爆仓摘要 ────────────────────────────────
# 发个币名出来的那张卡是他最常用的路径，但上面一直没有爆仓数据。
# 卡片有 24 行的硬预算（超了按钮会被挤出屏幕），所以这里**最多两行**：
# 一行数字、一行解读，解读只在有话可说时才出现。
async def liq_line(symbol, hours=24):
    """→ [一行或两行] 或 []。取不到就整段不出现，不占位不报错。"""
    import httpx
    sym = symbol.upper().replace("USDT", "")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            rows = await _gate_stats(c, sym, hours + 1)
    except Exception as e:
        log.info(f"[posflow] {sym} 卡片爆仓行取数失败: {e}")
        return []
    if len(rows) < 3:
        return []
    lg = sum(_f(x.get("long_liq_usd")) for x in rows)
    sh = sum(_f(x.get("short_liq_usd")) for x in rows)
    if lg + sh <= 0:
        return [f"爆仓({hours}h): 两边都是 0——这个币没人被强平"]
    out = [f"爆仓({hours}h): 多 {_money(lg)} / 空 {_money(sh)}"]
    tot = lg + sh
    # 一边压倒性地多才说话。五五开时说"多头略多"是把噪音当结论。
    if lg >= tot * 0.8:
        out.append("　└ 几乎全是多头被打——价格被砸穿的那一侧是多头")
    elif sh >= tot * 0.8:
        out.append("　└ 几乎全是空头被打——价格被轧上去的那一侧是空头")
    return out


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
# 窗口必须能改。**固定 24 小时会把两段行情糊成一段**：他那份 BTR 分析里
# 13 小时横盘的前面是 47 小时单边扩张，按 24 小时切的话两段各切一半，
# 得出的是一个哪一段都不像的平均数。
WINDOWS = (6, 13, 24, 48)


def kb(sym, hours=DEFAULT_HOURS):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if h == hours else ''}{h}h",
                              callback_data=f"pf:h{h}:{sym}") for h in WINDOWS],
        [InlineKeyboardButton("💣 清算地图", callback_data=f"lq:w:{sym}:7日"),
         InlineKeyboardButton("🔄 刷新", callback_data=f"pf:r:{sym}")],
        [InlineKeyboardButton("ℹ️ 口径", callback_data=f"pf:i:{sym}"),
         InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")],
    ])


# ── 转向清单：接下来要看到什么，才算真的转了 ──────────────────
#
# 卡片现在能说清"发生了什么"，但说不清"**接下来看什么才算翻转**"。
# 后者才是能拿来盯盘的东西——不然每隔一小时就得把整张卡重读一遍，
# 自己在脑子里比对哪几项变了。
#
# 四个条件取自那套「新资金重新开多」的判据，每一项这里都已经有数据：
#     价格 ↑ + 持仓 ↑ + 爆仓减少 + 资金费率回到正常区间
# 逐项打勾，并且**报"满足几项"**——差一项和差三项完全是两回事，
# 只给一句"尚未确认"等于没说。
FUND_NORMAL_APR = 30.0     # 资金费年化在这个数以内算"回到正常"


def flip_checklist(now, prev, chg=None, apr=None):
    """→ (标题, [逐条], 满足数, 总数)。数据不够就返回 None。

    方向按当前处境定：正在跌就列**看涨确认**，正在涨就列**见顶确认**。
    两边列同一套条件是错的——涨势里"持仓增加"是延续不是反转。
    """
    if not now or chg is None:
        return None
    down = chg < 0
    liq_now = (now["long_liq"] + now["short_liq"]) / max(now["bars"], 1)
    liq_prev = ((prev["long_liq"] + prev["short_liq"]) / max(prev["bars"], 1)
                if prev else None)
    items = []
    if down:
        title = "🟢 *看涨确认*（要新资金真的进来，这四项得凑齐）"
        items.append(("价格企稳转涨", chg > 0,
                      f"现在 {chg:+.1f}%"))
        items.append(("持仓量重新增加", now["oi_pct"] > 0,
                      f"现在 {now['oi_pct']:+.1f}%"))
    else:
        title = "🔴 *见顶确认*（这四项凑齐说明是轧空不是真涨）"
        items.append(("价格涨不动了", chg < 2,
                      f"现在 {chg:+.1f}%"))
        items.append(("持仓量不再增加", now["oi_pct"] <= 0,
                      f"现在 {now['oi_pct']:+.1f}%"))
    if liq_prev is not None and liq_prev > 0:
        ok = liq_now < liq_prev * 0.5
        items.append(("爆仓平息下来", ok,
                      f"每小时 {_money(liq_now)}，上一段 {_money(liq_prev)}"))
    else:
        items.append(("爆仓平息下来", None, "上一段没有爆仓，比不了"))
    if apr is None:
        items.append(("资金费率回到正常", None, "取不到"))
    else:
        items.append(("资金费率回到正常", abs(apr) <= FUND_NORMAL_APR,
                      f"年化 {apr:+.0f}%（±{FUND_NORMAL_APR:.0f}% 以内算正常）"))
    hit = sum(1 for _n, ok, _d in items if ok is True)
    total = sum(1 for _n, ok, _d in items if ok is not None)
    return title, items, hit, total


def checklist_lines(got):
    """**汇总并进标题，不单独占一行。** 整张卡有 24 行的预算
    （超了按钮会被挤出屏幕），实测加上清单正好卡在 25 行。"""
    if not got:
        return []
    title, items, hit, total = got
    tail = ("　**齐了**" if total and hit == total
            else f"　**{hit}/{total}，还差 {total - hit} 项**" if total else "")
    out = [title + tail]
    for name, ok, detail in items:
        mark = "✅" if ok is True else ("⬜" if ok is False else "❔")
        out.append(f"　{mark} {name}　{detail}")
    return out


def _liq_pair(s):
    return f"多 ${s['long_liq']:,.0f} / 空 ${s['short_liq']:,.0f}"


def gate_lines(g, chg=None, venues=None, liqp=None, apr=None):
    """Gate 那套的正文。**结论在最上面，数字在下面**——
    他定过的列表版式：结论一行，细节收在后面。"""
    now, prev = g["now"], g["prev"]
    h, ph = g["hours"], g["prev_hours"]
    out = []

    # ① 横盘还是趋势。这条要排第一：后面所有解读都建立在它上面。
    #
    # ⚠️ **横盘不能只看主源那一家**。真机撞到的：龙虾涨了 79%，Gate 那边持仓
    # 只动了 2.3%，于是卡片印出「横盘，没有一方在净建仓」——而同一时间
    # 币安持仓 +120%。仓位确实在净增，只是增在另一家。
    # 一家的横盘 + 另一家的暴增 = 那波行情根本不在这家发生，这比"横盘"
    # 有信息量得多，也是接了跨所数据之后才看得见的。
    other = None
    if venues:
        big = {k: v for k, v in venues.items() if abs(v) >= VENUE_SPREAD_HIGH}
        if big:
            k = max(big, key=lambda x: abs(big[x]))
            other = (VENUE_CN.get(k, k), big[k])
    flat = is_flat(now) and not other
    if is_flat(now) and other:
        out.append(f"→ **Gate 这边是横盘（振幅 {now['amp']:.1f}%），"
                   f"但 {other[0]} 那边持仓 {other[1]:+.0f}%** → "
                   f"这波不在 Gate 发生，仓位是在另一家净增的")
    elif flat:
        out.append(f"→ **这 {h} 小时是横盘，不是趋势**：持仓振幅只有 "
                   f"{now['amp']:.1f}%，{h} 小时里上升 {now['up']} 次、"
                   f"下降 {now['dn']} 次，几乎对半——**没有一方在净建仓**")
        if prev and abs(prev["oi_pct"]) > 50:
            out.append(f"　└ 而前 {ph} 小时持仓 {prev['oi_pct']:+.0f}%，"
                       f"是完全不同的状态（那一段有人在单边扩张）")
    else:
        for v in verdict(chg, now["oi_pct"],
                         _delta(now["top_first"], now["top_last"]),
                         _delta(now["retail_first"], now["retail_last"])):
            out.append(f"→ {v}")

    # ② 爆仓引擎还着着没有。**只能靠两段对比**，单看这一段看不出来
    sq = squeeze_verdict(now, prev)
    if sq:
        out.append(f"→ {sq}")

    # ③ 跨所分歧。实测区分度 3.27x，是量过的候选里最强的一个，
    #    而且和持仓量本身正交——三家一起加仓 vs 一家独走是两种行情
    vv, vdetail = venue_verdict(venues, h)
    if vv:
        out.append(f"→ {vv}")

    # ④ 减掉的仓是自己走的还是被打爆的。**只在异常时出声**——
    #    实测 73.7% 的减仓窗口强平占比都在 5% 以下，主动平仓才是常态
    fw = forced_words(forced_share(now))
    if fw:
        out.append(f"→ {fw}")

    # ⑤ 大户 vs 散户：两个口径可以同时成立且不矛盾
    bs = big_vs_small(now)
    if bs:
        out.append(f"→ {bs}")

    out.append("")
    # ⚠️ 这个数是**主源（Gate）一家的**，不是全网。紧挨着下面"三家各自"那行印，
    # 不标出处会被读成合计——而 Gate 在多数币上只占全网一到两成。
    out.append(f"持仓量 {_money(now['oi_usd'])} U · 仅 Gate"
               f"（{h}h {now['oi_pct']:+.1f}%，振幅 {now['amp']:.1f}%，"
               f"升 {now['up']} 次/降 {now['dn']} 次）")
    if vdetail:
        out.append(f"　三家各自的变化：{vdetail}")
    out.append(f"爆仓　{_liq_pair(now)}")
    if prev:
        out.append(f"　└ 前 {ph} 小时是 {_liq_pair(prev)}")
    lw = liq_words(liqp)
    if lw:
        out.append(f"　└ {lw}")
    out.append(f"大户持仓比 {now['top_last']:.2f}"
               f"（{now['top_first']:.2f} → {now['top_last']:.2f}）")
    out.append(f"人数多空比 {now['retail_last']:.4f}"
               f"（{now['retail_first']:.4f} → {now['retail_last']:.4f}）")
    wl = who_left(now)
    if wl:
        out.append(f"　{wl}")
    if abs(now["funding"]) > 0.0001:
        out.append(f"资金费率 {now['funding']:+.4f}%")

    # 最后放「接下来看什么」。前面全是"发生了什么"，这一段是唯一能拿去
    # 盯盘的——不然每小时得把整张卡重读一遍，自己在脑子里比对哪几项变了。
    ck = checklist_lines(flip_checklist(now, prev, chg, apr))
    if ck:
        out.append("")
        out.extend(ck)
    return out


def _money(x):
    from handlers.liqmap import _money as m
    return m(x)


async def build_text(sym, hours=DEFAULT_HOURS):
    """Gate 有就走 Gate（多了爆仓量、账户数原值、两段对比三样），
    没有才退回币安那套。"""
    chg = None
    try:
        g = await fetch_segments(sym, hours)
    except Exception as e:
        log.info(f"[posflow] {sym} Gate 分段取数失败: {e}")
        g = None
    if g and g.get("now"):
        # 三件事互不依赖，一起打。任何一件失败都只是少一行，不影响其余
        f, venues, liqp = await asyncio.gather(
            fetch(sym, hours), venue_oi(sym, hours), liq_percentile(sym, hours),
            return_exceptions=True)
        chg = (f or {}).get("chg") if isinstance(f, dict) else None
        if isinstance(venues, Exception):
            log.info(f"[posflow] {sym} 跨所持仓取数失败: {venues}")
            venues = None
        if isinstance(liqp, Exception):
            log.info(f"[posflow] {sym} 爆仓分位取数失败: {liqp}")
            liqp = None
        # 转向清单要用资金费年化：费率是不是"回到正常"是四项之一
        apr = f.get("funding_apr") if isinstance(f, dict) else None
        head = f"📊 *{escape_md(sym)} 持仓结构*　近 {hours} 小时"
        if chg is not None:
            head += f"　价格 {chg:+.1f}%"
        tail = (f"\n数据源 Gate（爆仓量和账户数只有它给逐小时的）"
                f"｜对比段取前 {g['prev_hours']} 小时"
                f"｜⚠️ 结构分析不构成投资建议")
        return (head + "\n\n"
                + "\n".join(gate_lines(g, chg, venues, liqp, apr)) + "\n" + tail)

    f = await fetch(sym, hours)
    if not f:
        return (f"📊 *{escape_md(sym)} 持仓结构*\n\n"
                f"Gate、币安、Bybit 都没有 {escape_md(sym)} 的永续合约数据。\n"
                f"三家的覆盖不一样（Gate 942 个、币安 703 个，各有独占），"
                f"都没有就是真没有——不是出错。")
    chg = f.get("chg")
    head = f"📊 *{escape_md(sym)} 持仓结构*　近 {hours} 小时"
    if chg is not None:
        head += f"　价格 {chg:+.1f}%"
    body = lines(f, chg)
    tail = ("\n数据源币安（Gate 没有这个币，所以**没有爆仓量和账户数原值**）"
            "｜变化门槛 10%｜⚠️ 结构分析不构成投资建议")
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
        "*两段对比：为什么非有不可*",
        "「爆仓引擎熄火了」这种结论**单看当前那一段是看不出来的**：",
        "$12k 的爆空量本身既不高也不低，只有和上一段一比才知道是「熄火」",
        "还是「从来就没着过」。所以每张卡都会把上一段行情摆出来。",
        "",
        "对比段**刻意不等长**（默认取前面 3 倍长度）：13 小时横盘的前面",
        "可能是 47 小时单边扩张，等长切的话那 47 小时被砍成 13 小时，",
        "「持仓 +450%」这个量级就没了。",
        "",
        "窗口用 `/pos BTR 13` 指定，或点卡片上的 6h/13h/24h/48h。",
        "**固定 24 小时会把两段行情糊成一段**，得出一个哪段都不像的平均数。",
        "",
        "*横盘怎么判*",
        "只看首尾差会把来回震荡读成「没动」。真正的判据是",
        "**振幅小 且 涨跌次数接近对半**（= 没有一方在净建仓）。",
        f"振幅门槛 {FLAT_AMP:g}%，来自实测基线：平静的大盘币 24 小时",
        "|ΔOI| 中位数 6.6%、75 分位 9.4%。",
        "",
        "*跨所分歧：量过的候选里最强的一个*",
        "三家的持仓变化率一起看。实测异动组极差 20.3 个点、平静基线 6.2 个点",
        "（3.27 倍），而且和持仓量本身**正交**——三家一起加仓和一家独走",
        "是两种完全不同的行情：",
        "　龙虾 +81.7%　币安持仓 +117.5%　Gate +2.5%　→ 单边杠杆事件",
        "　BICO -14.2%　币安 -10.2%　Bybit +7.0%　Gate -28.3%　→ 三家反向",
        "只能比**变化率**不能比绝对值：三家的持仓单位不一样",
        "（币安给美元名义值，Bybit 和 Gate 给张数）。",
        "",
        "*刻意没加的四样*",
        "同一套判据量下来，这四个在异动组和基线组之间分不开，加了只会",
        "挤走真信号：",
        "```",
        "  主动买卖盘失衡     7.0  vs  9.2   0.76x",
        "  合约/现货倍数      5.6  vs  7.3   0.77x",
        "  现货主动买入占比  48.9% vs 49.3%  0.99x",
        "  大单占比(相对口径)28.7% vs 49.7%  0.58x",
        "```",
        "最意外的是主动买卖盘——它自己就贴着 1.0，涨跌 ±80% 的币也只在",
        "0.80~1.01 之间晃，因为每一笔成交都同时有主动买和主动卖。",
        "",
        "*爆仓量分位是参照系，不是判据*",
        "「这段爆了 $11k」本身读不出多少——对 BTC 是零头，对小币是天量。",
        "所以换算成「在这个币自己 30 天历史里排第几分位」。",
        "但它区分度只有 1.28 倍，所以只用来翻译数字，不用来下结论。",
        "",
        "*覆盖范围*",
        "主源是 **Gate**（942 个 USDT 永续，比币安的 703 个多，各有独占）。",
        "选它是因为它一个接口给六样，其中两样别家没有：",
        "　· **爆仓金额分多空、逐小时** —— 币安的 allForceOrders 已经 404",
        "　· **账户数原值**（837→680）—— 币安只给占比，反推不出人数",
        "Gate 没有的币退回币安，那时**没有爆仓量和账户数**，卡片底部会写明。",
        "两家都没有就整段不出现——不编数据。",
        "",
        "⚠️ 结构分析不构成投资建议",
    ])


def parse_hours(args):
    """`/pos BTR 13` 的第二个参数。看不懂就退回默认，别报错。"""
    for a in (args or [])[1:]:
        try:
            v = int(str(a).lower().rstrip("h小时"))
        except (TypeError, ValueError):
            continue
        if 2 <= v <= 720:
            return v
    return DEFAULT_HOURS


async def pos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pos <币> [小时] —— 这波是谁推的：大户加仓还是散户接盘。"""
    if not context.args:
        await safe_reply(update.message,
                         "用法：`/pos BTC`　或 `/pos BTR 13`（自己指定几小时）\n\n"
                         "看这一段里持仓量、爆仓量、大户仓位、散户人数各挪了多少，"
                         "判「新资金进场」「轧空推的」还是「横盘没人建仓」。\n"
                         "**并且拿上一段行情做对比**——「爆仓引擎熄火了」这种话"
                         "只能从两段一比里看出来。", parse_mode="Markdown")
        return
    sym = str(context.args[0]).upper().replace("USDT", "")
    hours = parse_hours(context.args)
    msg = await safe_reply(update.message, f"📊 拉 {sym} 近 {hours} 小时的持仓结构…")
    try:
        text = await build_text(sym, hours)
    except Exception as e:
        log.error(f"/pos {sym} 出错: {e}", exc_info=True)
        await safe_reply(update.message, f"取数失败，稍后再试：{str(e)[:80]}")
        return
    if msg:
        try:
            await msg.edit_text(text, parse_mode="Markdown",
                                reply_markup=kb(sym, hours))
            return
        except Exception:
            pass
    await safe_reply(update.message, text, parse_mode="Markdown",
                     reply_markup=kb(sym, hours))


async def from_btn(query, context, sym, detail=False, hours=DEFAULT_HOURS):
    if detail:
        await safe_edit(query, detail_text(sym), parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            "⬅️ 回结构卡", callback_data=f"pf:r:{sym}")]]))
        return
    await safe_edit(query, f"📊 拉 {sym} 近 {hours} 小时的持仓结构…")
    try:
        text = await build_text(sym, hours)
    except Exception as e:
        log.error(f"持仓结构按钮出错 {sym}: {e}", exc_info=True)
        await safe_edit(query, f"取数失败：{str(e)[:80]}",
                        reply_markup=kb(sym, hours))
        return
    await safe_edit(query, text, parse_mode="Markdown",
                    reply_markup=kb(sym, hours))
