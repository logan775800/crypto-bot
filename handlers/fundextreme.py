"""资金费率极端榜 /fex —— 全市场跨所扫最极端的正/负费率（挤多 / 轧空机会）。

和 /fundingrank（OKX 十几个主流币逐个查）的区别：这里是**全市场**，且做了两件正事：
1) **按结算周期归一到日化**。ST/退市类永续常是 1h 结算 = 8× 常规抽血速度，
   只比「每期费率」会把它们看漏——那正是最容易被埋的地方，所以榜按日化排序。
2) 跨所：Bybit / Binance 都能一次请求拿全量费率（OKX 没有批量接口，逐个查代价太大，跳过）。

可订阅：|日化| 超阈值就推送。
"""
import time
import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from storage import data, save_data
from handlers.util import safe_reply, safe_edit, escape_md

log = logging.getLogger(__name__)

MIN_TURNOVER = 5_000_000     # 24h 成交额门槛：小池子费率再夸张也进不去也出不来
COOLDOWN = 3 * 3600          # 同币同方向推送冷却
DEF_THRESHOLD = 1.0          # 默认 |日化| ≥1% 才推


# ── Bybit：tickers 一次拿全量费率，instruments-info 一次拿全量结算周期 ──
async def _bybit():
    from handlers import marketdata as md
    t = await md._get("/v5/market/tickers", {"category": "linear"})
    inst = await md._get("/v5/market/instruments-info", {"category": "linear", "limit": 1000})
    iv = {}
    for x in (inst.get("list") or []):
        try:
            iv[x["symbol"]] = int(x.get("fundingInterval") or 480)   # 分钟
        except (TypeError, ValueError, KeyError):
            continue
    out = []
    for x in (t.get("list") or []):
        sym = x.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            rate = float(x.get("fundingRate") or 0) * 100
            turn = float(x.get("turnover24h") or 0)
            last = float(x.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        if turn < MIN_TURNOVER:
            continue
        out.append(_row("Bybit", sym.replace("USDT", ""), rate, iv.get(sym, 480), turn, last))
    return out


# ── Binance：premiumIndex 一次拿全量费率 + 下次结算时间 ────────────
async def _binance():
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.get("https://fapi.binance.com/fapi/v1/premiumIndex")
        r.raise_for_status()
        prem = r.json()
        r2 = await c.get("https://fapi.binance.com/fapi/v1/ticker/24hr")
        r2.raise_for_status()
        vol = {x["symbol"]: float(x.get("quoteVolume") or 0) for x in r2.json()}
        # 币安的结算周期要从 fundingInfo 拿（只列非 8h 的特例，其余默认 8h）
        iv = {}
        try:
            r3 = await c.get("https://fapi.binance.com/fapi/v1/fundingInfo")
            r3.raise_for_status()
            for x in r3.json():
                try:
                    iv[x["symbol"]] = int(float(x.get("fundingIntervalHours") or 8)) * 60
                except (TypeError, ValueError, KeyError):
                    continue
        except Exception as e:
            log.warning(f"币安 fundingInfo 取失败(按8h算): {e}")
    out = []
    for x in prem:
        sym = x.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            rate = float(x.get("lastFundingRate") or 0) * 100
            last = float(x.get("markPrice") or 0)
        except (TypeError, ValueError):
            continue
        turn = vol.get(sym, 0)
        if turn < MIN_TURNOVER:
            continue
        out.append(_row("Binance", sym.replace("USDT", ""), rate, iv.get(sym, 480), turn, last))
    return out


def _row(ex, sym, rate, mins, turn, last):
    mins = mins or 480
    return {
        "ex": ex, "sym": sym, "rate": rate, "mins": mins,
        # 归一到日化才能横向比：1h 结算的实际抽血速度是标称的 8 倍
        "daily": rate * (1440 / mins),
        "turn": turn, "price": last,
    }


async def scan_all():
    """跨所扫描。任一所挂了不影响另一所（部分数据也比没有强）。"""
    rows = []
    for name, fn in (("Bybit", _bybit), ("Binance", _binance)):
        try:
            rows.extend(await fn())
        except Exception as e:
            log.warning(f"资金费扫描 {name} 失败: {e}")
    return rows


def _hi(r):
    """1h/2h/4h 结算的高频费率是最容易被忽略的坑，明确标出来。"""
    if r["mins"] >= 480:
        return ""
    h = r["mins"] // 60
    return f" ⚠️{h}h结算"


def _line(r):
    return (f"　{escape_md(r['sym'])} `{r['rate']:+.4f}%`/期 → 日化 `{r['daily']:+.3f}%`"
            f"{_hi(r)}　_{r['ex']}_")


def build_text(rows, n=8):
    if not rows:
        return "💵 *资金费率极端榜*\n\n所有数据源都取不到，稍后再试"
    rows = sorted(rows, key=lambda x: x["daily"])
    neg = [r for r in rows if r["daily"] < 0][:n]
    pos = [r for r in rows[::-1] if r["daily"] > 0][:n]
    lines = [f"💵 *资金费率极端榜*（{len(rows)} 个永续，24h额≥${MIN_TURNOVER/1e6:g}M）",
             "_按**日化**排序——1h结算的币抽血是常规8倍，只看每期费率会看漏_", ""]
    if neg:
        lines.append("🟢 *空头付费最多*（轧空风险 / 做多收费率）")
        lines += [_line(r) for r in neg]
    if pos:
        lines.append("\n🔴 *多头付费最多*（挤多风险 / 做空收费率）")
        lines += [_line(r) for r in pos]
    hf = [r for r in neg + pos if r["mins"] < 480]
    if hf:
        lines.append(f"\n⚠️ 榜上有 {len(hf)} 个**高频结算**币（{'、'.join(escape_md(r['sym']) for r in hf[:5])}）。"
                     f"这类费率一天扣 8~24 次，扛单必被磨死——只做快进快出。")
    lines.append("\n💡 收费率≠白拿：费率极端往往意味着行情也极端。`/fexsub 1` 可订阅推送")
    lines.append("⚠️ 不构成投资建议")
    return "\n".join(lines)


def _kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 刷新", callback_data="fex")],
        [InlineKeyboardButton("订阅 |日化|≥0.5%", callback_data="fexsub:0.5"),
         InlineKeyboardButton("≥1%", callback_data="fexsub:1"),
         InlineKeyboardButton("≥2%", callback_data="fexsub:2")],
        [InlineKeyboardButton("🔕 取消订阅", callback_data="fexsub:off")],
    ])


async def fex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/fex 资金费率极端榜。"""
    await safe_reply(update.message, "💵 扫描全市场资金费率…")
    try:
        rows = await scan_all()
    except Exception as e:
        log.error(f"资金费极端榜出错: {e}")
        await safe_reply(update.message, f"扫描失败：{str(e)[:100]}")
        return
    await safe_reply(update.message, build_text(rows), reply_markup=_kb(), parse_mode="Markdown")


async def fexsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/fexsub 1 订阅；/fexsub off 取消。"""
    cid = str(update.effective_chat.id)
    subs = data.setdefault("fex_subs", {})
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("off", "关", "0"):
        subs.pop(cid, None)
        save_data()
        await safe_reply(update.message, "🔕 已取消资金费极值推送")
        return
    thr = DEF_THRESHOLD
    if arg:
        try:
            thr = float(arg)
        except ValueError:
            await safe_reply(update.message, "用法：`/fexsub 1`（|日化|≥1% 推送）或 `/fexsub off`",
                             parse_mode="Markdown")
            return
        if not (0.1 <= thr <= 20):
            await safe_reply(update.message, "阈值范围 0.1~20%")
            return
    subs[cid] = {"threshold": thr}
    save_data()
    await safe_reply(update.message,
        f"✅ 已订阅：任一永续 **|日化费率| ≥ {thr:g}%** 就推送（每小时扫，同币同向 3 小时冷却）\n"
        f"取消发 `/fexsub off`", parse_mode="Markdown")


async def scan_fex(context: ContextTypes.DEFAULT_TYPE):
    """后台 job：每小时扫一次，只推越界的。"""
    subs = data.get("fex_subs", {})
    if not subs:
        return
    try:
        rows = await scan_all()
    except Exception as e:
        log.warning(f"资金费订阅扫描失败: {e}")
        return
    if not rows:
        return
    now = time.time()
    cd = data.setdefault("fex_alerted", {})
    changed = False
    for cid, cfg in list(subs.items()):
        thr = cfg.get("threshold", DEF_THRESHOLD)
        hits = [r for r in rows if abs(r["daily"]) >= thr]
        if not hits:
            continue
        hits.sort(key=lambda x: -abs(x["daily"]))
        fresh = []
        for r in hits[:10]:
            key = f"{cid}:{r['ex']}:{r['sym']}:{'pos' if r['daily'] > 0 else 'neg'}"
            if now - cd.get(key, 0) < COOLDOWN:
                continue
            cd[key] = now
            changed = True
            fresh.append(r)
        if not fresh:
            continue
        lines = [f"💵 *资金费极值告警*（|日化| ≥ {thr:g}%）\n"]
        for r in fresh:
            side = "多头在付费 → 挤多风险" if r["daily"] > 0 else "空头在付费 → 轧空风险"
            lines.append(f"{_line(r)}\n　　{side}")
        lines.append("\n⚠️ 费率极端常伴随行情极端，收费率≠白拿。`/fexsub off` 关")
        try:
            await context.bot.send_message(chat_id=int(cid), text="\n".join(lines),
                                           parse_mode="Markdown")
        except Exception as e:
            log.error(f"资金费极值推送失败 {cid}: {e}")
    if changed:
        save_data()


# ── 按钮回调 ───────────────────────────────────────────────────────
async def fex_from_btn(query, context):
    await safe_edit(query, "💵 扫描中…")
    try:
        rows = await scan_all()
    except Exception as e:
        log.error(f"资金费榜按钮出错: {e}")
        await safe_edit(query, "扫描失败，稍后再试")
        return
    await safe_edit(query, build_text(rows), reply_markup=_kb(), parse_mode="Markdown")


async def sub_from_btn(query, context, val):
    cid = str(query.message.chat_id)
    subs = data.setdefault("fex_subs", {})
    if val == "off":
        subs.pop(cid, None)
        await query.answer("已取消订阅")
    else:
        subs[cid] = {"threshold": float(val)}
        await query.answer(f"已订阅：|日化|≥{val}%")
    save_data()
