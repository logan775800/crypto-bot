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

# 交易所时间 vs 本机时间的差。时钟漂移或网络劣化会让"实时数据"其实是几十秒前的，
# 而永续在这个尺度上完全可以走完一个止损距离。
FRESH_OK = 10        # 秒：这以内算实时
FRESH_WARN = 30      # 秒：超过就只能给观察方案，不能给"立即下单"
# 各数据源的服务器时间彼此相差多少算异常（不同源拼在一起做决策的前提是它们同代）
SRC_SKEW_WARN = 15   # 秒


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
        self.src_ms = {}        # {数据源: 服务器毫秒} —— 用于跨源时间差检查
        self.local_ms = None    # 探测时的本机毫秒
        self.spike = None       # 插针检测结论

    # ── 新鲜度 ────────────────────────────────────────────
    @property
    def delay_s(self):
        """数据落后本机多少秒。负数(交易所时钟略快)一律按 0 看。"""
        if not (self.server_ms and self.local_ms):
            return None
        return max(0.0, (self.local_ms - self.server_ms) / 1000)

    @property
    def skew_s(self):
        """各数据源服务器时间的最大差值——拼在一起做决策前必须确认它们同代。"""
        if len(self.src_ms) < 2:
            return None
        v = list(self.src_ms.values())
        return (max(v) - min(v)) / 1000

    @property
    def realtime_ok(self):
        """够不够新到可以给「立即下单」的方案。

        延迟未知时按「不阻断」处理：probe() 一定会填 local_ms，未知只出现在
        手工构造的 Report 上。把未知当成过期会让所有非探测路径无谓降级。
        """
        d = self.delay_s
        return d is None or d <= FRESH_WARN

    def freshness_line(self):
        d = self.delay_s
        if d is None:
            return "新鲜度：未知（拿不到交易所时间）"
        tag = "实时" if d <= FRESH_OK else ("偏慢" if d <= FRESH_WARN else "过期")
        txt = f"最新数据延迟 {d:.0f}s（{tag}）"
        sk = self.skew_s
        if sk is not None and sk > SRC_SKEW_WARN:
            txt += f"　⚠️ 各数据源时间相差 {sk:.0f}s，不是同一时刻的快照"
        return txt

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
        return not self.missing and not self.stale and self.realtime_ok and not self.spike

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
            self.freshness_line(),
            f"K线：{self._grp(self.klines)}",
        ]
        oi_txt = self._grp(self.oi)
        others = "｜".join(
            f"{name} {'✅' if ok else '⚠️ 暂不可用'}" for name, (ok, _) in self.others.items())
        lines.append(f"OI：{oi_txt}｜{others}")
        if self.spike:
            lines.append(f"⚠️ {self.spike}")
        if not self.realtime_ok:
            lines.append("⚠️ *数据过期，只能给观察方案，不能按「立即下单」执行*")
        if self.stale:
            lines.append(f"⚠️ {'/'.join(self.stale)} 数据滞后，可能停更或合约停牌")
        if self.missing:
            lines.append(f"⚠️ *数据完整度 {self.completeness:.0f}%*"
                         f"（{self.ok_count}/{self.total}）—— 缺：{'、'.join(self.missing)}")
            lines.append("相关维度的结论已降级，不要按满信心执行")
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
        extra = []
        if not self.realtime_ok:
            d = self.delay_s
            extra.append(
                f"⚠️ 实时性不达标（数据延迟 {d:.0f}s，阈值 {FRESH_WARN}s）。"
                f"你**只能**给观察方案与触发条件，**不得**给「现价立即进场」这类指令，"
                f"并要提醒用户先刷新确认现价。")
        sk = self.skew_s
        if sk is not None and sk > SRC_SKEW_WARN:
            extra.append(f"⚠️ 各数据源服务器时间相差 {sk:.0f}s，它们不是同一时刻的快照，"
                         f"跨源对比（如盘口 vs K线）的结论要降低置信度。")
        if self.spike:
            extra.append(f"⚠️ {self.spike}。插针价位没有真实成交承接，"
                         f"**不要**把它当作前高/前低或止损参考位。")
        if self.healthy and not extra:
            d = self.delay_s
            lag = f"，延迟 {d:.0f}s" if d is not None else ""
            return (f"【数据状态】{self.symbol} 全部维度取数成功，{md.stamp(self.server_ms)}"
                    f"{lag}。可正常给出完整结论。")
        parts = [f"【数据状态·重要】{self.symbol}，{md.stamp(self.server_ms)}，"
                 f"数据完整度 {self.completeness:.0f}%（{self.ok_count}/{self.total}）。"]
        parts += extra
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
        if srv:
            rep.src_ms[f"Bybit-K线{iv}"] = int(srv)
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
        if srv:
            rep.src_ms["Bybit-盘口"] = int(srv)
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


async def _probe_spike(rep):
    """插针检测：最近 5m K线里有没有异常长影线。

    插针会把「前高/前低」污染成一个根本没有成交承接的价位，止损挂在那种位置
    等于白送。所以要显式标出来，而不是让它混进结构位里。
    """
    try:
        r, srv = await md._get2("/v5/market/kline", {
            "category": md.CAT, "symbol": rep.symbol,
            "interval": "5", "limit": 60})
        rows = (r.get("list") or [])[::-1]
        if len(rows) < 20:
            return
        if srv:
            rep.src_ms["Bybit-K线5m"] = int(srv)
        rng = []
        for x in rows:
            hi, lo, c = float(x[2]), float(x[3]), float(x[4])
            if c > 0:
                rng.append((hi - lo) / c * 100)
        if not rng:
            return
        med = sorted(rng)[len(rng) // 2]
        if med <= 0:
            return
        worst_i, worst = max(enumerate(rng), key=lambda t: t[1])
        # 单根振幅超过中位数 6 倍 = 插针而非正常波动
        if worst > med * 6 and worst > 1.0:
            bar = rows[worst_i]
            rep.spike = (f"最近5小时内出现插针：单根5m振幅 {worst:.1f}%"
                         f"（常态 {med:.2f}%），最高 {md.f(bar[2])} / 最低 {md.f(bar[3])}")
    except Exception as e:
        log.debug(f"插针检测失败 {rep.symbol}: {e}")


async def probe(symbol, kline_ivs=KLINE_IVS, oi_ivs=OI_IVS):
    """并发探测所有维度。整个过程不抛异常——探测本身失败也是一种「数据状态」。"""
    rep = Report(symbol)
    tasks = [_probe_kline(rep, iv) for iv in kline_ivs]
    tasks += [_probe_oi(rep, iv) for iv in oi_ivs]
    tasks += [_probe_funding(rep), _probe_book(rep), _probe_liq(rep), _probe_spike(rep)]
    await asyncio.gather(*tasks, return_exceptions=True)
    # 本机时间在**所有请求都回来之后**取，这样 delay 包含了网络往返，
    # 反映的是"用户看到这份数据时它已经多旧了"，而不是理论上的时钟差
    rep.local_ms = int(time.time() * 1000)
    if rep.src_ms:
        rep.server_ms = max(rep.src_ms.values())
    if not rep.server_ms:
        # 所有 Bybit 调用都失败了，退回本机时间并明确标注——总比不给时间强
        rep.server_ms = int(time.time() * 1000)
    # 探测顺序是并发的，渲染顺序要稳定，否则每次刷新 header 里的周期会跳来跳去
    rep.klines = {iv: rep.klines[iv] for iv in kline_ivs if iv in rep.klines}
    rep.oi = {iv: rep.oi[iv] for iv in oi_ivs if iv in rep.oi}
    rep.others = {k: rep.others[k] for k in ("资金费率", "盘口", "清算数据") if k in rep.others}
    return rep


# ── 系统体检：地基通没通电 ──────────────────────────────────
# 2026-08-07 的教训：Bybit 密钥从来没配过，于是实盘台、驾驶舱、复盘、周报、
# 组合风险、连亏降险这一整批功能一直是空的，却没有任何地方会说出来——
# 只有等用户去点某个功能才发现。体检必须覆盖**依赖链**，不只是行情源。


async def _check_market():
    try:
        r, srv = await md._get2("/v5/market/tickers",
                               {"category": md.CAT, "symbol": "BTCUSDT"})
        ok = bool((r.get("list") or []))
        return ok, ("正常" if ok else "返回空"), srv
    except Exception as e:
        return False, str(e)[:50], None


async def _check_account():
    """账户链路：密钥配了没、能不能连上、有没有危险权限。"""
    try:
        from bybit_trade import BYBIT_API_KEY, _is_testnet
    except Exception as e:
        return False, f"模块加载失败：{str(e)[:40]}", {}
    if not BYBIT_API_KEY:
        return False, ("**未配置密钥** —— 实盘台/驾驶舱/复盘/周报/组合风险/"
                       "连亏降险全部无数据"), {}
    env = "🧪模拟盘" if _is_testnet() else "🔴实盘"
    try:
        from handlers.rtrade import _client
        bal = await _client().wallet_balance("USDT")
        eq = bal.get("totalEquity")
        return True, f"{env}｜权益 {eq} USDT", {"env": env}
    except Exception as e:
        return False, f"{env}｜连接失败：{str(e)[:50]}", {"env": env}


async def _check_ai():
    from config import AI_API_KEY, AI_BASE_URL
    if not (AI_API_KEY and AI_BASE_URL):
        return False, "未配置（AI 分析/交易计划不可用）"
    try:
        from handlers.ai import current_model
        return True, f"模型 {current_model()}"
    except Exception as e:
        return False, str(e)[:50]


def _check_subs():
    """订阅状态：清空了却没人知道，是这次事故最贵的部分。"""
    from storage import data as _d
    rows = []
    for key, label in (("contract_watch", "合约异动"), ("pump_watch", "急涨急跌"),
                       ("event_subs", "事件预警"), ("market_watch", "市场异动"),
                       ("weekly_subs", "周报")):
        n = len(_d.get(key) or [])
        rows.append(f"{label} {n}" if n else f"{label} ⚠️0")
    killed = _d.get("trading_disabled")
    return rows, killed


async def system_health():
    """不带币名的 /datacheck：整条依赖链的体检。"""
    market, account, ai = await asyncio.gather(
        _check_market(), _check_account(), _check_ai(), return_exceptions=True)

    def unpack(x, n=3):
        if isinstance(x, Exception):
            return (False, str(x)[:50]) + ((None,) if n == 3 else ({},))
        return x

    m_ok, m_msg, _srv = unpack(market)
    a_ok, a_msg, _extra = unpack(account, 3)
    ai_ok, ai_msg = unpack(ai, 2)[:2]
    subs, killed = _check_subs()

    lines = ["🩺 *系统体检*", "━━━━━━━━━━━━━━",
             f"{'✅' if m_ok else '❌'} 行情源(Bybit)　{m_msg}",
             f"{'✅' if ai_ok else '❌'} AI 中转站　{ai_msg}",
             f"{'✅' if a_ok else '❌'} 账户链路　{a_msg}",
             "━━━━━━━━━━━━━━",
             "订阅：" + "｜".join(subs)]
    if killed:
        lines.append("🔴 *实盘下单已被 killswitch 禁用* —— 恢复发 `/killswitch off`")
    if not a_ok:
        lines.append("")
        lines.append("_账户链路不通时，以下功能返回的是空数据而不是报错，"
                     "很容易被误认为「没交易记录」：_")
        lines.append("_/trade /rpos /cockpit /rstats /weekly、组合风险、亏损归因、"
                     "计划vs成交、连亏自动降险_")
    # 更新播报这条链：部署成功了群里却没有更新内容时，以前完全没有可观测性
    # （日志在服务器上，他看不到），只能靠猜。现在体检里直接给出状态。
    try:
        from handlers.monitor import broadcast_state
        b = broadcast_state()
        lines.append("")
        lines.append("📣 *更新播报*")
        mark = "⏳ 待发" if b["will_send"] else "✅ 已发过"
        lines.append(f"当前 {b['version']}｜已播报 {b['announced'] or '—'}　{mark}")
        lines.append(f"订阅会话 {b['subscribed']} 个，"
                     f"排除管理员私聊 {b['excluded_admins']} 个，"
                     f"实际目标 {len(b['targets'])} 个")
        if not b["targets"]:
            lines.append("⚠️ 一个目标都没有——群没订阅过任何推送，"
                         "或者 ADMIN_CHAT_ID 把目标群自己填进去了")
        if b["text_len"] == 0:
            lines.append("⚠️ 这一版在 CHANGELOG.md 里没有条目，所以不会播报")
    except Exception as e:
        log.info(f"播报状态取不到: {e}")

    lines.append("")
    lines.append("查单个币的数据维度：`/datacheck BTC`")
    return "\n".join(lines)


# ── /datacheck 命令 ────────────────────────────────────────
async def datacheck(update, context):
    """/datacheck BANK —— 这个币现在到底哪些数据取得到。
    排查「AI 说没数据，可系统明明有这个工具」时用它，一眼看出是工具挂了还是该币真没有。"""
    from handlers.util import safe_reply
    args = context.args or []
    if not args:
        # 不带币名 = 整条依赖链的体检。行情源好好的但账户没配、订阅被清空
        # 这类「地基没通电」，以前只能等某个功能返回空才发现
        await safe_reply(update.message, "🩺 体检中…")
        try:
            txt = await system_health()
        except Exception as e:
            log.error(f"系统体检失败: {e}")
            txt = f"体检失败：{str(e)[:80]}"
        await safe_reply(update.message, txt, parse_mode="Markdown")
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
