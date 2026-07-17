"""条件触发提醒 /cond —— 价格+指标的**组合**条件，全满足才叫。

和现有告警的分工：
  /alert     单价格，到价即叫
  /rsialert  日线 RSI/均线状态切换
  /cond      任意周期的多条件与运算 —— 「BTC 跌到 60000 且 15m RSI<30 才叫我」

支持的条件（空格分隔，全部 AND）：
  <60000  >70000        价格
  rsi<30  rsi15m>70     RSI（默认 15m，可带周期前缀 rsi5m/rsi1h/rsi4h/rsi1d）
  chg1h<-3  chg15m>2    区间涨跌 %
  ema20>  ema50<        价格在 EMA 之上/之下
  vol>2                 量能倍数（最近5根/前20根均量）

数据源 Bybit 永续公开接口，复用 marketdata 的指标实现（服务端算，不丢K线给谁）。
"""
import re
import time
import logging
from telegram import Update
from telegram.ext import ContextTypes

from storage import data, save_data
from handlers.util import safe_reply, escape_md
from handlers import marketdata as md

log = logging.getLogger(__name__)

COOLDOWN = 1800          # 同一条规则触发后冷却 30 分钟
MAX_PER_CHAT = 20
VALID_IV = ("5m", "15m", "30m", "1h", "4h", "1d")

# rsi15m<30 / rsi<30
_RE_RSI = re.compile(r"^rsi(?P<iv>5m|15m|30m|1h|4h|1d)?(?P<op>[<>])(?P<val>-?\d+(?:\.\d+)?)$")
# chg1h<-3
_RE_CHG = re.compile(r"^chg(?P<iv>5m|15m|30m|1h|4h|1d)?(?P<op>[<>])(?P<val>-?\d+(?:\.\d+)?)$")
# ema20>  /  ema50<
_RE_EMA = re.compile(r"^ema(?P<n>20|50|200)(?P<op>[<>])$")
# vol>2
_RE_VOL = re.compile(r"^vol(?P<op>[<>])(?P<val>\d+(?:\.\d+)?)$")
# <60000 / >70000 / price<60000
_RE_PX = re.compile(r"^(?:price|价格)?(?P<op>[<>])(?P<val>\d+(?:\.\d+)?)$")


def parse_cond(tok):
    """一个 token → 条件 dict。看不懂返回 None（调用方负责报错，不静默吞）。"""
    t = tok.lower().replace("＜", "<").replace("＞", ">").replace(",", "")
    m = _RE_RSI.match(t)
    if m:
        return {"kind": "rsi", "iv": m["iv"] or "15m", "op": m["op"], "val": float(m["val"])}
    m = _RE_CHG.match(t)
    if m:
        return {"kind": "chg", "iv": m["iv"] or "15m", "op": m["op"], "val": float(m["val"])}
    m = _RE_EMA.match(t)
    if m:
        return {"kind": "ema", "n": int(m["n"]), "op": m["op"], "iv": "15m"}
    m = _RE_VOL.match(t)
    if m:
        return {"kind": "vol", "iv": "15m", "op": m["op"], "val": float(m["val"])}
    m = _RE_PX.match(t)
    if m:
        return {"kind": "price", "op": m["op"], "val": float(m["val"])}
    return None


def cond_text(c):
    op = c["op"]
    if c["kind"] == "price":
        return f"价格 {op} {c['val']:g}"
    if c["kind"] == "rsi":
        return f"{c['iv']} RSI {op} {c['val']:g}"
    if c["kind"] == "chg":
        return f"{c['iv']} 涨跌 {op} {c['val']:g}%"
    if c["kind"] == "ema":
        return f"价格{'上' if op == '>' else '下'}穿在 {c['iv']} EMA{c['n']} {'之上' if op == '>' else '之下'}"
    if c["kind"] == "vol":
        return f"{c['iv']} 量能 {op} {c['val']:g}x"
    return "?"


def rule_text(r):
    return f"{r['symbol']} ：" + " 且 ".join(cond_text(c) for c in r["conds"])


# ── 取数：一个币一个周期只拉一次 K 线，多个条件共用 ────────────────
async def _snapshot(symbol, ivs):
    """{周期: 指标包}。任一周期失败就跳过该周期（对应条件当作不满足）。"""
    out = {}
    for iv in ivs:
        try:
            r = await md._get("/v5/market/kline", {
                "category": "linear", "symbol": md.norm(symbol),
                "interval": md.INTERVALS.get(iv, "15"), "limit": 250})
            rows = (r.get("list") or [])[::-1]      # Bybit 新→旧，反成旧→新
            if len(rows) < 30:
                continue
            c = [float(x[4]) for x in rows]
            v = [float(x[5]) for x in rows]
            pack = {
                "close": c[-1],
                "rsi": md.rsi(c, 14),
                "ema20": md.ema(c, 20), "ema50": md.ema(c, 50), "ema200": md.ema(c, 200),
            }
            # 区间涨跌：近 20 根（≈ 一个「周期段」的体感）
            if len(c) >= 21:
                pack["chg"] = (c[-1] - c[-21]) / c[-21] * 100
            if len(v) >= 25:
                base = sum(v[-25:-5]) / 20
                pack["vol"] = (sum(v[-5:]) / 5 / base) if base > 0 else None
            out[iv] = pack
        except Exception as e:
            log.warning(f"条件告警取 {symbol} {iv} K线失败: {e}")
    return out


def _cmp(op, a, b):
    if a is None:
        return False
    return a > b if op == ">" else a < b


def eval_rule(rule, snap):
    """全部条件满足才返回 (True, 说明文本)。任一条件数据缺失 → 不触发（宁可漏也不误报）。"""
    parts = []
    for c in rule["conds"]:
        iv = c.get("iv", "15m")
        p = snap.get(iv)
        if not p:
            return False, ""
        if c["kind"] == "price":
            cur = p["close"]
        elif c["kind"] == "rsi":
            cur = p.get("rsi")
        elif c["kind"] == "chg":
            cur = p.get("chg")
        elif c["kind"] == "vol":
            cur = p.get("vol")
        elif c["kind"] == "ema":
            e = p.get(f"ema{c['n']}")
            if e is None:
                return False, ""
            if not _cmp(c["op"], p["close"], e):
                return False, ""
            parts.append(f"价格 {md.f(p['close'])} {c['op']} EMA{c['n']} {md.f(e)}")
            continue
        else:
            return False, ""
        if not _cmp(c["op"], cur, c["val"]):
            return False, ""
        unit = "%" if c["kind"] == "chg" else ("x" if c["kind"] == "vol" else "")
        label = {"price": "价格", "rsi": f"{iv} RSI", "chg": f"{iv} 涨跌",
                 "vol": f"{iv} 量能"}[c["kind"]]
        shown = md.f(cur) if c["kind"] == "price" else f"{cur:.2f}"
        parts.append(f"{label} {shown}{unit} {c['op']} {c['val']:g}{unit}")
    return True, "\n".join(f"　✓ {x}" for x in parts)


# ── 命令 ───────────────────────────────────────────────────────────
USAGE = (
    "🎯 *条件触发提醒*（多条件**同时**满足才叫你）\n\n"
    "`/cond BTC <60000 rsi15m<30`\n"
    "　跌到 60000 **且** 15m RSI 超卖才提醒 —— 不是单看价格\n\n"
    "*可用条件*（空格分隔，全部 AND）：\n"
    "`<60000` `>70000`　价格\n"
    "`rsi<30` `rsi1h>70`　RSI（默认15m，可加 5m/15m/30m/1h/4h/1d）\n"
    "`chg1h<-3` `chg15m>2`　该周期涨跌%\n"
    "`ema20>` `ema50<`　价格在 EMA 之上/之下\n"
    "`vol>2`　放量 2 倍\n\n"
    "*更多例子*\n"
    "`/cond ETH >4000 vol>2 ema20>`　放量站上均线突破\n"
    "`/cond SOL chg15m<-5 rsi15m<25`　急跌超卖，抄底候选\n\n"
    "`/conds` 我的条件提醒　`/delcond 2` 删第2条\n"
    "触发后 30 分钟冷却，可反复触发（不是一次性）。"
)


async def cond(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2:
        await safe_reply(update.message, USAGE, parse_mode="Markdown")
        return
    symbol = args[0].upper().replace("USDT", "")
    conds, bad = [], []
    for tok in args[1:]:
        c = parse_cond(tok)
        (conds if c else bad).append(c or tok)
    if bad:
        await safe_reply(update.message,
            f"❌ 看不懂这些条件：`{escape_md(' '.join(bad))}`\n\n发 /cond 看用法",
            parse_mode="Markdown")
        return

    # 币必须真在 Bybit 有永续，否则规则永远不触发、用户还以为在盯
    try:
        r = await md._get("/v5/market/tickers",
                          {"category": "linear", "symbol": md.norm(symbol)})
        if not (r.get("list") or []):
            raise RuntimeError("empty")
    except Exception:
        await safe_reply(update.message, f"❌ Bybit 没有 {symbol} 永续合约，换一个")
        return

    chat_id = update.effective_chat.id
    rules = data.setdefault("cond_alerts", [])
    if len([x for x in rules if x["chat_id"] == chat_id]) >= MAX_PER_CHAT:
        await safe_reply(update.message, f"每个会话最多 {MAX_PER_CHAT} 条条件提醒，先 /conds 删几条")
        return
    rule = {"chat_id": chat_id, "symbol": symbol, "conds": conds,
            "set_by": update.effective_user.first_name, "last_ts": 0}
    rules.append(rule)
    save_data()
    await safe_reply(update.message,
        f"✅ *条件提醒已设*\n{escape_md(rule_text(rule))}\n\n"
        f"全部满足才提醒（每2分钟检查，触发后30分钟冷却）。\n`/conds` 查看　`/delcond` 删除",
        parse_mode="Markdown")


async def conds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mine = [r for r in data.get("cond_alerts", []) if r["chat_id"] == chat_id]
    if not mine:
        await safe_reply(update.message, "还没有条件提醒。\n\n" + USAGE, parse_mode="Markdown")
        return
    lines = ["🎯 *我的条件提醒*\n"]
    for i, r in enumerate(mine, 1):
        cd = ""
        if r.get("last_ts"):
            left = COOLDOWN - (time.time() - r["last_ts"])
            if left > 0:
                cd = f"　_(冷却中 {left/60:.0f}分)_"
        lines.append(f"{i}. {escape_md(rule_text(r))}{cd}")
    lines.append("\n删除：`/delcond 2`")
    await safe_reply(update.message, "\n".join(lines), parse_mode="Markdown")


async def delcond(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rules = data.setdefault("cond_alerts", [])
    mine = [r for r in rules if r["chat_id"] == chat_id]
    if not context.args:
        await safe_reply(update.message, "用法：`/delcond 2`（序号看 /conds）\n全删：`/delcond all`",
                         parse_mode="Markdown")
        return
    if context.args[0].lower() in ("all", "全部"):
        rules[:] = [r for r in rules if r["chat_id"] != chat_id]
        save_data()
        await safe_reply(update.message, f"已删除全部 {len(mine)} 条条件提醒")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await safe_reply(update.message, "序号要是数字，看 /conds")
        return
    if not (1 <= n <= len(mine)):
        await safe_reply(update.message, f"序号超范围（你有 {len(mine)} 条），看 /conds")
        return
    target = mine[n - 1]
    rules.remove(target)
    save_data()
    await safe_reply(update.message, f"✅ 已删除：{escape_md(rule_text(target))}",
                     parse_mode="Markdown")


# ── 后台 job ───────────────────────────────────────────────────────
async def check_conds(context: ContextTypes.DEFAULT_TYPE):
    """每 2 分钟跑一遍。按 币 聚合取数，同币多规则共用一次 K 线。"""
    rules = data.get("cond_alerts", [])
    if not rules:
        return
    now = time.time()
    # 冷却中的规则不必为它取数
    live = [r for r in rules if now - r.get("last_ts", 0) >= COOLDOWN]
    if not live:
        return
    need = {}
    for r in live:
        need.setdefault(r["symbol"], set()).update(
            c.get("iv", "15m") for c in r["conds"])

    snaps = {}
    for sym, ivs in need.items():
        try:
            snaps[sym] = await _snapshot(sym, sorted(ivs))
        except Exception as e:
            log.warning(f"条件告警取数失败 {sym}: {e}")

    changed = False
    for r in live:
        snap = snaps.get(r["symbol"])
        if not snap:
            continue
        try:
            hit, detail = eval_rule(r, snap)
        except Exception as e:
            log.error(f"条件告警求值出错 {r['symbol']}: {e}")
            continue
        if not hit:
            continue
        r["last_ts"] = now
        changed = True
        try:
            await context.bot.send_message(
                chat_id=r["chat_id"],
                text=(f"🎯 *条件触发* {escape_md(r['symbol'])}\n"
                      f"{escape_md(' 且 '.join(cond_text(c) for c in r['conds']))}\n"
                      f"━━━━━━━━━━━━━━\n{detail}\n"
                      f"━━━━━━━━━━━━━━\n"
                      f"30分钟内不再重复提醒。`/conds` 管理\n"
                      f"⚠️ 不构成投资建议"),
                parse_mode="Markdown")
        except Exception as e:
            log.error(f"条件告警推送失败 {r['chat_id']}: {e}")
    if changed:
        save_data()
