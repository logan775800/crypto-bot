"""复盘 /rstats —— 把虚拟盘的已平仓成绩单算清楚，再交给 AI 做行为诊断。

数据源只有虚拟盘（v1.70.0 起实盘整体移除）。虚拟盘成交记录不联网、不需要密钥，
任何人查的都是自己的。统计口径（胜率/盈亏比/期望值/回撤/亏损归因/时长/时段）
仍按原来实盘那套写，虚拟盘记录在 load_virtual 里转成同一结构。
"""
import time
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.util import safe_reply, safe_edit, escape_md

log = logging.getLogger(__name__)

DEFAULT_DAYS = 30
MAX_DAYS = 180


# ── 统计 ───────────────────────────────────────────────────────────
def compute_stats(trades):
    """核心成绩单。空列表返回 None，调用方负责提示。"""
    if not trades:
        return None
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    total = sum(pnls)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    # 盈亏比：没亏过就是无穷，别除零
    rr = (avg_win / avg_loss) if avg_loss > 0 else (float("inf") if wins else 0.0)
    win_rate = len(wins) / n * 100

    # 最大回撤：按平仓顺序累计的资金曲线，峰值到谷底
    cum = peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # 连亏：最长 + 当前
    max_streak = cur_streak = tail = 0
    for p in pnls:
        if p < 0:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0
    for p in reversed(pnls):
        if p < 0:
            tail += 1
        else:
            break

    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate, "total": total,
        "avg_win": avg_win, "avg_loss": avg_loss, "rr": rr,
        "expectancy": total / n,          # 每笔期望值
        "max_dd": max_dd,
        "max_loss_streak": max_streak, "cur_loss_streak": tail,
        "best": max(pnls), "worst": min(pnls),
    }


def _agg(trades, keyfn):
    """按维度聚合 → [(key, 笔数, 总盈亏, 胜率)]，按总盈亏升序（最亏的在前）。"""
    g = {}
    for t in trades:
        k = keyfn(t)
        if k is None:
            continue
        e = g.setdefault(k, [0, 0.0, 0])
        e[0] += 1
        e[1] += t["pnl"]
        if t["pnl"] > 0:
            e[2] += 1
    return sorted(((k, v[0], v[1], v[2] / v[0] * 100) for k, v in g.items()),
                  key=lambda x: x[2])


DUR_BUCKETS = [
    (300, "<5分钟"), (1800, "5~30分钟"), (7200, "30分钟~2小时"),
    (28800, "2~8小时"), (86400, "8~24小时"), (float("inf"), ">24小时"),
]


def _dur_bucket(t):
    d = t.get("dur")
    if d is None:
        return None
    for lim, label in DUR_BUCKETS:
        if d < lim:
            return label
    return ">24小时"


def _hour_bucket(t):
    """按平仓时刻的本地小时分组。容器 TZ=Asia/Shanghai，localtime 即北京时间。"""
    if not t.get("ts"):
        return None
    h = time.localtime(t["ts"] / 1000).tm_hour
    return f"{h:02d}:00-{h:02d}:59"


# ── 虚拟盘数据源 ────────────────────────────────────────────────
# 复盘这一整层（亏损归因、行为画像、周报）最早只认实盘，
# 于是「先用虚拟盘练手」的阶段，最有价值的诊断反而一个都用不上——
# 而那个阶段恰恰最需要诊断，因为习惯就是在那时候养成的。
# 虚拟盘的成交记录字段够齐，转成同一套结构就能复用全部分析。


def load_virtual(uid, days=DEFAULT_DAYS):
    """虚拟盘成交 → 统一的 trade 列表（沿用原实盘 closed-pnl 的结构）。不联网。"""
    from storage import data as _d
    a = (_d.get("vtrade") or {}).get(str(uid)) or {}
    cutoff = time.time() - int(days) * 86400
    out = []
    for h in a.get("history") or []:
        ts = float(h.get("ts") or 0)
        if ts < cutoff:
            continue
        margin = float(h.get("margin") or 0)
        lev = float(h.get("lev") or 0)
        sym = str(h.get("sym") or "?").upper()
        out.append({
            # 归因里的 MAJORS 用的是 BTCUSDT 这种写法，这里统一补上后缀
            "symbol": sym if sym.endswith("USDT") else sym + "USDT",
            "side": h.get("side") or "long",
            "pnl": float(h.get("pnl") or 0),
            "entry": float(h.get("entry") or 0),
            "exit": float(h.get("exit") or 0),
            "qty": (margin * lev / float(h["entry"])) if h.get("entry") else 0.0,
            "value": float(h.get("value") or margin * lev),
            "lev": lev,
            "ts": int(ts * 1000),        # 统计口径是毫秒（沿用原实盘结构）
            "dur": h.get("dur"),
        })
    out.sort(key=lambda t: t["ts"])
    return out


def virtual_funding(uid, days=DEFAULT_DAYS):
    """虚拟盘资金费净支出 {币: USDT}。正 = 付出，负 = 收到。"""
    from storage import data as _d
    a = (_d.get("vtrade") or {}).get(str(uid)) or {}
    cutoff = time.time() - int(days) * 86400
    tot = {}
    for h in a.get("history") or []:
        if float(h.get("ts") or 0) < cutoff:
            continue
        f = float(h.get("funding") or 0)
        if f:
            sym = str(h.get("sym") or "?").upper()
            tot[sym] = tot.get(sym, 0.0) + f
    return tot


# ── 亏损归因 ────────────────────────────────────────────────────
# 「胜率 42%」告诉不了你该改什么。「你的亏损 70% 来自持仓超过一天的山寨多单」
# 才是可执行的。这里把每笔亏损打上**行为标签**，再按标签汇总亏了多少。
#
# 标签必须能从成交数据本身判定，不能靠猜意图。所以只用四条能证实的：
MAJORS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
BIG_LEV = 20            # 超过这个杠杆算重杠杆
LONG_HOLD = 86400       # 持仓超过一天
BIG_LOSS_R = 2.0        # 单笔亏损超过账户典型亏损的这个倍数 = 止损被放大过


def loss_tags(t, typical_loss):
    """给一笔交易打行为标签。只打**能从数据证实**的，不猜意图。"""
    tags = []
    if t["pnl"] >= 0:
        return tags
    if t["symbol"] not in MAJORS:
        tags.append("山寨小币")
    if (t.get("lev") or 0) >= BIG_LEV:
        tags.append("重杠杆")
    d = t.get("dur")
    if d is not None and d >= LONG_HOLD:
        tags.append("持仓超一天")
    if d is not None and d < 300:
        tags.append("5分钟内被打掉")
    if typical_loss > 0 and abs(t["pnl"]) >= typical_loss * BIG_LOSS_R:
        tags.append("单笔亏损放大")
    return tags or ["其他"]


def attribution(trades):
    """亏损归因：{标签: (笔数, 亏损额)}，按亏损额降序。

    注意分母：一笔亏损可能同时命中多个标签（重杠杆的山寨单持仓超一天），
    所以各标签金额**相加会超过总亏损**。这是刻意的——我们要回答的是
    「哪些行为参与了亏损」，不是把亏损切成互斥的份额。
    """
    losses = [t for t in trades if t["pnl"] < 0]
    if not losses:
        return [], 0.0
    amounts = sorted(abs(t["pnl"]) for t in losses)
    typical = amounts[len(amounts) // 2]        # 中位亏损作为"典型"
    g = {}
    for t in losses:
        for tag in loss_tags(t, typical):
            e = g.setdefault(tag, [0, 0.0])
            e[0] += 1
            e[1] += abs(t["pnl"])
    rows = sorted(((k, v[0], v[1]) for k, v in g.items()), key=lambda x: -x[2])
    return rows, sum(abs(t["pnl"]) for t in losses)


def build_attribution_text(trades):
    if not trades:
        return ""
    rows, total = attribution(trades)
    if not rows:
        return "\n*亏损归因*　这段时间没有亏损单。"
    lines = ["", f"*亏损归因*　总亏损 {_money(-total)}"]
    for tag, n, amt in rows[:6]:
        lines.append(f"　{tag}　{n}笔　{_money(-amt)}　({amt/total*100:.0f}%)")
    lines.append("_一笔可能命中多个标签，百分比相加会超过 100%——"
                 "这里回答的是「哪些行为参与了亏损」，不是切份额_")
    return "\n".join(lines)


def _money(x):
    return f"{x:+,.2f}"


def _dur_txt(sec):
    if sec is None:
        return "—"
    if sec < 60:
        return f"{sec:.0f}秒"
    if sec < 3600:
        return f"{sec/60:.0f}分钟"
    if sec < 86400:
        return f"{sec/3600:.1f}小时"
    return f"{sec/86400:.1f}天"


# ── 文本渲染 ────────────────────────────────────────────────────────
def build_stats_text(trades, days, fund=None, env=""):
    s = compute_stats(trades)
    if not s:
        return f"📊 *复盘* {env}\n\n近 {days} 天没有已平仓记录。"
    rr_txt = "∞（还没亏过）" if s["rr"] == float("inf") else f"{s['rr']:.2f}"
    exp_emoji = "🟢" if s["expectancy"] > 0 else "🔴"
    lines = [
        f"📊 *复盘* 近{days}天 {env}",
        "━━━━━━━━━━━━━━",
        f"总盈亏 *{_money(s['total'])}* USDT｜{s['n']} 笔",
        f"胜率 {s['win_rate']:.1f}%（{s['wins']}胜 {s['losses']}负）",
        f"盈亏比 {rr_txt}（均盈 {s['avg_win']:,.2f} / 均亏 {s['avg_loss']:,.2f}）",
        f"{exp_emoji} 期望值 *{_money(s['expectancy'])}* /笔　← 这个是正的才算有系统",
        f"最大回撤 -{s['max_dd']:,.2f}｜最长连亏 {s['max_loss_streak']} 笔"
        + (f"（当前正连亏 {s['cur_loss_streak']} 笔 ⚠️）" if s["cur_loss_streak"] >= 3 else ""),
        f"最赚一笔 {_money(s['best'])}｜最亏一笔 {_money(s['worst'])}",
    ]

    # 亏损归因放在币种拆解之前：「亏在哪种行为上」比「亏在哪个币上」更可执行
    attr = build_attribution_text(trades)
    if attr:
        lines.append(attr)
    # 币种：最亏的 5 个
    by_sym = _agg(trades, lambda t: t["symbol"].replace("USDT", ""))
    if by_sym:
        lines.append("\n*💸 最亏的币*")
        for k, n, p, wr in by_sym[:5]:
            lines.append(f"　{escape_md(k)}　{_money(p)}｜{n}笔 胜率{wr:.0f}%")
        best = [x for x in by_sym[::-1] if x[2] > 0][:3]
        if best:
            lines.append("*🤑 最赚的币*")
            for k, n, p, wr in best:
                lines.append(f"　{escape_md(k)}　{_money(p)}｜{n}笔 胜率{wr:.0f}%")

    # 多空
    by_side = _agg(trades, lambda t: "做多" if t["side"] == "long" else "做空")
    if len(by_side) > 0:
        lines.append("\n*⚖️ 多空*")
        for k, n, p, wr in by_side:
            lines.append(f"　{k}　{_money(p)}｜{n}笔 胜率{wr:.0f}%")

    # 持仓时长（只有还原到开仓时间才有）
    by_dur = _agg(trades, _dur_bucket)
    if by_dur:
        order = {label: i for i, (_, label) in enumerate(DUR_BUCKETS)}
        lines.append("\n*⏱ 持仓时长 vs 盈亏*")
        for k, n, p, wr in sorted(by_dur, key=lambda x: order.get(x[0], 99)):
            lines.append(f"　{k}　{_money(p)}｜{n}笔 胜率{wr:.0f}%")
        durs = [t["dur"] for t in trades if t.get("dur") is not None]
        w = [t["dur"] for t in trades if t.get("dur") is not None and t["pnl"] > 0]
        l = [t["dur"] for t in trades if t.get("dur") is not None and t["pnl"] < 0]
        if durs:
            lines.append(f"　平均持仓 {_dur_txt(sum(durs)/len(durs))}"
                         + (f"｜赚钱单 {_dur_txt(sum(w)/len(w))}" if w else "")
                         + (f"｜亏钱单 {_dur_txt(sum(l)/len(l))}" if l else ""))

    # 时段：只列最亏的 3 个
    by_hour = _agg(trades, _hour_bucket)
    worst_h = [x for x in by_hour if x[2] < 0][:3]
    if worst_h:
        lines.append("\n*🕐 最亏时段*（北京时间，按平仓时刻）")
        for k, n, p, wr in worst_h:
            lines.append(f"　{k}　{_money(p)}｜{n}笔 胜率{wr:.0f}%")

    # 资金费
    if fund:
        tot_f = sum(fund.values())
        if abs(tot_f) > 0.01:
            top = sorted(fund.items(), key=lambda x: -abs(x[1]))[:3]
            lines.append(f"\n*💵 资金费净{'支出' if tot_f > 0 else '收入'}* {abs(tot_f):,.2f} USDT")
            lines.append("　" + "、".join(
                f"{escape_md(k.replace('USDT',''))} {v:+,.2f}" for k, v in top))

    lines.append("\n⚠️ 数据来自虚拟盘，不构成投资建议")
    return "\n".join(lines)


# ── AI 复盘 ────────────────────────────────────────────────────────
AI_SYSTEM = (
    "你是一个加密永续合约交易者的复盘教练。用户给你的是他**虚拟盘**的成绩单统计。"
    "你的任务不是安慰他，也不是预测行情，而是从这些数字里找出**可改的行为模式**。\n\n"
    "要求：\n"
    "1) 先一句话给整体判断：这套打法目前是正期望还是负期望，主要漏在哪。\n"
    "2) 找 2~4 条具体的行为特征，每条必须**引用具体数字**做证据，并说明它通常意味着什么"
    "（例：平均持仓 8 分钟且短持仓大幅亏损 = 追高进场后被立刻打脸，典型 FOMO；"
    "做空胜率远高于做多 = 在逆势币上做多；某币独占大半亏损 = 该币不适合你的节奏/波动）。\n"
    "3) 给 2~3 条**下周就能执行**的具体调整（可量化的，比如「XX 币先停手」「持仓不足 N 分钟不平」"
    "「单笔风险降到权益 0.5%」），别说「注意风控」这种废话。\n"
    "4) 如果样本量太小（<20 笔），要明确说结论不稳，别过度解读。\n\n"
    "用简体中文，直接、具体、不客套。控制在 400 字内。末尾一句「不构成投资建议」。"
)


def build_ai_digest(trades, days, fund=None):
    """给 AI 的紧凑摘要——不丢原始几百笔，只丢统计结论（省 token 且模型不会算错）。

    这份会经中转站，所以脱敏时把所有金额换算成 **R 倍数**（1R = 平均亏损）。
    这不只是遮挡：R 本来就比 USDT 更适合复盘——它把不同时期、不同仓位大小
    的交易拉到同一把尺子上，"期望值 +0.35R" 比 "+12.4 USDT" 信息量更大。
    """
    from handlers import privacy
    s = compute_stats(trades)
    if not s:
        return None
    red = privacy.enabled()
    unit = abs(s["avg_loss"]) or 1.0        # 1R = 平均亏损

    def m(x, signed=True):
        if not red:
            return f"{x:+.2f} USDT" if signed else f"{x:.2f}"
        return f"{x/unit:+.2f}R" if signed else f"{x/unit:.2f}R"

    rr = "无穷(未亏过)" if s["rr"] == float("inf") else f"{s['rr']:.2f}"
    out = [
        f"统计窗口: 近{days}天, 共{s['n']}笔已平仓"
        + ("（金额已脱敏为 R 倍数，1R = 平均亏损）" if red else ""),
        f"总盈亏 {m(s['total'])}, 胜率 {s['win_rate']:.1f}% ({s['wins']}胜{s['losses']}负)",
        f"均盈 {m(s['avg_win'], False)} / 均亏 {m(s['avg_loss'], False)}, 盈亏比 {rr}",
        f"每笔期望值 {m(s['expectancy'])}",
        f"最大回撤 {m(s['max_dd'], False)}, 最长连亏 {s['max_loss_streak']}笔, 当前连亏 {s['cur_loss_streak']}笔",
        f"最赚一笔 {m(s['best'])}, 最亏一笔 {m(s['worst'])}",
    ]

    def _dump(title, rows, limit=8):
        if not rows:
            return
        out.append(title)
        for k, n, p, wr in rows[:limit]:
            out.append(f"  {k}: {m(p)}, {n}笔, 胜率{wr:.0f}%")

    rows, tot_loss = attribution(trades)
    if rows:
        out.append(f"亏损归因(总亏损 {m(tot_loss, False)}，一笔可命中多个标签):")
        for tag, n, amt in rows[:6]:
            out.append(f"  {tag}: {m(amt, False)}, {n}笔 ({amt/tot_loss*100:.0f}%)")
    _dump("按币种(最亏在前):", _agg(trades, lambda t: t["symbol"].replace("USDT", "")))
    _dump("按方向:", _agg(trades, lambda t: "做多" if t["side"] == "long" else "做空"))
    by_dur = _agg(trades, _dur_bucket)
    if by_dur:
        order = {label: i for i, (_, label) in enumerate(DUR_BUCKETS)}
        _dump("按持仓时长:", sorted(by_dur, key=lambda x: order.get(x[0], 99)))
        durs = [t["dur"] for t in trades if t.get("dur") is not None]
        w = [t["dur"] for t in trades if t.get("dur") is not None and t["pnl"] > 0]
        l = [t["dur"] for t in trades if t.get("dur") is not None and t["pnl"] < 0]
        if durs:
            out.append(f"平均持仓 {sum(durs)/len(durs):.0f}秒"
                       + (f", 盈利单平均 {sum(w)/len(w):.0f}秒" if w else "")
                       + (f", 亏损单平均 {sum(l)/len(l):.0f}秒" if l else ""))
    _dump("按平仓时段(北京时间,最亏在前):", _agg(trades, _hour_bucket), 5)
    levs = [t["lev"] for t in trades if t["lev"] > 0]
    if levs:
        out.append(f"平均杠杆 {sum(levs)/len(levs):.1f}x, 最高 {max(levs):.0f}x")
    if fund:
        tf = sum(fund.values())
        if abs(tf) > 0.01:
            out.append(f"资金费净{'支出' if tf > 0 else '收入'} {m(abs(tf), False)}")
    return "\n".join(out)


async def build_ai_review(trades, days, fund=None):
    from config import AI_API_KEY, AI_BASE_URL
    if not AI_API_KEY or not AI_BASE_URL:
        return "AI 未配置（缺 AI_API_KEY / AI_BASE_URL）"
    digest = build_ai_digest(trades, days, fund)
    if not digest:
        return "没有可复盘的交易记录"
    from handlers.ai import ask_ai_messages
    reply = await ask_ai_messages(
        [{"role": "user", "content": f"这是我虚拟盘的交易成绩单，帮我复盘：\n\n{digest}"}],
        system=AI_SYSTEM)
    return reply


# ── 命令 ───────────────────────────────────────────────────────────
ENV = "🎮虚拟盘"


def _kb(days):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 AI 复盘诊断", callback_data=f"rsai:{days}")],
        [InlineKeyboardButton("7天", callback_data="rsd:7"),
         InlineKeyboardButton("30天", callback_data="rsd:30"),
         InlineKeyboardButton("90天", callback_data="rsd:90")],
    ])


def _empty(days):
    return (f"近 {days} 天没有虚拟盘平仓记录。\n"
            f"先用 `/vopen BTC long 1000 10` 开一单练练。")


async def rstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rstats [天数] [ai] —— 虚拟盘成绩单，加 ai 出 AI 诊断。"""
    days = DEFAULT_DAYS
    want_ai = False
    for a in (context.args or []):
        low = a.lower()
        if low in ("ai", "复盘", "诊断"):
            want_ai = True
            continue
        if low in ("v", "虚拟", "vtrade", "模拟"):
            continue        # 以前用来从实盘切到虚拟盘，现在只有虚拟盘，照旧接受别报错
        try:
            days = max(1, min(int(a), MAX_DAYS))
        except ValueError:
            pass
    uid = update.effective_user.id
    trades = load_virtual(uid, days)
    if not trades:
        await safe_reply(update.message, _empty(days), parse_mode="Markdown")
        return
    fund = virtual_funding(uid, days)
    await safe_reply(update.message, build_stats_text(trades, days, fund, ENV),
                     reply_markup=_kb(days), parse_mode="Markdown")
    if want_ai:
        await _send_ai(update.message, trades, days, fund)


async def _send_ai(message, trades, days, fund):
    from handlers.chat import _send
    try:
        await message.reply_text("🤖 AI 复盘中…")
        review = await build_ai_review(trades, days, fund)
    except Exception as e:
        log.error(f"AI 复盘出错: {e}")
        await safe_reply(message, f"AI 复盘失败：{str(e)[:100]}")
        return
    await _send(message, f"🧠 *AI 复盘诊断*（近{days}天）\n\n{review}")


# ── 按钮回调（由 menu.button_handler 分发）──────────────────────────
async def days_from_btn(query, context, days):
    uid = query.from_user.id
    trades = load_virtual(uid, days)
    if not trades:
        await safe_edit(query, _empty(days), reply_markup=_kb(days), parse_mode="Markdown")
        return
    await safe_edit(query, build_stats_text(trades, days, virtual_funding(uid, days), ENV),
                    reply_markup=_kb(days), parse_mode="Markdown")


async def ai_from_btn(query, context, days):
    uid = query.from_user.id
    trades = load_virtual(uid, days)
    if not trades:
        await query.answer("这段时间没有已平仓记录", show_alert=True)
        return
    await query.answer("AI 复盘中，稍等…")
    await _send_ai(query.message, trades, days, virtual_funding(uid, days))
