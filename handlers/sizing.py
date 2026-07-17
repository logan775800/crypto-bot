"""风险反推仓位 —— 回答「这个止损下，我到底能开多少」，而不是甩个公式让人自己算。

核心恒等式（先想清楚再改）：
    单笔最大亏损 = 名义仓位 × 止损距离%
  ⇒ 名义仓位 = (权益 × 风险%) / 止损距离%
杠杆**不影响**这个名义仓位，只决定要占多少保证金 —— 这是最多人搞反的地方：
以为「加杠杆 = 加风险」。真正决定风险的是**名义 × 止损距离**；杠杆只决定
保证金占用和爆仓价离你多远。所以下面把「建议名义」和「各杠杆下的保证金」分开列。

同向暴露检查：单笔算得再对，5 个仓全同向照样一起死，所以要把已有仓位算进去。
"""
import logging

from handlers.util import safe_reply, safe_edit
from handlers import marketdata as md

log = logging.getLogger(__name__)

TIERS = [("保守", 0.25), ("常规", 0.5), ("进攻", 1.0)]
LEVS = (3, 5, 10, 20)
MAJORS = ("BTCUSDT", "ETHUSDT")


def plan_size(equity, entry, stop, risk_pct):
    """纯计算，不碰网络不碰账户 —— 方便测，也方便别处复用。

    返回 dict；入场=止损、或方向不合法时返回 None（调用方给友好提示）。"""
    if not (equity > 0 and entry > 0 and stop > 0 and risk_pct > 0):
        return None
    dist = abs(entry - stop)
    if dist == 0:
        return None
    dist_pct = dist / entry * 100
    risk_usdt = equity * risk_pct / 100
    notional = risk_usdt / (dist_pct / 100)      # = risk_usdt * entry / dist
    qty = notional / entry
    return {
        "side": "long" if stop < entry else "short",
        "entry": entry, "stop": stop,
        "dist": dist, "dist_pct": dist_pct,
        "risk_pct": risk_pct, "risk_usdt": risk_usdt,
        "notional": notional, "qty": qty,
        "equity": equity,
        # 触及止损后账户回撤就等于风险% —— 写出来是为了让人对这个数字有实感
        "dd_pct": risk_pct,
        "margins": {lev: notional / lev for lev in LEVS},
    }


def exposure(positions, side=None):
    """已有仓位的名义暴露。side 传 long/short 只算同向的。"""
    tot = 0.0
    same = 0.0
    alt = 0.0
    for p in positions or []:
        try:
            v = float(p.get("positionValue") or 0)
        except (TypeError, ValueError):
            continue
        tot += v
        pside = "long" if p.get("side") == "Buy" else "short"
        if side and pside == side:
            same += v
            if p.get("symbol") not in MAJORS:
                alt += v
    return {"total": tot, "same_side": same, "same_side_alt": alt}


def build_text(s, exp=None, symbol=None, env=""):
    """把计算结果渲染成一屏能看完的卡。"""
    if not s:
        return ("❌ 算不了：入场价和止损价不能相同，且都要大于 0。\n"
                "用法 `/risk 0.081 0.0828 0.5%`（入场 止损 风险%）")
    dir_txt = "做多 📈" if s["side"] == "long" else "做空 📉"
    head = f"🧮 *仓位计算*" + (f" {symbol}" if symbol else "") + (f" {env}" if env else "")
    lines = [
        head,
        f"{dir_txt}｜入场 {md.f(s['entry'])} → 止损 {md.f(s['stop'])}",
        f"止损距离 *{s['dist_pct']:.2f}%*（{md.f(s['dist'])}）",
        "━━━━━━━━━━━━━━",
        f"总权益　　　{s['equity']:,.2f} USDT",
        f"本单计划风险 *{s['risk_pct']:g}%* = *{s['risk_usdt']:,.2f}* USDT",
        f"建议最大名义 *{s['notional']:,.2f}* USDT",
        f"　≈ {md.f(s['qty'])} 张/个",
        "",
        "*各杠杆所需保证金*（名义不变，杠杆只决定占用和爆仓距离）",
    ]
    for lev, m in s["margins"].items():
        warn = ""
        if m > s["equity"]:
            warn = "　❌ 超过总权益，做不了"
        elif m > s["equity"] * 0.5:
            warn = "　⚠️ 占用过半权益"
        lines.append(f"　{lev}x → {m:,.2f} USDT{warn}")
    lines.append("")
    lines.append(f"触及止损 → 账户回撤 *-{s['dd_pct']:g}%*（{s['risk_usdt']:,.2f} USDT）")

    if exp is not None:
        lines.append("━━━━━━━━━━━━━━")
        eq = s["equity"]
        cur = exp["same_side"]
        after = cur + s["notional"]
        lines.append(f"*同向暴露检查*")
        lines.append(f"　已有同向名义　{cur:,.0f} USDT（权益 {cur/eq*100:.0f}%）"
                     if eq else f"　已有同向名义 {cur:,.0f}")
        lines.append(f"　本单后合计　　{after:,.0f} USDT（权益 {after/eq*100:.0f}%）"
                     if eq else f"　本单后合计 {after:,.0f}")
        if exp["same_side_alt"] > 0 and cur > 0:
            lines.append(f"　其中山寨 {exp['same_side_alt']/cur*100:.0f}% —— "
                         f"BTC 一破位会一起走，等于放大版单一仓位")
        if eq and after / eq > 3:
            lines.append("　⚠️ 同向名义已超权益 3 倍，单边行情打脸时回撤会远超本单的计划风险")
    lines.append("\n⚠️ 只是按你给的止损反推，不构成投资建议")
    return "\n".join(lines)


def _kb(entry, stop, symbol=None):
    """风险档位按钮。价格编进 callback_data 里，重算不用重新输入。"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    sym = symbol or "-"
    row = [InlineKeyboardButton(f"{name} {pct}%",
                                callback_data=f"sz:{sym}:{entry:g}:{stop:g}:{pct}")
           for name, pct in TIERS]
    return InlineKeyboardMarkup([row,
        [InlineKeyboardButton("🎛 交易台", callback_data="tpanel"),
         InlineKeyboardButton("🛡 风险守护", callback_data="rgpanel")]])


async def _account(update_or_query):
    """(权益, 持仓)。没配密钥/非管理员 → (None, None)，调用方退回手输权益。"""
    from config import is_admin
    uid = None
    try:
        uid = (update_or_query.from_user or update_or_query.effective_user).id
    except AttributeError:
        pass
    if uid is not None and not is_admin(uid):
        return None, None
    try:
        from handlers.rtrade import _client
        c = _client()
        bal = await c.wallet_balance("USDT")
        pos = await c.positions_all()
        return float(bal.get("totalEquity") or 0), pos
    except RuntimeError:
        return None, None          # 没配密钥
    except Exception as e:
        log.warning(f"仓位计算取账户失败: {e}")
        return None, None


def parse_args(args):
    """`0.081 0.0828 0.5%` / `BANK 0.081 0.0828 0.5%` / `0.081 0.0828`（默认0.5%）
    → (symbol|None, entry, stop, risk_pct)；看不懂返回 None。"""
    if not args:
        return None
    a = list(args)
    symbol = None
    # 第一个不像数字就当币名
    try:
        float(a[0].replace(",", ""))
    except ValueError:
        symbol = a[0].upper().replace("USDT", "")
        a = a[1:]
    if len(a) < 2:
        return None
    try:
        entry = float(a[0].replace(",", ""))
        stop = float(a[1].replace(",", ""))
    except ValueError:
        return None
    risk = 0.5
    if len(a) >= 3:
        try:
            risk = float(a[2].replace("%", "").replace("％", ""))
        except ValueError:
            return None
    if not (0 < risk <= 100):
        return None
    return symbol, entry, stop, risk


USAGE = (
    "🧮 *风险反推仓位* —— 「这个止损下我能开多少」\n\n"
    "`/risk 0.081 0.0828`　入场 止损（默认风险 0.5%）\n"
    "`/risk 0.081 0.0828 0.5%`　指定风险\n"
    "`/risk BANK 0.081 0.0828 0.5%`　带币名（会一并查同向暴露）\n\n"
    "已配 Bybit 只读密钥时自动读你的真实权益；没配就先发 `/risk` 看这条说明。\n"
    "会给出：止损距离、本单风险 USDT、建议最大名义、各杠杆所需保证金、"
    "触及止损的账户回撤、以及**同向暴露**检查。\n\n"
    "不带参数的 `/risk` = 风险守护面板。"
)


async def size_cmd(update, context, parsed):
    """/risk 带参数时走这里（不带参数是风险守护面板，见 riskguard.risk）。"""
    symbol, entry, stop, risk = parsed
    equity, pos = await _account(update)
    if not equity:
        await safe_reply(update.message,
            "🧮 需要账户权益才能反推仓位，但拿不到（未配 Bybit 密钥 / 非管理员）。\n\n"
            "先手动算：\n"
            f"　止损距离 = |{md.f(entry)} - {md.f(stop)}| / {md.f(entry)} "
            f"= *{abs(entry-stop)/entry*100:.2f}%*\n"
            f"　建议名义 = 权益 × {risk:g}% ÷ {abs(entry-stop)/entry*100:.2f}%\n"
            f"　（例：权益 10000 → 名义 {10000*risk/100/(abs(entry-stop)/entry):,.0f} USDT）\n\n"
            "配好只读密钥后这里会自动用你的真实权益。",
            parse_mode="Markdown")
        return
    s = plan_size(equity, entry, stop, risk)
    exp = exposure(pos, s["side"]) if s else None
    from handlers.rtrade import _env_tag
    await safe_reply(update.message, build_text(s, exp, symbol, _env_tag()),
                     reply_markup=_kb(entry, stop, symbol), parse_mode="Markdown")


async def from_btn(query, context, symbol, entry, stop, risk):
    """风险档位按钮：换个风险%重算，不用重新输价格。"""
    from handlers.rtrade import _btn_admin_ok, _env_tag
    if not _btn_admin_ok(query):
        await query.answer("仅管理员", show_alert=True)
        return
    equity, pos = await _account(query)
    if not equity:
        await query.answer("拿不到账户权益（未配密钥？）", show_alert=True)
        return
    s = plan_size(equity, entry, stop, risk)
    if not s:
        await query.answer("入场价和止损价不能相同", show_alert=True)
        return
    exp = exposure(pos, s["side"])
    sym = None if symbol == "-" else symbol
    await safe_edit(query, build_text(s, exp, sym, _env_tag()),
                    reply_markup=_kb(entry, stop, symbol), parse_mode="Markdown")
