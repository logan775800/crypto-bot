"""爆仓一边倒告警 `/liqflip` —— 5 分钟内某一侧被扫光，用来摸顶抄底。

他的原话：「近5分钟5倍以上杠杆清算，空头爆仓占比大于80% 或多头爆仓80%，
实现摸顶抄底」。

## 先说两件必须先讲清楚的

**① 爆仓数据里没有杠杆倍数。** 交易所公布的强平单只有价格/数量/方向，
不含这一单开了几倍——「5 倍以上」这个条件**没法直接筛**。
好在几乎所有永续爆仓本来就是 5 倍以上（1~3 倍要跌 30%~100% 才爆得掉），
所以这个闸在实际数据上接近空转，不做它不影响判据。

**② 这个信号是回测出来的，不是想当然。** 37 个币、7 天、5 万多个 5 分钟
窗口（`tools\\probe_liqflip.py` / `probe_liqsize.py` / `probe_liqlevels.py`）：

    档    分位  绝对下限   抄底样本 1h涨    摸顶样本 4h涨   每天条数
    宽    90%   $1万        297  68.0%      218  36.2%    74
    标准  95%   $3万        125  65.6%       87  33.3%    30
    严    98%   $10万        36  66.7%       28  42.9%     9
    （同期基线：抄底 49.6% / 摸顶 52.2%）

## 光有相对分位不行——这是他实际用了之后骂出来的

上线后他收到「5 分钟内爆仓 5106 U，其中 100% 是空头」，一句
「这么小的金额有啥意思」。他是对的：当时门槛**只有相对口径**
（该币自己的分位），而实测 32 个币里 **27 个的 90 分位低于 2 万美元**，
门槛中位数才 $7,484。按绝对金额分桶一看：

    抄底(1h上涨)   < $1万  49.5%  ← 基线 49.6%，**零边际，纯噪音**
                  $1~3万  69.7%      $3~10万 64.0%
                 $10~50万 68.7%      > $50万 71.4%

一万以下和瞎猜没区别，而绝大多数命中恰好落在那里。
根因是**外推**：回测那批是币安成交额前 30，分位是几万到十几万美元级
（BTC $163k、ETH $164k），把同一套分位套到分位只有三千的币上，
等于用到了没验证过的区间。所以加了绝对下限，两道闸取严的那个。

⚠️ 加了绝对闸之后，**「门槛越高信号越强」这句话不再成立**（之前是成立的）。
三档胜率都在 65~68%，分位再往上提只降频率不提胜率——
所以档位的作用是**控频率**，别再拿它当"提高准确度"卖。

## 两个方向的时间尺度不一样——用错了这信号就是废的

    抄底（多头被爆）  看 **1 小时**。15m 也有效但弱，4h 就没了
    摸顶（空头被爆）  看 **4 小时**。15m 和 1h 都很弱，4h 才显著

所以卡片上必须**分别写各自的观察窗口**，不能笼统说"之后会反转"。

## 摸顶那一侧的尾部风险（这条一定要印出来）

空头一边倒那组：中位是负的（-0.562%），**但均值经常是正的**
（98 分位那档 4h 均值 +0.433%，中位却是 -0.562%）。
意思是大多数时候确实回落，但**偶尔会被轧到天上去**——
做空轧空的经典形态：胜率高、尾部亏得狠。
抄底那一侧均值和中位同号，干净得多。两边的措辞不能一样。

## 为什么要两道闸，而不是只留一道

**相对分位**管「对这个币来说算不算大」——$1 万对 BTC 是零头、对小币是天量，
只用绝对金额的话换个币就得重调，永远调不对。
**绝对下限**管「这个量值不值得看一眼」——分位再高，$5,106 也不是被扫。
两个各管一件事，谁都替代不了谁，所以取严的那个。

## 数据源

只有 Gate 的 `contract_stats` 给**逐根的多空爆仓金额**
（币安 allForceOrders 已 404、Bybit 没有公开 REST）。
5m 粒度、limit 2000 = 6.9 天，正好够算分位。
"""
import asyncio
import logging
import time

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from storage import data, save_data
from handlers.util import safe_reply, escape_md

log = logging.getLogger(__name__)

GATE = "https://api.gateio.ws/api/v4"

SIDE_TH = 0.80          # 一边占比门槛。他要的就是 80%
MIN_TURNOVER = 5_000_000
MAX_SCAN = 40           # 扫多少个币。每个币一次请求，5 分钟一轮
CONCURRENCY = 8
BASE_BARS = 2000        # 算分位用多少根 5m（2000 根 ≈ 6.9 天）
BASE_TTL = 6 * 3600     # 分位基线多久重算一次
COOLDOWN = 2 * 3600     # 同一个币多久内不重复报
PER_HOUR = 6            # 兜底闸

# ── 两道闸：相对分位 + 绝对下限 ──────────────────────────────
#
# **只有相对分位是不够的**，这是他实际收到告警之后反馈出来的：
# 「5 分钟内爆仓 5106 U，其中 100% 是空头」——这种量根本不算被扫。
#
# 去量了才发现问题有多大（`tools\\probe_liqsize.py`）：32 个币里
# **27 个的 90 分位门槛低于 2 万美元**，门槛中位数只有 $7,484。
# 把命中事件按绝对金额分桶：
#
#     抄底(1h上涨)      < $1万   49.5%   ← 基线 49.6%，**零边际，纯噪音**
#                     $1~3万   69.7%
#                     $3~10万  64.0%
#                    $10~50万  68.7%
#                     > $50万  71.4%
#
# 一万以下那一档和瞎猜没区别，而绝大多数命中恰好落在那里。
# 原因也清楚：回测那批是币安成交额前 30，它们的分位是几万到十几万美元级
# （BTC $163k、ETH $164k），**把同一套分位逻辑套到分位只有三千的币上，
# 等于外推到了没验证过的区间**。
#
# 所以档位现在同时管两个数，取严的那个。加上绝对闸之后重测：
#
#     档    分位  绝对下限   抄底样本 1h涨    摸顶样本 4h涨   每天条数
#     宽    90%   $1万        297  68.0%      218  36.2%    74
#     标准  95%   $3万        125  65.6%       87  33.3%    30
#     严    98%   $10万        36  66.7%       28  42.9%     9
#
# ⚠️ **「门槛越高信号越强」这句话现在不成立了**（加绝对闸之前是成立的）。
# 三档胜率都在 65~68%，分位再往上提只降频率不提胜率。
# 所以档位的作用是**控频率**，别再拿它当"提高准确度"卖。
LEVELS = {"宽": (0.90, 10_000), "标准": (0.95, 30_000), "严": (0.98, 100_000)}
DEFAULT_LEVEL = "标准"

# 回测实测值（两道闸都生效之后重测的），印在卡片上——
# **判据的底气要给用户看见**，不然「空头爆仓 100%」只是个现象描述。
# key 是分位，和 LEVELS 对齐。
STATS = {
    "long": {   # 多头被爆 → 抄底，看 1 小时
        0.90: (297, 0.28, 68.0), 0.95: (125, 0.34, 65.6),
        0.98: (36, 0.29, 66.7)},
    "short": {  # 空头被爆 → 摸顶，看 4 小时
        0.90: (218, -0.39, 36.2), 0.95: (87, -0.39, 33.3),
        0.98: (28, -0.11, 42.9)},
}
BASELINE = {"long": 49.6, "short": 52.2}
HORIZON = {"long": "1 小时", "short": "4 小时"}

_base = {}      # sym -> (ts, 分位阈值)


def _cfg():
    return data.setdefault("liqflip", {})


def level():
    """→ (档名, 分位, 绝对下限)。**三元组，不是二元组**——加绝对闸时
    改过一次返回值，所有解包点都要跟着改（这类改动最容易在别处安静地炸）。"""
    n = _cfg().get("level") or DEFAULT_LEVEL
    if n not in LEVELS:
        n = DEFAULT_LEVEL
    q, floor = LEVELS[n]
    return n, q, floor


def set_level(name):
    if name not in LEVELS:
        return None
    _cfg()["level"] = name
    save_data()
    return level()


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


def quota_left():
    c = _cfg()
    bucket = int(time.time() // 3600)
    if c.get("hour") != bucket:
        c["hour"] = bucket
        c["sent"] = 0
    return max(0, PER_HOUR - int(c.get("sent") or 0))


def _used(n=1):
    quota_left()          # 先滚小时，否则记完的数下一次查询就被抹掉
    c = _cfg()
    c["sent"] = int(c.get("sent") or 0) + n


def cooled(sym, side, now=None):
    now = now or time.time()
    rec = _cfg().setdefault("sent_at", {})
    return now - float(rec.get(f"{sym}:{side}") or 0) < COOLDOWN


def mark(sym, side, now=None):
    now = now or time.time()
    rec = _cfg().setdefault("sent_at", {})
    rec[f"{sym}:{side}"] = now
    for k in [k for k, v in rec.items() if now - float(v) > COOLDOWN * 3]:
        rec.pop(k, None)


def seen_bar(sym, ts):
    """同一根 K 线只判一次。扫描每 5 分钟一轮而 K 线也是 5 分钟，
    错位时会连着两轮看到同一根——不去重就是同一个事件报两次。"""
    rec = _cfg().setdefault("last_bar", {})
    if rec.get(sym) == ts:
        return True
    rec[sym] = ts
    if len(rec) > 500:
        for k in list(rec)[:200]:
            rec.pop(k, None)
    return False


# ── 取数 ────────────────────────────────────────────────────
async def _stats(c, base, limit):
    r = await c.get(f"{GATE}/futures/usdt/contract_stats",
                    params={"contract": f"{base}_USDT", "interval": "5m",
                            "limit": limit})
    if r.status_code != 200:
        return []
    d = r.json()
    return d if isinstance(d, list) else []


def _liq(x):
    try:
        return (float(x.get("long_liq_usd") or 0),
                float(x.get("short_liq_usd") or 0))
    except (TypeError, ValueError):
        return 0.0, 0.0


def percentile(vals, q):
    v = sorted(x for x in vals if x > 0)
    if len(v) < 30:
        return None          # 样本太少，分位数没意义，这个币这轮不判
    return v[min(len(v) - 1, int(len(v) * q))]


async def baseline(c, base, q, floor=0, now=None):
    """这个币的爆仓额门槛。**两道闸取严的那个**：相对分位管"对这个币算不算大"，
    绝对下限管"这个量值不值得看一眼"。

    缓存的是**分位值**不是最终门槛——换档时绝对下限跟着变，
    把最终值缓存起来的话切档要等 6 小时才生效。
    """
    now = now or time.time()
    hit = _base.get(base)
    if hit and now - hit[0] < BASE_TTL and hit[2] == q:
        pct = hit[1]
    else:
        rows = await _stats(c, base, BASE_BARS)
        pct = percentile([sum(_liq(x)) for x in rows], q)
        _base[base] = (now, pct, q)
    if pct is None:
        return None
    return max(pct, floor)


async def universe(c):
    """扫哪些币。

    ⚠️ **成交额按币安排，不按 Gate 排。** 第一版按 Gate 自己的成交额取，
    自检一跑就露馅：Gate 的量高度集中，≥500 万的只有 32 个，而且构成和
    回测那批不一样——**回测用的是币安成交额前 30**。
    验的池子和跑的池子不是同一批，那份 68%/34% 的胜率就不能直接往上套。
    所以这里按币安排序（和回测同源），再去 Gate 取爆仓数据（只有它有）。

    **代币化美股/商品必须剔**——同一个坑这是第四次了（微市值榜、涨跌榜、
    多空比榜各踩过一次）：成交额前几名里就有 SNDK、SKHYNIX、XAU、SPCX，
    它们的"爆仓"和币圈情绪不是一回事。
    """
    try:
        from handlers.klines import noncrypto_bases
        from handlers.dayrank import _BSTOCK
        skip = set(await noncrypto_bases()) | _BSTOCK
    except Exception as e:
        log.warning(f"[liqflip] 取非加密品类失败，本轮不剔代币化美股: {e}")
        skip = set()
    # Gate 上有哪些合约（爆仓数据只有它给，没有的币扫了也白扫）
    gate = set()
    try:
        r = await c.get(f"{GATE}/futures/usdt/tickers")
        if r.status_code == 200:
            gate = {str(x.get("contract") or "")[:-5]
                    for x in (r.json() or [])
                    if str(x.get("contract") or "").endswith("_USDT")}
    except Exception as e:
        log.warning(f"[liqflip] 取 Gate 合约列表失败: {e}")
    r = await c.get("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if r.status_code != 200:
        return [], 0
    rows = []
    for x in (r.json() or []):
        s = str(x.get("symbol") or "")
        if not s.endswith("USDT"):
            continue
        b = s[:-4]
        if b in skip or (gate and b not in gate):
            continue
        try:
            v = float(x.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if v >= MIN_TURNOVER:
            rows.append((b, v))
    rows.sort(key=lambda x: -x[1])
    return [b for b, _v in rows[:MAX_SCAN]], len(rows)


# ── 判据 ────────────────────────────────────────────────────
def classify(long_usd, short_usd, cut):
    """→ ("long"|"short", 占比) 或 None。

    `long` = 多头被爆（价格刚被砸穿）→ 抄底方向
    `short` = 空头被爆（价格刚被轧上去）→ 摸顶方向
    命名按**被爆的是谁**，不是按建议做什么——后者会在代码里读反。
    """
    tot = long_usd + short_usd
    if cut is None or tot < cut or tot <= 0:
        return None
    if short_usd / tot >= SIDE_TH:
        return "short", short_usd / tot
    if long_usd / tot >= SIDE_TH:
        return "long", long_usd / tot
    return None


async def scan_once(c, q, syms, floor=0):
    """→ [(币, 方向, 占比, 爆仓额, 门槛, 价格, 根时间)]。不做去重和配额，
    好让自检能看到"本来会报什么"。"""
    sem = asyncio.Semaphore(CONCURRENCY)
    out = []

    async def one(b):
        async with sem:
            try:
                cut = await baseline(c, b, q, floor)
                if cut is None:
                    return
                rows = await _stats(c, b, 3)
            except Exception as e:
                log.debug(f"[liqflip] {b} 取数失败: {e}")
                return
        if len(rows) < 2:
            return
        # **用倒数第二根，不用最后一根**：最后那根多半还没收盘，
        # 爆仓额只累积了一部分，拿它比门槛会系统性偏低（v1.33.1 同一个坑）
        bar = rows[-2]
        lg, sh = _liq(bar)
        got = classify(lg, sh, cut)
        if not got:
            return
        side, share = got
        try:
            px = float(bar.get("mark_price") or 0)
            ts = int(bar.get("time") or 0)
        except (TypeError, ValueError):
            return
        out.append((b, side, share, lg + sh, cut, px, ts))

    await asyncio.gather(*[one(b) for b in syms])
    return sorted(out, key=lambda x: -(x[3] / (x[4] or 1)))


# ── 渲染 ────────────────────────────────────────────────────
def _money(v):
    from handlers.liqmap import _money as m
    return m(v)


def format_hit(sym, side, share, total, cut, px, q):
    lv, _q, _floor = level()
    n, med, win = STATS[side].get(q, (0, 0.0, 0.0))
    base = BASELINE[side]
    if side == "long":
        head = f"🩸 *多头被扫* · {escape_md(sym)}"
        what = (f"5 分钟内爆仓 {_money(total)} U，其中 **{share*100:.0f}% 是多头**"
                f"——刚被砸穿，多头在被强制卖出")
        read = (f"回测里这种形态**之后 1 小时** 上涨概率 **{win:.0f}%**"
                f"（同期基线 {base:.0f}%），中位 **{med:+.2f}%**")
        note = ("抄底方向。注意这个边际**只在 1 小时尺度上成立**，"
                "拉到 4 小时就没了——不是趋势信号，是短线反抽。")
    else:
        head = f"🔥 *空头被扫* · {escape_md(sym)}"
        what = (f"5 分钟内爆仓 {_money(total)} U，其中 **{share*100:.0f}% 是空头**"
                f"——刚被轧上去，空头在被强制买回")
        read = (f"回测里这种形态**之后 4 小时** 上涨概率只有 **{win:.0f}%**"
                f"（同期基线 {base:.0f}%），中位 **{med:+.2f}%**")
        note = ("摸顶方向，但这一侧**尾部风险大**：中位是跌的，"
                "均值却经常是涨的——大多数时候回落，偶尔被轧到天上去。"
                "胜率高不等于赔率好，仓位别按胜率给。\n"
                "而且要等 **4 小时**才显著，15 分钟和 1 小时都很弱。")
    return "\n".join([
        head, "",
        what,
        f"现价 {px:,.6g}　门槛 {_money(cut)} U"
        f"（{int(q*100)} 分位 与 绝对下限 {_money(LEVELS[lv][1])} 取严的那个）",
        "",
        f"📊 {read}",
        f"　样本 {n} 次 · 26 个币 7 天 · {lv}档",
        "",
        note,
        "",
        "⚠️ 统计边际不是保证，单次结果和胜率无关。不构成投资建议",
    ])


def kb(sym):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📊 {sym} 谁推的", callback_data=f"pf:r:{sym}"),
         InlineKeyboardButton(f"💣 清算图", callback_data=f"lq:w:{sym}:7日")],
        [InlineKeyboardButton("⚙️ 设置", callback_data="lf:panel")],
    ])


# ── 后台任务 ────────────────────────────────────────────────
async def scan(context: ContextTypes.DEFAULT_TYPE):
    chats = subs()
    if not chats:
        return
    _lv, q, floor = level()
    async with httpx.AsyncClient(timeout=25) as c:
        syms, pool = await universe(c)
        if not syms:
            return
        hits = await scan_once(c, q, syms, floor)
    sent = 0
    for sym, side, share, total, cut, px, ts in hits:
        if quota_left() <= 0:
            break
        if seen_bar(f"{sym}:{side}", ts) or cooled(sym, side):
            continue
        mark(sym, side)
        text = format_hit(sym, side, share, total, cut, px, q)
        for cid in chats:
            try:
                await context.bot.send_message(int(cid), text,
                                               parse_mode="Markdown",
                                               reply_markup=kb(sym))
            except Exception as e:
                log.warning(f"[liqflip] 推送失败 {cid}: {e}")
        _used(1)
        sent += 1
    if sent or hits:
        save_data()


# ── 自检 ────────────────────────────────────────────────────
async def selftest():
    """现在扫一遍，看有没有命中。**低频告警必须能自检**：
    几小时不响的时候，「没有一边倒的爆仓」和「任务挂了」看起来一模一样。"""
    lv, q, floor = level()
    async with httpx.AsyncClient(timeout=25) as c:
        syms, pool = await universe(c)
        if not syms:
            return "❌ 取不到币种列表——Gate 的行情接口没通，发 /datacheck 看看。"
        hits = await scan_once(c, q, syms, floor)
    lines = [f"🩸 *爆仓一边倒 · 自检*（{lv}档：{int(q*100)} 分位 且 ≥${floor:,}，"
             f"一边 ≥{int(SIDE_TH*100)}%）",
             f"扫了成交额前 {len(syms)} 个（池子里 ≥{MIN_TURNOVER//10000}万的有 {pool} 个）"]
    if not hits:
        rate = {"宽": 74, "标准": 30, "严": 9}.get(lv, 30)
        lines += ["", f"这一轮没有命中——正常。{lv}档实测约 {rate} 条/天，"
                      f"平摊到每 5 分钟一轮就是绝大多数轮次都是空的。"]
        return "\n".join(lines)
    lines.append("")
    lines.append(f"*命中 {len(hits)} 个*")
    for sym, side, share, total, cut, _px, _ts in hits[:6]:
        cn = "多头被扫→抄底" if side == "long" else "空头被扫→摸顶"
        tag = "（冷却中，不重复报）" if cooled(sym, side) else ""
        lines.append(f"　{sym}　{cn}　{share*100:.0f}%　{_money(total)} U"
                     f"（门槛 {_money(cut)}）{tag}")
    lines.append("")
    lines.append(f"这一小时还能推 {quota_left()} 条")
    return "\n".join(lines)


# ── 入口 ────────────────────────────────────────────────────
def panel_text(chat_id):
    lv, q, floor = level()
    on = "✅ 已订阅" if is_on(chat_id) else "⬜ 未订阅"
    return "\n".join([
        f"🩸 *爆仓一边倒告警*　{on}",
        "",
        "5 分钟内某一侧被扫光就推给你，用来摸顶抄底。",
        "",
        f"当前：**{lv}**档　两道闸取严的那个：",
        f"　① 爆仓额 ≥ 该币 7 天分布的 {int(q*100)} 分位",
        f"　② 爆仓额 ≥ **${floor:,}**（绝对下限）",
        f"　③ 一边占比 ≥{int(SIDE_TH*100)}%",
        f"每小时最多 {PER_HOUR} 条，同一个币 {COOLDOWN//3600} 小时内不重复",
        "",
        "*回测（37 个币 · 7 天 · 5 万多个 5 分钟窗口）*",
        "```",
        "档   分位  下限   抄底1h涨  摸顶4h涨  每天",
        "宽   90%  $1万    68.0%    36.2%    74条",
        "标准 95%  $3万    65.6%    33.3%    30条",
        "严   98%  $10万   66.7%    42.9%     9条",
        "基线              49.6%    52.2%",
        "```",
        "**档位管的是频率，不是准确度**——三档胜率都在 65~68%，",
        "分位再往上提只会更少，不会更准。",
        "",
        "**为什么要绝对下限**：只看相对分位的话，小币的 90 分位可能只有",
        "三千多美元。实测 **$1 万以下那一档抄底胜率 49.5%，基线 49.6%",
        "——零边际，纯噪音**，而绝大多数命中恰好落在那里。",
        "",
        "**两个方向的时间尺度不一样**：抄底看 1 小时（4 小时就没了），",
        "摸顶看 4 小时（15m 和 1h 都很弱）。用错尺度这信号就是废的。",
        "",
        "⚠️ 摸顶那一侧尾部风险大：中位是跌的，均值却经常是涨的——",
        "多数时候回落，偶尔被轧到天上。胜率高不等于赔率好。",
        "",
        "ℹ️ 爆仓数据里**没有杠杆倍数**（交易所不公布），所以「5 倍以上」",
        "筛不了。好在 1~3 倍要跌 30%~100% 才爆得掉，实际上都是高杠杆。",
    ])


def panel_kb(chat_id):
    lv, _q, _f = level()
    rows = [[InlineKeyboardButton(f"{'✅' if x == lv else ''}{x}档",
                                  callback_data=f"lf:lv:{x}") for x in LEVELS]]
    rows.append([InlineKeyboardButton(
        "🔴 关闭告警" if is_on(chat_id) else "🟢 开启告警",
        callback_data="lf:toggle")])
    rows.append([InlineKeyboardButton("🔍 现在有没有（自检）", callback_data="lf:test")])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="cat_notify")])
    return InlineKeyboardMarkup(rows)


async def liqflip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/liqflip —— 爆仓一边倒告警（摸顶抄底）。"""
    chat_id = update.effective_chat.id
    args = [str(x).lower() for x in (context.args or [])]
    if args and args[0] in ("on", "开", "订阅"):
        toggle(chat_id, True)
        await safe_reply(update.message, panel_text(chat_id),
                         parse_mode="Markdown", reply_markup=panel_kb(chat_id))
        return
    if args and args[0] in ("off", "关", "退订"):
        toggle(chat_id, False)
        await safe_reply(update.message, "🔕 已关闭爆仓一边倒告警")
        return
    if args and args[0] in ("now", "自检", "现在"):
        msg = await safe_reply(update.message, "🩸 扫一遍…（十几秒）")
        txt = await selftest()
        if msg:
            try:
                await msg.edit_text(txt, parse_mode="Markdown")
                return
            except Exception:
                pass
        await safe_reply(update.message, txt, parse_mode="Markdown")
        return
    if args and context.args and context.args[0].strip() in LEVELS:
        set_level(context.args[0].strip())
    await safe_reply(update.message, panel_text(chat_id),
                     parse_mode="Markdown", reply_markup=panel_kb(chat_id))


async def on_button(query, context):
    from handlers.util import safe_edit
    d = query.data
    cid = query.message.chat_id
    if d == "lf:toggle":
        on = toggle(cid, not is_on(cid))
        try:
            await query.answer("🩸 已开启" if on else "🔕 已关闭", show_alert=False)
        except Exception:
            pass
    elif d.startswith("lf:lv:"):
        set_level(d.split(":")[2])
        try:
            await query.answer("已切档")
        except Exception:
            pass
    elif d == "lf:test":
        await safe_edit(query, "🩸 扫一遍…（十几秒）")
        txt = await selftest()
        await safe_edit(query, txt, parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            "⬅️ 回设置", callback_data="lf:panel")]]))
        return
    await safe_edit(query, panel_text(cid), parse_mode="Markdown",
                    reply_markup=panel_kb(cid))
