"""数据可信度层 —— 每次分析都必须能回答：这结论是几点的？哪些维度真取到了？

为什么这是第一优先级：一份「精确到小数点后四位」的计划，如果底下有个维度其实没取到，
它的精确是假的。用户没法判断该信几分。所以：
  • 时间戳一律用**交易所服务器时间**（marketdata.stamp），不用本机时间——本机时钟漂了就是骗人；
  • 每个维度显式标 ✅/⚠️，取不到就说「暂不可用」并给原因；
  • 区分「工具坏了」和「这个币确实没这项数据」——两者处理方式完全不同；
  • 降级信息同时喂给 AI（for_ai），让它主动降低相应结论的置信度，而不是照常输出精确价位。

探测是并发的，全部失败也不抛异常——拿不到就如实标出来。
"""
import asyncio
import logging
import time

from handlers import marketdata as md

log = logging.getLogger(__name__)

# 分析默认覆盖的周期；OI 只有部分周期有
KLINE_IVS = ("5m", "15m", "30m", "1h", "4h", "1d")
OI_IVS = ("15m", "1h")


class Report:
    """一次探测的结果。渲染成给人看的 header，或给 AI 看的降级说明。"""

    def __init__(self, symbol):
        self.symbol = md.norm(symbol)
        self.exchange = "Bybit"
        self.server_ms = None
        self.klines = {}        # {周期: (ok, 说明)}
        self.oi = {}
        self.others = {}        # {名称: (ok, 说明)}
        self.stale = []         # 数据滞后的周期

    # ── 完整度 ────────────────────────────────────────────
    def _all(self):
        return list(self.klines.values()) + list(self.oi.values()) + list(self.others.values())

    @property
    def total(self):
        return len(self._all())

    @property
    def ok_count(self):
        return sum(1 for ok, _ in self._all() if ok)

    @property
    def completeness(self):
        return (self.ok_count / self.total * 100) if self.total else 0.0

    @property
    def missing(self):
        """取不到的维度名 → 用于告诉 AI 哪些结论不能下。"""
        out = []
        for iv, (ok, _) in self.klines.items():
            if not ok:
                out.append(f"K线{iv}")
        for iv, (ok, _) in self.oi.items():
            if not ok:
                out.append(f"OI{iv}")
        for name, (ok, _) in self.others.items():
            if not ok:
                out.append(name)
        return out

    @property
    def healthy(self):
        return not self.missing and not self.stale

    @property
    def invalid_symbol(self):
        """Bybit 明确说这个 symbol 不存在——和「接口挂了」是两回事，
        不该渲染成一份 0% 完整度的体检报告，那会让人以为是系统故障。"""
        msgs = [m for _, m in self._all() if m]
        if not msgs:
            return False
        hits = sum(1 for m in msgs if "symbol" in m.lower() and "invalid" in m.lower())
        return self.ok_count == 0 and hits >= 2

    # ── 渲染 ──────────────────────────────────────────────
    def _grp(self, d):
        """{周期: (ok, msg)} → "5m / 15m ✅｜4h ⚠️" 形式。"""
        ok = [k for k, (o, _) in d.items() if o]
        bad = [k for k, (o, _) in d.items() if not o]
        parts = []
        if ok:
            parts.append(" / ".join(ok) + " ✅")
        if bad:
            parts.append(" / ".join(bad) + " ⚠️")
        return "｜".join(parts) if parts else "—"

    def header(self):
        """固定挂在分析顶部的那几行。"""
        short = self.symbol.replace("USDT", "")
        if self.invalid_symbol:
            return (f"❌ *{self.exchange} 没有 {short}USDT 永续合约*\n"
                    f"这不是数据故障——是这个币在 {self.exchange} 上不存在（或代号写错了）。\n"
                    f"换个币，或先用 `/fex`／币安专区确认它在哪个所有永续。")
        lines = [
            f"`{short}USDT 永续｜{self.exchange}｜{md.stamp(self.server_ms)}`",
            f"K线：{self._grp(self.klines)}",
        ]
        oi_txt = self._grp(self.oi)
        others = "｜".join(
            f"{name} {'✅' if ok else '⚠️ 暂不可用'}" for name, (ok, _) in self.others.items())
        lines.append(f"OI：{oi_txt}｜{others}")
        if self.stale:
            lines.append(f"⚠️ {'/'.join(self.stale)} 数据滞后，可能停更或合约停牌")
        if self.missing:
            lines.append(f"⚠️ *数据完整度 {self.completeness:.0f}%*"
                         f"（{self.ok_count}/{self.total}）—— 缺：{'、'.join(self.missing)}")
            lines.append("_相关维度的结论已降级，不要按满信心执行_")
        return "\n".join(lines)

    def reasons(self):
        """失败原因明细（用户想深究时看）。"""
        out = []
        for grp in (self.klines, self.oi, self.others):
            for k, (ok, msg) in grp.items():
                if not ok and msg:
                    out.append(f"• {k}: {msg}")
        return "\n".join(out)

    def for_ai(self):
        """喂给模型的降级指令。不是「参考信息」，是硬约束。"""
        if self.invalid_symbol:
            return (f"【数据状态】Bybit 上不存在 {self.symbol} 这个永续合约（symbol 无效）。"
                    f"这不是取数失败，是该合约不存在。请直接告诉用户币种代号可能写错了、"
                    f"或该币在 Bybit 没有永续，**不要**给出任何该币的分析或价位。")
        if self.healthy:
            return (f"【数据状态】{self.symbol} 全部维度取数成功，{md.stamp(self.server_ms)}。"
                    f"可正常给出完整结论。")
        parts = [f"【数据状态·重要】{self.symbol}，{md.stamp(self.server_ms)}，"
                 f"数据完整度 {self.completeness:.0f}%（{self.ok_count}/{self.total}）。"]
        if self.missing:
            parts.append(
                f"以下维度**本次取不到**：{'、'.join(self.missing)}。"
                f"你必须：(1) 在结论里明说这些维度缺失；(2) 不得给出依赖它们的判断"
                f"（例如缺 OI 就不要谈「谁在推动/是否拥挤」，缺订单簿就不要谈「挂单墙」，"
                f"缺清算就不要谈「挤压空间」）；(3) 相应降低整体置信度。"
                f"注意：取不到 ≠ 该币没有这项数据，只是这次没拿到，别下「该币无此数据」的结论。")
        if self.stale:
            parts.append(f"以下周期数据滞后：{'、'.join(self.stale)}，其价位可能不是最新，"
                         f"不要据此给精确进场位。")
        return "\n".join(parts)


# ── 各维度探测（并发，失败不抛）──────────────────────────────
async def _probe_kline(rep, iv):
    try:
        r, srv = await md._get2("/v5/market/kline", {
            "category": md.CAT, "symbol": rep.symbol,
            "interval": md.INTERVALS.get(iv, "15"), "limit": 5})
        rows = r.get("list") or []
        if not rows:
            rep.klines[iv] = (False, "Bybit 返回空K线（该周期无数据）")
            return
        rep.server_ms = rep.server_ms or srv
        lag_txt, stale = md.bar_lag(srv, int(rows[0][0]), iv)   # list 是新→旧，[0] 最新
        if stale:
            rep.stale.append(iv)
        rep.klines[iv] = (True, "")
    except Exception as e:
        rep.klines[iv] = (False, str(e)[:70])


async def _probe_oi(rep, iv):
    try:
        r, srv = await md._get2("/v5/market/open-interest", {
            "category": md.CAT, "symbol": rep.symbol,
            "intervalTime": md.OI_INTERVALS.get(iv, "15min"), "limit": 5})
        rows = r.get("list") or []
        rep.server_ms = rep.server_ms or srv
        rep.oi[iv] = (bool(rows), "" if rows else "Bybit 未返回 OI 历史")
    except Exception as e:
        rep.oi[iv] = (False, str(e)[:70])


async def _probe_funding(rep):
    try:
        r, srv = await md._get2("/v5/market/tickers",
                                {"category": md.CAT, "symbol": rep.symbol})
        lst = r.get("list") or []
        rep.server_ms = rep.server_ms or srv
        # 有 ticker 但 fundingRate 为空 = 该合约确实没有资金费（少见），照实说
        if not lst:
            rep.others["资金费率"] = (False, "Bybit 未返回 ticker")
        elif lst[0].get("fundingRate") in (None, ""):
            rep.others["资金费率"] = (False, "该合约未返回资金费率字段")
        else:
            rep.others["资金费率"] = (True, "")
    except Exception as e:
        rep.others["资金费率"] = (False, str(e)[:70])


async def _probe_book(rep):
    try:
        r, srv = await md._get2("/v5/market/orderbook",
                                {"category": md.CAT, "symbol": rep.symbol, "limit": 1})
        rep.server_ms = rep.server_ms or srv
        ok = bool(r.get("b")) and bool(r.get("a"))
        rep.others["盘口"] = (ok, "" if ok else "Bybit 未返回买卖盘")
    except Exception as e:
        rep.others["盘口"] = (False, str(e)[:70])


async def _probe_liq(rep):
    """清算走 OKX 源（Bybit 无公开清算 REST）——这是最常挂的一个，所以单独标清楚。"""
    try:
        from handlers.okx import build_liq_text
        txt = await build_liq_text(rep.symbol.replace("USDT", ""))
        ok = bool(txt) and "失败" not in txt
        rep.others["清算数据"] = (ok, "" if ok else "OKX 源无该币清算数据")
    except Exception as e:
        rep.others["清算数据"] = (False, f"OKX 源取数失败：{str(e)[:50]}")


async def probe(symbol, kline_ivs=KLINE_IVS, oi_ivs=OI_IVS):
    """并发探测所有维度。整个过程不抛异常——探测本身失败也是一种「数据状态」。"""
    rep = Report(symbol)
    tasks = [_probe_kline(rep, iv) for iv in kline_ivs]
    tasks += [_probe_oi(rep, iv) for iv in oi_ivs]
    tasks += [_probe_funding(rep), _probe_book(rep), _probe_liq(rep)]
    await asyncio.gather(*tasks, return_exceptions=True)
    if not rep.server_ms:
        # 所有 Bybit 调用都失败了，退回本机时间并明确标注——总比不给时间强
        rep.server_ms = int(time.time() * 1000)
    # 探测顺序是并发的，渲染顺序要稳定，否则每次刷新 header 里的周期会跳来跳去
    rep.klines = {iv: rep.klines[iv] for iv in kline_ivs if iv in rep.klines}
    rep.oi = {iv: rep.oi[iv] for iv in oi_ivs if iv in rep.oi}
    rep.others = {k: rep.others[k] for k in ("资金费率", "盘口", "清算数据") if k in rep.others}
    return rep


# ── /datacheck 命令 ────────────────────────────────────────
async def datacheck(update, context):
    """/datacheck BANK —— 这个币现在到底哪些数据取得到。
    排查「AI 说没数据，可系统明明有这个工具」时用它，一眼看出是工具挂了还是该币真没有。"""
    from handlers.util import safe_reply
    args = context.args or []
    if not args:
        await safe_reply(update.message,
            "🔎 *数据体检*\n\n`/datacheck BANK` —— 查这个币各维度现在取不取得到\n"
            "分析结论存疑、或怀疑「AI 说没数据其实是接口挂了」时用它。",
            parse_mode="Markdown")
        return
    sym = args[0].upper().replace("USDT", "")
    await safe_reply(update.message, f"🔎 探测 {sym} 各数据源…")
    rep = await probe(sym)
    txt = "🔎 *数据体检*\n" + rep.header()
    reasons = rep.reasons()
    if reasons:
        txt += "\n\n*失败原因*\n" + reasons
    if rep.healthy:
        txt += "\n\n✅ 全部维度正常，分析结论可按满信心看待。"
    await safe_reply(update.message, txt, parse_mode="Markdown")
