"""清算地图 /liqmap —— 估算各个价位上堆了多少待强平的仓位，画成图。

## ⚠️ 先说清楚这是估算，不是交易所数据

CoinGlass 那张「清算地图」也不是原始数据——**没有任何交易所公布
「某个价位挂着多少待爆仓的仓位」**，那是每个账户的私有信息。
探针实测（`tools\\probe_liqmap.py`）：
  · 币安 `allForceOrders`（历史强平单）已经 404，公开拿不到了
  · CoinGlass 的 heatmap 接口不在免费档
  · 能白拿的只有 `openInterestHist`（持仓量历史）和 K 线

所以这张图和 CoinGlass 一样是**推算**出来的，口径必须写在脸上，
否则一张看起来很权威的图会被当成事实去下单。

## 推算模型

对每根 K 线：这根期间**新增**的持仓量（ΔOI，金额口径），就当成是在这根的
典型价 P 上建的仓。永续里每一份持仓同时是一个多头和一个空头
（多空持仓量恒等），所以同一份 ΔOI 会同时产生：

    多头爆仓位 = P × (1 − 1/杠杆)      （在 P 下方）
    空头爆仓位 = P × (1 + 1/杠杆)      （在 P 上方）

按几个杠杆档位分配权重，落进价格桶里累加，就是那些柱子。
最后只保留**还没被扫过**的一侧：现价下方的多头爆仓位、上方的空头爆仓位——
已经越过的价位，那些仓早就被平掉了，留着是假的。

**ΔOI 为负（减仓）不计**：那是平仓，不产生新的爆仓位。

杠杆权重是假设，不是实测（交易所不公布每个人开了几倍）。
默认按散户偏高杠杆给，具体数字印在 ℹ️ 卡片上——**假设必须可见**，
否则读图的人不知道柱子高矮有一半是我拍的。

真实爆仓价还要算维持保证金率（`leverageBracket` 要 API key，401），
这里没算，所以估出来的位置比真实爆仓价**略远一点**。
"""
import io
import logging
import time

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import matplotlib
matplotlib.use("Agg")          # 无界面后端，服务器上必须（和 chart.py 同一条规矩）
import matplotlib.pyplot as plt

from handlers.util import safe_reply, safe_edit
from handlers import busy, guided

log = logging.getLogger(__name__)

BN = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"

# 杠杆档位和假设权重。和 CoinGlass 那张图用同一批档位，方便对照。
# 权重是**假设**：交易所不公布谁开了几倍。散户在小市值币上普遍偏高杠杆，
# 所以往高倍数偏。改这里等于改整张图的形状，改了要同步改 ℹ️ 卡片上的说明。
LEVS = ((5, 0.12, "#2E9BE6"), (10, 0.20, "#22C3B6"),
        (25, 0.30, "#F5A524"), (50, 0.38, "#F0642B"))

WINDOWS = {                    # 标签 -> (openInterestHist 的 period, 取几根)
    "1日": ("15m", 96),
    "7日": ("1h", 168),
    "30日": ("4h", 180),
    # 长窗口只有 Bybit 给得出：币安 openInterestHist 只保留 30 天，
    # 换 period、把 limit 提到 500 都一样。
    #
    # ⚠️ 更正一次判断：我先前认定"一年做不到"，理由是 Bybit 单次硬卡 200 根。
    # 那只对了一半——**单次是 200 根，但换个 startTime/endTime 能拿到更老的**，
    # 实测 500~700 天前照样有数据。所以分段拉就能到一年，见 `_bybit_paged`。
    # 教训：接口"单次上限"不等于"历史上限"，下结论前先试一次翻页。
    "90日": ("1d", 90),
    "180日": ("1d", 180),
    "1年": ("1d", 365),
}
# 这些窗口只有 Bybit 有数据，且颗粒粗到一根一天——结论要打折看，见 _long_caveat()
LONG_WINDOWS = {"90日", "180日", "1年"}
BYBIT_PAGE = 200               # Bybit 单次返回上限，超过要分段拉
DEFAULT_WIN = "7日"
BUCKETS = 60                   # 价格分多少个桶
SPAN = 0.30                    # 只画现价 ±30% 的范围，再远的位置没有参考意义
CACHE_TTL = 300

_cache = {}                    # (sym, win) -> {"ts", "data"}


# ── 取数 ────────────────────────────────────────────────────
# Bybit 的持仓量周期名和币安不一样，各写各的
# period -> (Bybit 持仓量接口的 intervalTime, K线接口的 interval)
# 两个接口的粒度写法不一样（一个 "1d"、一个 "D"），映射错了不会报错，
# 只会拿到另一个周期的 K 线去对齐 OI，图安静地画歪。
BYBIT_IV = {"15m": ("15min", "15"), "1h": ("1h", "60"), "4h": ("4h", "240"),
            "1d": ("1d", "D")}


async def _fetch_binance(c, inst, period, limit):
    oi = await c.get(f"{BN}/futures/data/openInterestHist",
                     params={"symbol": inst, "period": period, "limit": limit})
    if oi.status_code != 200:
        return None
    oi_rows = oi.json()
    if not oi_rows:
        return None                      # 币安没这个币，交给 Bybit
    kl = await c.get(f"{BN}/fapi/v1/klines",
                     params={"symbol": inst, "interval": period, "limit": limit})
    kl_rows = kl.json()
    tk = await c.get(f"{BN}/fapi/v1/ticker/price", params={"symbol": inst})
    j = tk.json()
    # ⚠️ 这里以前直接 j["price"]。币安对不存在的币回 400 + {"code":-1121,...}，
    # 于是抛出一个光秃秃的 KeyError('price')，用户看到「画不出来：'price'」
    # ——**报错必须说人话**，不能把内部异常原样甩出去
    if not isinstance(j, dict) or "price" not in j:
        return None
    if not isinstance(kl_rows, list) or len(kl_rows) < 3:
        return None
    return oi_rows, kl_rows, float(j["price"]), "币安"


_PERIOD_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


async def _bybit_paged(c, kind, inst, iv, want, period):
    """从 Bybit 拉 want 根，超过单次上限就**往回翻页**。

    先前我以为「单次 200 根」就是历史上限，于是判定一年做不到。
    实测不是：换个 startTime/endTime 能拿到 500~700 天前的数据。
    **接口的单次上限 ≠ 历史上限**，下结论前先试一次翻页。

    kind: "oi" 持仓量 / "kl" K线。两个接口的参数名不一样（startTime/endTime
    vs start/end），也是那种写错不报错、只会安静少数据的地方。
    返回按**时间正序**的列表；某一段拿不到就停（多半是这个币上市时间不够）。
    """
    step = _PERIOD_MS.get(period, 86_400_000)
    end = int(time.time() * 1000)
    out, seen = [], set()
    while len(out) < want:
        n = min(BYBIT_PAGE, want - len(out))
        start = end - n * step
        if kind == "oi":
            r = await c.get(f"{BYBIT}/v5/market/open-interest", params={
                "category": "linear", "symbol": inst, "intervalTime": iv,
                "limit": BYBIT_PAGE, "startTime": start, "endTime": end})
            d = r.json()
            page = (d.get("result") or {}).get("list") or []
            if d.get("retCode") != 0:
                break
            key = lambda x: int(x["timestamp"])          # noqa: E731
        else:
            r = await c.get(f"{BYBIT}/v5/market/kline", params={
                "category": "linear", "symbol": inst, "interval": iv,
                "limit": BYBIT_PAGE, "start": start, "end": end})
            d = r.json()
            page = (d.get("result") or {}).get("list") or []
            if d.get("retCode") != 0:
                break
            key = lambda x: int(x[0])                    # noqa: E731
        fresh = [x for x in page if key(x) not in seen]
        if not fresh:
            break            # 这个币的历史到头了（上市时间不够），有多少用多少
        seen.update(key(x) for x in fresh)
        out.extend(fresh)
        end = min(key(x) for x in fresh) - 1
    out.sort(key=key)
    return out


async def _fetch_bybit(c, inst, period, limit):
    """Bybit 兜底。告警是全交易所的，只认币安等于一半的币点了没图。

    Bybit 的持仓量是**币的个数**，不像币安直接给金额——要自己乘当时的价格
    换成名义金额，否则和币安那条路口径不一致，图的量级会差几个数量级。
    """
    iv = BYBIT_IV.get(period)
    if not iv:
        return None
    oi_iv, kl_iv = iv
    rows = await _bybit_paged(c, "oi", inst, oi_iv, limit, period)
    if not rows:
        return None
    kl_rows = await _bybit_paged(c, "kl", inst, kl_iv, limit, period)
    if len(kl_rows) < 3:
        return None
    closes = {}
    for row in kl_rows:
        try:
            closes[int(row[0])] = float(row[4])
        except (TypeError, ValueError, IndexError):
            continue
    t = await c.get(f"{BYBIT}/v5/market/tickers",
                    params={"category": "linear", "symbol": inst})
    tl = (t.json().get("result") or {}).get("list") or []
    if not tl:
        return None
    last = float(tl[0]["lastPrice"])
    # 归一成币安那套结构，build_map 一行都不用改。
    # ⚠️ 顺序：Bybit 原始返回是新→旧，而 build_map 要旧→新（不然 ΔOI 正负全反、
    # "加仓"被当成"减仓"，整张图直接空掉）。
    # **翻转现在在 `_bybit_paged` 里统一做了**（它按时间正序返回），
    # 所以这里**不能再 reversed 一次**——翻两次等于没翻。
    out = []
    for row in rows:
        try:
            ts = int(row["timestamp"])
            oi_coins = float(row["openInterest"])
        except (KeyError, TypeError, ValueError):
            continue
        px = closes.get(ts) or last
        out.append({"sumOpenInterestValue": str(oi_coins * px), "timestamp": ts})
    if len(out) < 3:
        return None
    return out, kl_rows, last, "Bybit"


async def _fetch(symbol, win):
    """先币安后 Bybit。两家都没有才报错，且要说清是哪一步没有。

    ⚠️ 长窗口（90/180 日）**跳过币安直接走 Bybit**：币安的 openInterestHist
    只保留 30 天，拿它取 90 天会安静地只回 30 天的数据，画出来的图看着正常、
    实际窗口对不上标题——那比画不出来更糟。
    """
    period, limit = WINDOWS[win]
    inst = symbol.upper()
    if not inst.endswith("USDT"):
        inst += "USDT"
    async with httpx.AsyncClient(timeout=20) as c:
        got = None
        if win not in LONG_WINDOWS:
            try:
                got = await _fetch_binance(c, inst, period, limit)
            except Exception as e:
                log.debug(f"清算地图取币安失败 {inst}: {e}")
        if got is None:
            try:
                got = await _fetch_bybit(c, inst, period, limit)
            except Exception as e:
                log.debug(f"清算地图取 Bybit 失败 {inst}: {e}")
    if got is None:
        raise RuntimeError(
            f"{symbol} 画不出来：币安和 Bybit 的永续上都取不到它的持仓量历史。\n"
            f"（清算地图靠持仓量推算，只有这两家提供。"
            f"OKX/Gate 独有的币、或刚上市不久的币会取不到）")
    oi_rows, kl_rows, last, src = got
    # src 以前在这儿被丢掉，于是标题永远写「币安永续」——而 90/180 日的数据
    # 其实来自 Bybit。口径写错比不写更糟：一张 Bybit 的图挂着币安的抬头，
    # 没人看得出来。
    days = None
    try:
        ts = [int(r["timestamp"]) for r in oi_rows]
        days = (max(ts) - min(ts)) / 86_400_000
    except Exception:
        pass
    return oi_rows, kl_rows, last, inst, src, days


# ── 推算 ────────────────────────────────────────────────────
def build_map(oi_rows, kl_rows, last, levs=None):
    """→ {"lo","hi","edges","longs","shorts","added","bucket"}。

    longs/shorts: 每个杠杆档位一条 list，长度 = BUCKETS，值是估算的名义金额。
    """
    # K 线按时间对齐 OI（两个接口的 period 一样，条数可能差一两根）
    kmap = {int(k[0]): k for k in kl_rows}
    lo, hi = last * (1 - SPAN), last * (1 + SPAN)
    width = (hi - lo) / BUCKETS
    # levs 可以传别的档位（比如「按 5x/10x/20x 分档」那份明细），
    # 传 None 就用图上那套 5/10/25/50——**不要为了出明细去改 LEVS**，
    # 那会让所有人已经在看的那张图整个变形。
    levs = levs or LEVS
    longs = {L: [0.0] * BUCKETS for L, _w, _c in levs}
    shorts = {L: [0.0] * BUCKETS for L, _w, _c in levs}
    added_total = 0.0

    def put(book, price, amount):
        if price < lo or price >= hi:
            return          # 越出画布范围的位置不画，也不算进强度
        book[int((price - lo) / width)] += amount

    prev = None
    for row in oi_rows:
        try:
            val = float(row["sumOpenInterestValue"])
            ts = int(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if prev is not None:
            delta = val - prev
            k = kmap.get(ts)
            if delta > 0 and k:
                # 典型价：这根期间新开的仓大致建在这个价位上
                try:
                    p = (float(k[2]) + float(k[3]) + float(k[4])) / 3
                except (TypeError, ValueError, IndexError):
                    p = None
                if p and p > 0:
                    added_total += delta
                    for L, w, _c in levs:
                        amt = delta * w
                        # 永续里每份持仓同时是一多一空，所以两侧都要放
                        put(longs[L], p * (1 - 1 / L), amt)
                        put(shorts[L], p * (1 + 1 / L), amt)
        prev = val

    # 已经被价格越过的一侧要抹掉：那些仓早就被强平或平掉了，留着是假的
    cur_b = int((last - lo) / width)
    for L, _w, _c in levs:
        for i in range(BUCKETS):
            if i >= cur_b:
                longs[L][i] = 0.0      # 多头爆仓位只可能在现价下方
            if i <= cur_b:
                shorts[L][i] = 0.0
    edges = [lo + width * i for i in range(BUCKETS)]
    return {"lo": lo, "hi": hi, "edges": edges, "width": width,
            "longs": longs, "shorts": shorts, "added": added_total,
            "cur_bucket": cur_b, "levs": levs}


# ── 按杠杆分档的明细 ────────────────────────────────────────
# 他要的口径（2026-08-27）：「按 5x/10x/20x 分档，反推多空两侧仍未被触发的
# 清算价位与金额，仅列出 ≥10 万美元的清算簇」。
#
# **刻意不改上面那套 LEVS**：图上是 5/10/25/50，改它等于让所有人已经在看的
# 那张图整个变形。这里用同一批 ΔOI 数据、按他的档位单独算一份，两者并存。
TIER_LEVS = ((5, 0.20, "#2E9BE6"), (10, 0.35, "#22C3B6"), (20, 0.45, "#F0642B"))
TIER_FLOOR = 100_000           # 小于这个数的簇不列——噪音，列出来只会淹掉真的
# 一个桶要达到该档峰值的多少才算进簇。低于它的当背景，
# 否则相邻非零会全连成一条横跨整个价格区间的假"簇"。
DENSITY_FRAC = 0.30


def clusters(m, side, lev, floor=TIER_FLOOR):
    """某个杠杆档位、某一侧，**仍未被触发**的清算簇。

    「簇」= 相邻价格桶合并成一段。不合并的话，一个宽区间会被切成几十根
    小柱子，每根都不到门槛，于是**明明有一大堆待爆仓位却一条都列不出来**。

    未被触发这件事 `build_map` 已经处理了：现价越过的一侧整段抹零
    （那些仓早被平了）。所以这里非零的就是还没被扫到的。
    """
    book = (m["longs"] if side == "long" else m["shorts"]).get(lev)
    if not book:
        return []
    # **只把"够密"的桶算进簇。** 按「非零就合并」写的第一版，在 7 日 168 根
    # 数据下几乎每个桶都有值，相邻非零全连成一条横跨 8000 点的"簇"——
    # 那不是密集区，那是整个价格区间。
    # 以峰值的 DENSITY_FRAC 为界，低于它的当背景噪音，簇才切得开。
    peak = max(book) if book else 0
    cut = peak * DENSITY_FRAC
    out, run = [], None
    for i, v in enumerate(book):
        if v > cut:
            if run is None:
                run = {"lo": m["edges"][i], "hi": m["edges"][i] + m["width"],
                       "amount": 0.0}
            run["hi"] = m["edges"][i] + m["width"]
            run["amount"] += v
        elif run is not None:
            out.append(run)
            run = None
    if run is not None:
        out.append(run)
    return sorted([z for z in out if z["amount"] >= floor],
                  key=lambda z: -z["amount"])


def tier_report(m, sym, win, last, floor=TIER_FLOOR):
    """按杠杆分档列出两侧未触发的清算簇。返回 Markdown 文本。"""
    lines = [f"🧮 *{sym} 未触发清算簇 · 按杠杆分档*",
             f"近{win}｜现价 {_px(last)}｜只列 ≥ ${floor / 1e4:.0f} 万的簇", ""]
    any_hit = False
    for lev, w, _c in (m.get("levs") or TIER_LEVS):
        head = f"*{lev}x*（假设占新增仓位的 {w * 100:.0f}%）"
        blocks = []
        for side, arrow, word in (("long", "🔻", "多头爆仓"),
                                  ("short", "🔺", "空头爆仓")):
            zs = clusters(m, side, lev, floor)
            if not zs:
                continue
            any_hit = True
            blocks.append(f"　{arrow} {word}")
            for z in zs[:4]:
                mid = (z["lo"] + z["hi"]) / 2
                blocks.append(
                    f"　　{_px(z['lo'])}–{_px(z['hi'])}"
                    f"　{_money(z['amount'])} U"
                    f"　距现价 {(mid / last - 1) * 100:+.1f}%")
            if len(zs) > 4:
                rest = sum(z["amount"] for z in zs[4:])
                blocks.append(f"　　（还有 {len(zs) - 4} 簇，合计 {_money(rest)} U）")
        if blocks:
            lines.append(head)
            lines.extend(blocks)
            lines.append("")
    if not any_hit:
        # **"一条都没有"要说清是哪种没有**：门槛太高、还是这个币本来就没量
        lines.append(f"这个窗口里没有任何一侧的簇达到 ${floor / 1e4:.0f} 万。")
        lines.append("要么这个币盘子小、要么窗口太短——换 30日/180日 再看。")
    lines.append("⚠️ 模型估算，不是交易所数据。杠杆分布是假设（交易所不公布"
                 "谁开了几倍），没算维持保证金率，所以位置比真实爆仓价略远。")
    return "\n".join(lines)


def zones(m, side, top=3):
    """密度最高的几个价位区间。图是给人看的，这个是给人用的。"""
    book = m["longs"] if side == "long" else m["shorts"]
    _lv = m.get("levs") or LEVS
    tot = [sum(book[L][i] for L, _w, _c in _lv) for i in range(BUCKETS)]
    idx = sorted(range(BUCKETS), key=lambda i: -tot[i])[:top]
    out = []
    for i in idx:
        if tot[i] <= 0:
            continue
        out.append({"lo": m["edges"][i], "hi": m["edges"][i] + m["width"],
                    "amount": tot[i]})
    return sorted(out, key=lambda z: -z["amount"])


def totals(m, side, last):
    """这一侧一共还有多少待爆，以及**按距离累计**是多少。

    只列前三个密集区回答不了「往下扫 5% 会引爆多少」——那才是切换窗口时
    真正会变、也真正有用的数字。密集区说的是"堆在哪儿"，累计说的是"有多少"。

    返回 {"all": 总额, "d3"/"d5"/"d10": 距现价 3%/5%/10% 以内的累计,
          "near": 最近那一档距现价几个点（没有则 None）}。

    `near` 是为了解释"三个累计都是 0"：那多半不是坏了，
    而是价格已经离开了所有密集区。不给这个数的话，一行三个 0 看着就是故障。
    """
    book = m["longs"] if side == "long" else m["shorts"]
    _lv = m.get("levs") or LEVS
    tot = [sum(book[L][i] for L, _w, _c in _lv) for i in range(BUCKETS)]
    out = {"all": 0.0, "d3": 0.0, "d5": 0.0, "d10": 0.0, "near": None}
    for i, v in enumerate(tot):
        if v <= 0:
            continue
        mid = m["edges"][i] + m["width"] / 2
        dist = abs(mid - last) / last * 100 if last else 999
        out["all"] += v
        if dist <= 3:
            out["d3"] += v
        if dist <= 5:
            out["d5"] += v
        if dist <= 10:
            out["d10"] += v
        if out["near"] is None or dist < out["near"]:
            out["near"] = dist
    return out


def _near_note(t, verb):
    """三个累计全是 0 的时候补一句「最近一档在哪」。

    不补的话，「跌3%内 0　跌5%内 0　跌10%内 0」而总量却有一千多万，
    看着就是个 bug；实际含义是**价格已经离开了所有密集区**——
    这本身是有用的信息（近处没有燃料），但得说出来才成立。
    """
    if t["d10"] > 0 or t["all"] <= 0 or t["near"] is None:
        return ""
    return f"（近处没有，最近一档在{verb} {t['near']:.1f}%）"


def _fuel_line(lt, st):
    """多空两侧的对比。**哪边堆得多，价格就更容易被往哪边推**——
    连环爆仓本身就是燃料，扫的时候是自我加速的。

    差得不明显（1.5 倍以内）就不下结论，硬要解读噪音比不解读更糟。
    """
    a, b = lt["all"], st["all"]
    if a <= 0 or b <= 0:
        return ""
    if a >= b * 1.5:
        return f"下方是上方的 {a / b:.1f} 倍——往下扫的燃料更足"
    if b >= a * 1.5:
        return f"上方是下方的 {b / a:.1f} 倍——往上扫的燃料更足"
    return "上下两边量级接近，没有明显偏向"


def _money(x):
    if x >= 1e8:
        return f"{x / 1e8:.2f}亿"
    if x >= 1e4:
        return f"{x / 1e4:.0f}万"
    return f"{x:.0f}"


def _px(x):
    return f"{x:,.6g}"


# ── 画图 ────────────────────────────────────────────────────
def render(m, sym, win, last, src="币安"):
    # 中文字体：镜像里装了 fonts-noto-cjk，但本地/旧镜像可能没有。
    # 探测复用 annotchart 那套——没字体时中文会渲染成一排豆腐块，
    # 那比退回英文难看得多，所以探到才用中文。
    from handlers.annotchart import cjk_font
    font = cjk_font()
    if font:
        plt.rcParams["font.family"] = font
        plt.rcParams["axes.unicode_minus"] = False
    T = (lambda cn, en: cn) if font else (lambda cn, en: en)

    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=110)
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FFFFFF")
    w = m["width"] * 0.86

    # 柱子：按杠杆档位堆叠。多头一侧和空头一侧本来就不重叠（被现价分开）
    bottom = [0.0] * BUCKETS
    for L, _wt, color in LEVS:
        vals = [m["longs"][L][i] + m["shorts"][L][i] for i in range(BUCKETS)]
        ax.bar([e + m["width"] / 2 for e in m["edges"]], vals, width=w,
               bottom=bottom, color=color, label=T(f"{L}x 杠杆", f"{L}x"),
               linewidth=0)
        bottom = [bottom[i] + vals[i] for i in range(BUCKETS)]

    # 累计强度：从现价往两边累加——"价格走到这儿，一共会扫掉多少"
    ax2 = ax.twinx()
    cb = m["cur_bucket"]
    tot = bottom
    cum_l, s = [None] * BUCKETS, 0.0
    for i in range(min(cb, BUCKETS - 1), -1, -1):
        s += tot[i]
        cum_l[i] = s
    cum_s, s = [None] * BUCKETS, 0.0
    for i in range(cb, BUCKETS):
        s += tot[i]
        cum_s[i] = s
    xs = [e + m["width"] / 2 for e in m["edges"]]
    lx = [x for x, v in zip(xs, cum_l) if v is not None]
    ly = [v for v in cum_l if v is not None]
    sx = [x for x, v in zip(xs, cum_s) if v is not None]
    sy = [v for v in cum_s if v is not None]
    if lx:
        ax2.plot(lx, ly, color="#E8455F", linewidth=2.2, zorder=5)
        ax2.fill_between(lx, ly, color="#E8455F", alpha=.08)
    if sx:
        ax2.plot(sx, sy, color="#12B39A", linewidth=2.2, zorder=5)
        ax2.fill_between(sx, sy, color="#12B39A", alpha=.08)

    ax.axvline(last, color="#E01E37", linestyle="--", linewidth=2, zorder=6)
    ax.annotate(T(f"现价 {_px(last)}", f"last {_px(last)}"),
                xy=(last, ax.get_ylim()[1]),
                xytext=(0, 6), textcoords="offset points",
                ha="center", color="#E01E37", fontsize=10, fontweight="bold")

    # 图会被单独转发出去，脱离文字说明——所以来源必须画在图里，不能只写在卡片上
    _en = "Bybit" if src == "Bybit" else "Binance"
    ax.set_title(T(f"{sym}/USDT 清算地图（估算）· {src}永续 · 近{win}",
                   f"{sym}/USDT liquidation map (estimated) - {_en} perp"),
                 fontsize=12, fontweight="bold", pad=22)
    # 轴上别出现 1e7 这种科学计数法——他要一眼看出是多少钱
    from matplotlib.ticker import FuncFormatter

    def _axis(v, _pos):
        if font:
            if v >= 1e8:
                return f"{v / 1e8:g}亿"
            return f"{v / 1e4:g}万" if v >= 1e4 else f"{v:g}"
        if v >= 1e6:
            return f"{v / 1e6:g}M"
        return f"{v / 1e3:g}K" if v >= 1e3 else f"{v:g}"
    ax.yaxis.set_major_formatter(FuncFormatter(_axis))
    ax2.yaxis.set_major_formatter(FuncFormatter(_axis))
    ax.set_xlim(m["lo"], m["hi"])
    ax.tick_params(labelsize=9)
    ax2.tick_params(labelsize=8, colors="#7A8794")
    ax.grid(axis="y", color="#EDF0F3", linewidth=.8)
    ax.set_axisbelow(True)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
        ax2.spines[sp].set_visible(False)
    ax.spines["bottom"].set_color("#D8DEE4")
    ax.legend(loc="upper center", bbox_to_anchor=(.5, -.09), ncol=4,
              frameon=False, fontsize=9)
    # 左下角标死"估算"，图会被单独转发出去，脱离文字说明也不能让人误读
    fig.text(.01, .012, T("模型估算，非交易所数据", "model estimate, not exchange data"),
             fontsize=7.5, color="#9AA5B1")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ── 文字 ────────────────────────────────────────────────────
def caption(m, sym, win, last, src="币安", days=None):
    """days = 实际覆盖了多少天。**够不够要写出来**：新上市的币点「1年」
    只能拿到它上市以来那几个月，标题却写着 1 年——不说的话没人看得出来
    （AKE 实测只有 333 天，BTR 363 天）。"""
    up = zones(m, "short")
    dn = zones(m, "long")
    cover = ""
    want = WINDOWS[win][1]
    # 0.95：AKE 实测 333/365=91%，用 0.9 会漏掉——差一个月对"近1年"这个标题
    # 已经足够误导了
    if days is not None and want and days < want * 0.95:
        cover = f"，实际只有 {days:.0f} 天：这个币上市时间不够"
    lines = [f"💣 *{sym} 清算地图*（估算）· {src}永续 · 近{win}{cover}",
             f"现价 {_px(last)}"]
    if dn:
        lines.append("")
        lines.append("🔻 *下方多头爆仓密集区*（跌下去会连环平多）")
        for z in dn:
            mid = (z["lo"] + z["hi"]) / 2
            lines.append(f"　{_px(z['lo'])}–{_px(z['hi'])}　约 {_money(z['amount'])} U"
                         f"　距现价 {(mid / last - 1) * 100:+.1f}%")
    if up:
        lines.append("")
        lines.append("🔺 *上方空头爆仓密集区*（涨上去会连环平空）")
        for z in up:
            mid = (z["lo"] + z["hi"]) / 2
            lines.append(f"　{_px(z['lo'])}–{_px(z['hi'])}　约 {_money(z['amount'])} U"
                         f"　距现价 {(mid / last - 1) * 100:+.1f}%")
    if not up and not dn:
        lines.append("")
        lines.append("这个窗口里持仓量几乎没增长，估不出密集区。换个更长的窗口试试。")

    # 合计 + 按距离累计。密集区回答「堆在哪儿」，这一段回答「一共有多少、
    # 扫过去 5% 会引爆多少」——切换窗口时真正会变、也真正有用的就是这几个数。
    lt = totals(m, "long", last)
    st = totals(m, "short", last)
    if lt["all"] > 0 or st["all"] > 0:
        lines.append("")
        lines.append(f"📊 *合计待爆*（现价 ±{SPAN * 100:.0f}% 以内 · 近{win}{cover}）")
        lines.append(f"🔻 下方多头 {_money(lt['all'])} U"
                     f"　｜跌3%内 {_money(lt['d3'])}"
                     f"　跌5%内 {_money(lt['d5'])}"
                     f"　跌10%内 {_money(lt['d10'])}{_near_note(lt, '跌')}")
        lines.append(f"🔺 上方空头 {_money(st['all'])} U"
                     f"　｜涨3%内 {_money(st['d3'])}"
                     f"　涨5%内 {_money(st['d5'])}"
                     f"　涨10%内 {_money(st['d10'])}{_near_note(st, '涨')}")
        fuel = _fuel_line(lt, st)
        if fuel:
            lines.append(f"　{fuel}")

    lines.append("")
    lines.append("密集区常被当成磁吸位：价格容易被推过去扫一轮再走。")
    lines.append("止损别正好压在柱子上——那正是最容易被插针带走的位置。")
    lines.append("⚠️ 模型估算不是交易所数据，口径点 ℹ️")
    return "\n".join(lines)


def detail(m, sym, win):
    tiers = "、".join(f"{L}x {w * 100:.0f}%" for L, w, _c in LEVS)
    return "\n".join([
        f"ℹ️ *清算地图 · 这张图是怎么来的*", "━━━━━━━━━━━━━━",
        "*先说最重要的一句*",
        "没有任何交易所公布「某个价位挂着多少待爆仓的仓位」——那是每个账户的",
        "私有信息。CoinGlass 那张图也是推算的。**这是模型估算，不是数据。**",
        "",
        "*怎么算的*",
        "1. 拿币安的持仓量历史，看每根 K 线期间持仓量**新增**了多少（减仓不算，",
        "　 那是平仓，不产生新的爆仓位）",
        "2. 把这些新增的仓当成建在那根 K 线的典型价上",
        "3. 永续里每份持仓同时是一多一空，所以同一份仓在两侧都留下爆仓位：",
        "　 多头爆仓价 = 建仓价 × (1 − 1/杠杆)，空头 = 建仓价 × (1 + 1/杠杆)",
        "4. 按杠杆档位分配权重后落进价格桶累加，就是那些柱子",
        "5. 现价**已经越过**的一侧抹掉——那些仓早被平了，留着是假的",
        "",
        "*哪些是假设（这决定了柱子高矮）*",
        f"· 杠杆分布：{tiers}。交易所不公布谁开了几倍，这是拍的",
        "· 建仓价用 K 线典型价，实际是一根里分散成交的",
        "· **没算维持保证金率**（要 API key，公开拿不到），",
        "　 所以估出来的位置比真实爆仓价**略远一点**",
        "",
        "*怎么用*",
        "密集区是**磁吸位**：价格容易被推过去扫一轮再走。",
        "止损别正好压在柱子上——那正是最容易被插针带走的位置。",
        "它说的是「这里有人会被平」，不是「价格会到这里」，别当信号用。",
        "",
        f"扫描窗口：近{win}（{WINDOWS[win][1]} 根 {WINDOWS[win][0]}）",
        f"画的范围：现价 ±{SPAN * 100:.0f}%，分 {BUCKETS} 个价格桶",
        *_long_caveat(win),
        "",
        "⚠️ 估算值，不构成投资建议",
    ])


def _long_caveat(win):
    """90/180 日窗口额外要说的两条。**不说的话这两个窗口会被当成更准的图看**，
    而实际上恰恰相反——窗口越长，这张图越是虚构。"""
    if win not in LONG_WINDOWS:
        return []
    return [
        "",
        f"*近{win}这个窗口要打折看*",
        "· 只有 Bybit 有这么长的持仓量历史，所以这张图**是 Bybit 的**，不是币安",
        "· 颗粒是**一根一天**：整天新增的仓都算在当天典型价上，"
        "比短窗口粗得多",
        "· **最要紧的一条**：这个模型假设那些仓到现在还没平。"
        "永续里几个月不动的仓极少，所以越老的柱子越可能已经不存在了——"
        "长窗口适合看「历史上哪些价位堆过量」，不适合当成当下的爆仓分布",
    ]


# ── 入口 ────────────────────────────────────────────────────
async def _get(sym, win, force=False):
    k = (sym.upper(), win)
    c = _cache.get(k)
    if not force and c and time.time() - c["ts"] < CACHE_TTL:
        return c["data"]
    oi, kl, last, inst, src, days = await _fetch(sym, win)
    m = build_map(oi, kl, last)
    data = (m, last, inst, src, days)
    _cache[k] = {"ts": time.time(), "data": data}
    return data


def kb(sym, win):
    # 五个窗口一行会挤成一排小方块，点不准。切成两行：短窗口一行、长窗口一行，
    # 顺带把「这两个是另一类」这件事用排版说出来。
    def _b(w):
        return InlineKeyboardButton(f"{'✅' if w == win else ''}{w}",
                                    callback_data=f"lq:w:{sym}:{w}")
    short = [_b(w) for w in WINDOWS if w not in LONG_WINDOWS]
    long_ = [_b(w) for w in WINDOWS if w in LONG_WINDOWS]
    return InlineKeyboardMarkup([
        short,
        long_,
        # 清算地图说的是「上下堆着多少爆仓单」，持仓结构说的是「这波是谁推的」。
        # **这两句要连起来才能下判断**：上方燃料清空 + 持仓不降 = 多头在等新空
        # 进场（蓄力），上方燃料清空 + 持仓开始降 = 多头在派发（见顶）。
        # 只看图会把这两种完全相反的情形读成同一种。
        [InlineKeyboardButton("🧮 按杠杆分档", callback_data=f"lq:t:{sym}:{win}"),
         InlineKeyboardButton("📊 这波是谁推的", callback_data=f"pf:r:{sym}")],
        [InlineKeyboardButton("ℹ️ 口径", callback_data=f"lq:i:{sym}:{win}"),
         InlineKeyboardButton("🔄 重算", callback_data=f"lq:r:{sym}:{win}"),
         InlineKeyboardButton("🪙 换币", callback_data="lq:pick:-:-")],
    ])


async def _send(message, sym, win, force=False):
    m, last, inst, src, days = await _get(sym, win, force)
    buf = render(m, sym, win, last, src)
    cap = caption(m, sym, win, last, src, days)
    try:
        await message.reply_photo(photo=buf, caption=cap, parse_mode="Markdown",
                                  reply_markup=kb(sym, win))
    except Exception as e:
        log.warning(f"清算地图发图失败，降级纯文本: {e}")
        buf.seek(0)
        await message.reply_photo(photo=buf, caption=cap.replace("*", ""),
                                  reply_markup=kb(sym, win))


async def liqmap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/liqmap TRUMP [1日|7日|30日|90日|180日|1年] —— 清算地图（估算各价位待强平仓位）。"""
    args = context.args or []
    if not args:
        await safe_reply(update.message,
            "💣 *清算地图*（估算）\n\n"
            "`/liqmap TRUMP`　　默认近 7 日\n"
            "`/liqmap BTC 30日`　换窗口（1日 / 7日 / 30日）\n\n"
            "画的是**各个价位上估计堆了多少待强平的仓位**。\n"
            "没有交易所公布这个数（那是私有信息），CoinGlass 那张也是推算的——"
            "所以这是模型估算，口径在图下面的 ℹ️ 里写清楚了。\n\n"
            "菜单：📈 分析与图表 → 💣 清算地图", parse_mode="Markdown")
        return
    sym = args[0].upper()
    win = next((a for a in args[1:] if a in WINDOWS), DEFAULT_WIN)
    uid = update.effective_user.id
    async with busy.guard(uid, "liqmap") as ok:
        if not ok:
            await safe_reply(update.message, busy.busy_text(uid, "liqmap", "清算地图"))
            return
        await safe_reply(update.message, f"💣 算 {sym} 近{win}的清算地图…")
        try:
            await _send(update.message, sym, win)
        except RuntimeError as e:
            await safe_reply(update.message, f"❌ {e}")
        except Exception as e:
            log.error(f"清算地图出错 {sym}: {e}")
            await safe_reply(update.message, f"画不出来：{str(e)[:100]}")


async def from_btn(query, context, action, sym, win):
    if action == "pick":
        from handlers.menu import coin_grid
        await safe_edit(query, "💣 *清算地图*　点一个币（或查其他币）",
                        reply_markup=coin_grid("lqcoin", "cat_analysis"),
                        parse_mode="Markdown")
        return
    if action == "i":
        try:
            m, _last, _inst, _src, _days = await _get(sym, win)
        except Exception as e:
            await query.answer(f"取数失败：{str(e)[:60]}", show_alert=True)
            return
        await query.answer()
        await safe_reply(query.message, detail(m, sym, win), parse_mode="Markdown")
        return
    if action == "t":
        # 按杠杆分档的明细。**不能复用 _get 的缓存**：那份是按图上的
        # 5/10/25/50 算的，这里要的是 5/10/20——档位不同，整份数据都不同。
        try:
            oi, kl, last, _inst, _src, _days = await _fetch(sym, win)
        except Exception as e:
            await query.answer(f"取数失败：{str(e)[:60]}", show_alert=True)
            return
        await query.answer()
        tm = build_map(oi, kl, last, levs=TIER_LEVS)
        await safe_reply(query.message, tier_report(tm, sym, win, last),
                         parse_mode="Markdown")
        return
    uid = query.from_user.id
    async with busy.guard(uid, "liqmap") as ok:
        if not ok:
            await query.answer(f"上一张还在算（已 {busy.elapsed(uid, 'liqmap')} 秒）",
                               show_alert=True)
            return
        await query.answer("重新算…" if action == "r" else f"切到近{win}…")
        try:
            # 图片消息改不了图，只能发新的一条
            await _send(query.message, sym, win, force=(action == "r"))
        except RuntimeError as e:
            await safe_reply(query.message, f"❌ {e}")
        except Exception as e:
            log.error(f"清算地图按钮出错 {sym}: {e}")
            await safe_reply(query.message, f"画不出来：{str(e)[:100]}")


async def on_coin(message, context, sym):
    """点了「查其他币」之后发来的币名。"""
    uid = message.from_user.id
    async with busy.guard(uid, "liqmap") as ok:
        if not ok:
            await safe_reply(message, busy.busy_text(uid, "liqmap", "清算地图"))
            return
        try:
            await _send(message, sym.upper(), DEFAULT_WIN)
        except RuntimeError as e:
            await safe_reply(message, f"❌ {e}")
        except Exception as e:
            log.error(f"清算地图出错 {sym}: {e}")
            await safe_reply(message, f"画不出来：{str(e)[:100]}")
