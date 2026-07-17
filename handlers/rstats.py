"""实盘复盘 /rstats —— 把 Bybit 的已平仓成绩单拉下来算清楚，再交给 AI 做行为诊断。

设计要点（都是踩过的坑，改之前先看）：
1. 盈亏**不自己算**。用 /v5/position/closed-pnl 的 closedPnl（交易所权威口径，已含手续费），
   自己拿入场价出场价乘一遍必然对不上（滑点/分批/资金费）。
2. closed-pnl 的 side 是**平仓单**方向：Sell 平的是多头，Buy 平的是空头。写反多空胜率就全反了。
3. closed-pnl **不含开仓时间**，所以「持仓时长 vs 盈亏」要靠 /v5/execution/list 逐笔成交
   还原净仓位 0→非0 的时刻。顺带 execType=Funding 的 execFee 就是真实资金费支出。
4. 两个接口都有 **单次窗口 ≤7 天** 的硬限制 → 按 7 天切片 + cursor 翻页。

只读，不会下任何单。管理员 + 私聊。
"""
import time
import logging
import bisect
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.util import safe_reply, safe_edit, escape_md
from bybit_trade import BybitError

log = logging.getLogger(__name__)

CHUNK_MS = 7 * 86400 * 1000   # Bybit 单次查询窗口上限
MAX_PAGES = 40                # 每个切片的翻页上限，防异常时无限翻
DEFAULT_DAYS = 30
MAX_DAYS = 180


# ── 拉数（切片 + 翻页）──────────────────────────────────────────────
async def _paged(fetch, start_ms, end_ms):
    """按 7 天切片 + cursor 翻页拉全量。fetch(start, end, cursor) -> result dict。"""
    out = []
    t = start_ms
    while t < end_ms:
        seg_end = min(t + CHUNK_MS, end_ms)
        cursor = None
        for _ in range(MAX_PAGES):
            r = await fetch(t, seg_end, cursor)
            rows = r.get("list") or []
            out.extend(rows)
            cursor = r.get("nextPageCursor")
            if not cursor or not rows:
                break
        t = seg_end
    return out


async def fetch_closed(client, days):
    end = int(time.time() * 1000)
    start = end - int(days) * 86400 * 1000
    rows = await _paged(
        lambda s, e, c: client.closed_pnl(start_ms=s, end_ms=e, cursor=c), start, end)
    # 同一笔可能在切片边界重复拉到，按 orderId+updatedTime 去重
    seen, uniq = set(), []
    for r in rows:
        k = (r.get("orderId"), r.get("updatedTime"), r.get("closedSize"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    uniq.sort(key=lambda x: int(x.get("updatedTime") or 0))
    return uniq


async def fetch_execs(client, days):
    end = int(time.time() * 1000)
    start = end - int(days) * 86400 * 1000
    rows = await _paged(
        lambda s, e, c: client.executions(start_ms=s, end_ms=e, cursor=c), start, end)
    seen, uniq = set(), []
    for r in rows:
        k = r.get("execId")
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        uniq.append(r)
    return uniq


# ── 规整 ───────────────────────────────────────────────────────────
def norm_trade(r):
    """closed-pnl 一行 → 统一结构。side 取的是**持仓**方向，不是平仓单方向。"""
    closing = r.get("side")
    return {
        "symbol": r.get("symbol", "?"),
        "side": "long" if closing == "Sell" else "short",
        "pnl": float(r.get("closedPnl") or 0),
        "entry": float(r.get("avgEntryPrice") or 0),
        "exit": float(r.get("avgExitPrice") or 0),
        "qty": float(r.get("closedSize") or 0),
        "value": float(r.get("cumEntryValue") or 0),
        "lev": float(r.get("leverage") or 0),
        "ts": int(r.get("updatedTime") or r.get("createdTime") or 0),
    }


def open_times(execs):
    """按币还原「从空仓开出新仓」的时刻列表（升序）。
    execution 只有单笔方向，这里自己累计净仓位：0 → 非0 即为一次开仓。
    仅在单向持仓(positionIdx=0)下成立——本 bot 全程用单向模式。"""
    by, net = {}, {}
    for e in sorted(execs, key=lambda x: int(x.get("execTime") or 0)):
        if e.get("execType") != "Trade":
            continue          # Funding/Settle 不动仓位
        sym = e.get("symbol")
        q = float(e.get("execQty") or 0)
        if q <= 0 or not sym:
            continue
        d = q if e.get("side") == "Buy" else -q
        prev = net.get(sym, 0.0)
        cur = prev + d
        if abs(prev) < 1e-12 and abs(cur) > 1e-12:
            by.setdefault(sym, []).append(int(e.get("execTime") or 0))
        # 浮点累计会留下 1e-15 级残渣，夹掉否则永远认为还有仓
        net[sym] = 0.0 if abs(cur) < 1e-9 else cur
    return by


def attach_duration(trades, opens):
    """给每笔平仓配上持仓时长：取该币在平仓时刻之前、最近的一次开仓时间。"""
    for t in trades:
        lst = opens.get(t["symbol"])
        if not lst:
            t["dur"] = None
            continue
        i = bisect.bisect_right(lst, t["ts"]) - 1
        t["dur"] = (t["ts"] - lst[i]) / 1000.0 if i >= 0 else None
    return trades


def funding_cost(execs):
    """资金费净支出 {币: USDT}。Bybit 里 execFee 为正 = 你付钱，为负 = 你收钱。"""
    tot = {}
    for e in execs:
        if e.get("execType") != "Funding":
            continue
        sym = e.get("symbol")
        if not sym:
            continue
        tot[sym] = tot.get(sym, 0.0) + float(e.get("execFee") or 0)
    return tot


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
        return (f"📊 *实盘复盘* {env}\n\n近 {days} 天没有已平仓记录。\n"
                f"（Bybit 只保留近 2 年数据；刚换账户/刚开始交易是正常的）")
    rr_txt = "∞（还没亏过）" if s["rr"] == float("inf") else f"{s['rr']:.2f}"
    exp_emoji = "🟢" if s["expectancy"] > 0 else "🔴"
    lines = [
        f"📊 *实盘复盘* 近{days}天 {env}",
        "━━━━━━━━━━━━━━",
        f"总盈亏 *{_money(s['total'])}* USDT｜{s['n']} 笔",
        f"胜率 {s['win_rate']:.1f}%（{s['wins']}胜 {s['losses']}负）",
        f"盈亏比 {rr_txt}（均盈 {s['avg_win']:,.2f} / 均亏 {s['avg_loss']:,.2f}）",
        f"{exp_emoji} 期望值 *{_money(s['expectancy'])}* /笔　← 这个是正的才算有系统",
        f"最大回撤 -{s['max_dd']:,.2f}｜最长连亏 {s['max_loss_streak']} 笔"
        + (f"（当前正连亏 {s['cur_loss_streak']} 笔 ⚠️）" if s["cur_loss_streak"] >= 3 else ""),
        f"最赚一笔 {_money(s['best'])}｜最亏一笔 {_money(s['worst'])}",
    ]

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

    lines.append("\n⚠️ 数据直读 Bybit，不构成投资建议")
    return "\n".join(lines)


# ── AI 复盘 ────────────────────────────────────────────────────────
AI_SYSTEM = (
    "你是一个加密永续合约交易者的复盘教练。用户给你的是他**真实账户**的成绩单统计。"
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
    """给 AI 的紧凑摘要——不丢原始几百笔，只丢统计结论（省 token 且模型不会算错）。"""
    s = compute_stats(trades)
    if not s:
        return None
    rr = "无穷(未亏过)" if s["rr"] == float("inf") else f"{s['rr']:.2f}"
    out = [
        f"统计窗口: 近{days}天, 共{s['n']}笔已平仓",
        f"总盈亏 {s['total']:+.2f} USDT, 胜率 {s['win_rate']:.1f}% ({s['wins']}胜{s['losses']}负)",
        f"均盈 {s['avg_win']:.2f} / 均亏 {s['avg_loss']:.2f}, 盈亏比 {rr}",
        f"每笔期望值 {s['expectancy']:+.2f} USDT",
        f"最大回撤 {s['max_dd']:.2f}, 最长连亏 {s['max_loss_streak']}笔, 当前连亏 {s['cur_loss_streak']}笔",
        f"最赚一笔 {s['best']:+.2f}, 最亏一笔 {s['worst']:+.2f}",
    ]

    def _dump(title, rows, limit=8):
        if not rows:
            return
        out.append(title)
        for k, n, p, wr in rows[:limit]:
            out.append(f"  {k}: {p:+.2f} USDT, {n}笔, 胜率{wr:.0f}%")

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
            out.append(f"资金费净{'支出' if tf > 0 else '收入'} {abs(tf):.2f} USDT")
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
        [{"role": "user", "content": f"这是我真实账户的交易成绩单，帮我复盘：\n\n{digest}"}],
        system=AI_SYSTEM)
    return reply


# ── 命令 ───────────────────────────────────────────────────────────
async def _load(days):
    """拉齐一次复盘所需的全部数据。执行明细拉失败不致命（只是没有时长/资金费）。"""
    from handlers.rtrade import _client
    client = _client()
    closed = await fetch_closed(client, days)
    trades = [norm_trade(r) for r in closed]
    fund = None
    try:
        execs = await fetch_execs(client, days)
        attach_duration(trades, open_times(execs))
        fund = funding_cost(execs)
    except Exception as e:
        log.warning(f"复盘拉成交明细失败(降级：无持仓时长/资金费): {e}")
    return trades, fund


def _kb(days):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 AI 复盘诊断", callback_data=f"rsai:{days}")],
        [InlineKeyboardButton("7天", callback_data="rsd:7"),
         InlineKeyboardButton("30天", callback_data="rsd:30"),
         InlineKeyboardButton("90天", callback_data="rsd:90")],
        [InlineKeyboardButton("🎛 交易台", callback_data="tpanel")],
    ])


async def rstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rstats [天数] —— 实盘成绩单。加 ai 直接出 AI 诊断：/rstats 30 ai"""
    from handlers.rtrade import _guard, _env_tag
    if not await _guard(update):
        return
    days = DEFAULT_DAYS
    want_ai = False
    for a in (context.args or []):
        low = a.lower()
        if low in ("ai", "复盘", "诊断"):
            want_ai = True
            continue
        try:
            days = max(1, min(int(a), MAX_DAYS))
        except ValueError:
            pass
    await safe_reply(update.message,
        f"📊 正在拉取近 {days} 天实盘记录…（按 7 天切片翻页，稍等）")
    try:
        trades, fund = await _load(days)
    except RuntimeError:
        await safe_reply(update.message, "❌ 未配置 BYBIT API 密钥")
        return
    except BybitError as e:
        await safe_reply(update.message,
            f"❌ 查询被拒：[{e.ret_code}] {e.ret_msg}\n"
            f"（只读权限即可，若报权限不足请给 key 勾上「持仓/订单 读取」）")
        return
    except Exception as e:
        log.error(f"rstats 拉数出错: {e}")
        await safe_reply(update.message, f"❌ 拉取失败：{str(e)[:120]}")
        return

    text = build_stats_text(trades, days, fund, _env_tag())
    await safe_reply(update.message, text, reply_markup=_kb(days), parse_mode="Markdown")
    if want_ai and trades:
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
    from handlers.rtrade import _btn_admin_ok, _env_tag
    if not _btn_admin_ok(query):
        await query.answer("仅管理员", show_alert=True)
        return
    await safe_edit(query, f"📊 拉取近 {days} 天…")
    try:
        trades, fund = await _load(days)
    except RuntimeError:
        await safe_edit(query, "❌ 未配置 BYBIT API 密钥")
        return
    except Exception as e:
        log.error(f"rstats 按钮拉数出错: {e}")
        await safe_edit(query, f"❌ 拉取失败：{str(e)[:120]}")
        return
    await safe_edit(query, build_stats_text(trades, days, fund, _env_tag()),
                    reply_markup=_kb(days), parse_mode="Markdown")


async def ai_from_btn(query, context, days):
    from handlers.rtrade import _btn_admin_ok
    if not _btn_admin_ok(query):
        await query.answer("仅管理员", show_alert=True)
        return
    await query.answer("AI 复盘中，稍等…")
    try:
        trades, fund = await _load(days)
    except Exception as e:
        log.error(f"AI 复盘按钮拉数出错: {e}")
        await query.answer("拉取失败", show_alert=True)
        return
    if not trades:
        await query.answer("这段时间没有已平仓记录", show_alert=True)
        return
    await _send_ai(query.message, trades, days, fund)
