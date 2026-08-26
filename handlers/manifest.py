"""数据清单 —— 本轮分析**实际拿到了什么**，以及由此**禁止说什么**。

和 datameta 的区别（别搞混）：
  • datameta.probe 是**主动体检**：现在这个币各维度取不取得到。模型得自己想起来调。
  • 本模块是**被动记账**：这一轮模型实际调了哪些工具、每个工具返回是好是坏。

后者才是真正的约束来源。原因很直接：模型根本没调 get_orderbook 时，datameta
说盘口"可用"也没用——它手上就是没有盘口数据，却照样能写出"卖墙承接明显"。
**最危险的不是分析错一次，而是看起来完整、实际上有字段是猜的。**

所以这里做三件事：
  1. 记账：每个工具调用记 (维度, 成功/降级, 说明)；
  2. 约束：把清单作为硬指令喂回模型（缺哪个维度就禁止哪类结论）；
  3. 校验：模型给出最终答案后，扫描它有没有用到清单里没有的维度，
     违规就带着违规清单让它重写一次。

校验用关键词而不是让模型自觉——自觉在这件事上已经被证明不可靠。
"""
import re

# 维度定义：key → (中文名, 由哪些工具提供, 缺失时禁止出现的词, 禁令说明)
# 关键词只挑**必须有该维度才能说**的强断言词，避免误伤泛泛提及。
DIMENSIONS = {
    "oi": ("持仓量OI", {"get_oi_history"},
           ("新多进场", "新空堆积", "空头回补", "多头平仓", "持仓量", "OI",
            "多头拥挤", "空头拥挤", "增仓", "减仓量"),
           "缺 OI → 不得谈「谁在推动 / 是否拥挤 / 增减仓」"),
    "book": ("订单簿", {"get_orderbook"},
             ("买墙", "卖墙", "挂单墙", "盘口", "买盘承接", "卖压挂单",
              "深度不足", "买一", "卖一"),
             "缺订单簿 → 不得谈「买墙 / 卖墙 / 盘口承接 / 深度」"),
    "trades": ("逐笔成交", {"get_recent_trades"},
               ("主动买", "主动卖", "大单", "净delta", "taker"),
               "缺逐笔成交 → 不得谈「主动买卖 / 大单方向」"),
    "liq": ("清算数据", {"get_liquidations"},
            ("清算密集", "清算区", "轧空", "挤压空间", "爆仓密集", "流动性猎杀"),
            "缺清算 → 不得谈「清算密集区 / 轧空挤压空间」"),
    "funding": ("资金费率", {"get_funding_history"},
                ("资金费", "费率", "基差", "永续溢价", "永续折价"),
                "缺资金费 → 不得谈「费率高低 / 基差 / 溢价折价」"),
    "btc": ("BTC联动", {"get_market_context"},
            ("BTC联动", "BTC破位", "大盘联动"),
            "缺市场联动 → 不得谈「BTC 联动 / 大盘风险」"),
    "account": ("真实账户", {"get_my_account"},
                ("你的权益", "总权益", "可用保证金", "你的持仓", "爆仓价"),
                "缺账户 → 不得给出基于真实权益的具体 USDT 仓位，只能给公式"),
}

# K线周期单独处理：缺哪个周期就不许给哪个周期的精确位置
_IV_PAT = re.compile(r"(5m|15m|30m|1h|4h|1d|日线|小时线)")

# "截至 HH:MM" 这类实时性声明，没有任何成功取数时不许出现
_ASOF = re.compile(r"截至|实时|此刻|当前价[为是]")

# 工具结果里出现这些词 = 这次没真拿到，只是拿到一句解释
_DEGRADED = ("⚠️", "暂不可用", "取不到", "返回空", "不足", "失败", "查不到", "不存在")


class Manifest:
    """一轮对话的数据账本。跟着 tool_executor 走，模型调什么它记什么。"""

    def __init__(self, symbol=None):
        self.symbol = symbol
        self.calls = []              # [(工具名, ok, 摘要)]
        self.ivs = {}                # {周期: ok}  K线专用
        self.identity = None         # 合约身份（symbols.for_ai 的结果）
        self.kline_ok = False        # 这一轮到底拿到过 K 线没有（与周期名无关）
        self.times = []              # 每次取数的时刻，用来算这批数据是不是同一时点的
        # 工具在取不到数据时给出的替代办法（如「/liqmap 是估算但不依赖 OKX」）。
        # **由我们自己贴到答案末尾，不交给模型转达**——模型会压缩会改写，
        # 真机上它就把这句丢掉过，而那是整条回答里唯一能让人往下走的信息。
        self.fallbacks = []

    # ── 记账 ────────────────────────────────────────────────
    def record(self, name, args, result):
        import time as _t
        ok = not any(w in (result or "") for w in _DEGRADED)
        self.calls.append((name, ok, (result or "")[:80]))
        self.times.append(_t.time())
        if name == "get_klines":
            # 拿到过 K 线这件事本身要单独记：周期名只是锦上添花。
            # 只靠 self.ivs 判断的话，没带 interval 的调用会让「市场数据」整块显示成
            # 未取——K 线明明成功了，用户却以为分析没有地基。
            self.kline_ok = getattr(self, "kline_ok", False) or ok
            iv = str((args or {}).get("interval") or "").lower()
            if iv:
                # 同一周期多次调用，只要有一次成功就算有
                self.ivs[iv] = self.ivs.get(iv, False) or ok
        return ok

    def _got(self, dim):
        """这个维度这一轮到底拿到没有。"""
        _n, tools, _kw, _r = DIMENSIONS[dim]
        return any(ok for name, ok, _ in self.calls if name in tools)

    def _called(self, dim):
        _n, tools, _kw, _r = DIMENSIONS[dim]
        return any(name in tools for name, _, _ in self.calls)

    @property
    def any_success(self):
        return any(ok for _, ok, _ in self.calls)

    @property
    def available(self):
        return [k for k in DIMENSIONS if self._got(k)]

    @property
    def unavailable(self):
        return [k for k in DIMENSIONS if not self._got(k)]

    # ── 给模型的硬约束 ──────────────────────────────────────
    def ledger(self):
        """本轮数据清单 + 禁令。在模型给最终答案前喂给它。"""
        if not self.calls:
            return ("【数据清单】本轮**一个数据工具都没调**。因此你不得给出任何"
                    "具体价位、方向判断或交易计划——先调工具取数，或如实告诉用户"
                    "需要先取数。")
        lines = ["【数据清单·本轮实际取到的】"]
        if self.symbol:
            lines.append(f"标的：{self.symbol}")
        if self.ivs:
            got = [iv for iv, ok in self.ivs.items() if ok]
            bad = [iv for iv, ok in self.ivs.items() if not ok]
            txt = f"K线：{'/'.join(got) or '无'} 可用"
            if bad:
                txt += f"；{'/'.join(bad)} 未取到，不得给这些周期的位置"
            lines.append(txt)
        for k, (cn, _t, _kw, _r) in DIMENSIONS.items():
            if self._got(k):
                lines.append(f"{cn}：可用")
        if self._got("book") and self._got("trades"):
            # 挂单是「打算成交」，逐笔是「已经成交」，两者本来就常打架。
            # 挑一边讲成单边结论，是这套数据最容易出的错。
            lines.append("盘口(挂单意图)与逐笔(已成交)方向不一致时，必须写明"
                         "「信号冲突」并说清各自指向，不得只挑一边给单边结论。")
        sp = self.spread()
        if sp >= self.SPREAD_WARN:
            lines.append(f"注意：本轮各项数据前后相隔 {sp:.0f} 秒，不是同一时点的快照。"
                         f"盘口和逐笔时效最短，不要拿它们和更早取的 K 线当作同时发生。")
        miss = [DIMENSIONS[k] for k in self.unavailable]
        if miss:
            lines.append("")
            lines.append("**以下维度本轮没有数据，硬性禁止使用**：")
            for cn, _t, _kw, rule in miss:
                lines.append(f"- {rule}")
            lines.append("没调过的工具＝没有那份数据。不许凭常识、凭经验、凭"
                         "「一般来说」补全这些字段——那是编造，比说不知道危险得多。")
        untouched = [DIMENSIONS[k][0] for k in self.unavailable if not self._called(k)]
        if untouched:
            lines.append(f"（其中 {'、'.join(untouched)} 是你**根本没调**，"
                         f"需要就现在调，不要直接下结论）")
        return "\n".join(x for x in lines if x)

    # ── 事后校验 ────────────────────────────────────────────
    def violations(self, text):
        """扫描最终答案里有没有用到本轮没拿到的维度。返回违规说明列表。"""
        if not text:
            return []
        out = []
        for k in self.unavailable:
            cn, _t, kws, rule = DIMENSIONS[k]
            hit = [w for w in kws if w in text]
            if hit:
                out.append(f"你提到了「{'、'.join(hit[:3])}」，但本轮没有{cn}数据。{rule}")
        # 缺的周期不许给该周期的精确位置
        bad_ivs = [iv for iv, ok in self.ivs.items() if not ok]
        for iv in bad_ivs:
            if iv in text:
                out.append(f"你引用了 {iv} 周期，但该周期本轮取数失败，不得给出它的位置。")
        # 一次成功取数都没有还写"截至/实时"
        if not self.any_success and _ASOF.search(text):
            out.append("本轮没有任何成功取数，却写了「截至/实时/当前价」这类实时性声明，必须删掉。")
        return out

    def fix_prompt(self, violations):
        """违规时给模型的重写指令。只说改什么，不替它改。

        ⚠️ **「XX」必须只写和这次问题相关的那一两项。**
        原来只说「说明本次缺少 XX 数据」，模型就把 ledger 里那张完整禁令表
        整个背了一遍——用户问「有多少空头被清算」，回答开头却是
        「本次缺少 OI、订单簿、逐笔成交、清算、资金费率、市场联动和真实账户数据」。
        订单簿跟这个问题毫无关系，但列出来之后整条消息看起来就是"系统全挂了"。
        真机上群友因此以为机器人坏了（2026-08-26）。
        """
        return ("你上一版回答违反了数据清单约束：\n"
                + "\n".join(f"{i+1}. {v}" for i, v in enumerate(violations))
                + "\n\n请重写这份回答：删掉所有依赖缺失维度的判断。"
                  "开头**只用一句**说明缺的是什么，"
                  "而且**只点名和用户这个问题直接相关的那一两项**——"
                  "不要罗列所有没取到的维度（用户没问的东西不算「缺」，"
                  "列出来会让人以为整个系统都挂了）。"
                  "其余基于真实取到的数据的内容保留。不要为了凑完整而编造，宁可短。"
                  "如果某个维度只是你没去调、而不是取不到，**现在就去调**，别直接说缺。")

    # ── 给用户看的 ──────────────────────────────────────────
    # 维度归类。「19/21 项可用」这种总分看着完整，其实把两件性质不同的事混成了
    # 一个数：市场数据缺了是结论没地基，账户数据缺了是仓位不能按真实权益算。
    # 分开报，用户一眼知道哪一层能信。
    MARKET_DIMS = ("oi", "book", "trades", "liq", "funding", "btc")
    ACCOUNT_DIMS = ("account",)

    SPREAD_WARN = 90        # 秒。首尾取数间隔超过这个值就不算「同一时点的快照」

    def spread(self):
        """本轮第一次和最后一次取数相隔多久（秒）。"""
        return (self.times[-1] - self.times[0]) if len(self.times) >= 2 else 0.0

    def _group(self, dims):
        got = [DIMENSIONS[k][0] for k in dims if self._got(k)]
        miss = [DIMENSIONS[k][0] for k in dims if self._called(k) and not self._got(k)]
        return got, miss

    def header(self):
        """贴在回答顶部的清单，让用户自己能核对结论的地基。"""
        if not self.calls:
            return ""
        lines = []
        mk_got, mk_miss = self._group(self.MARKET_DIMS)
        ivs = "/".join(iv for iv, ok in self.ivs.items() if ok) if self.ivs else ""
        kl = [f"K线 {ivs}" if ivs else "K线"] if self.kline_ok else []
        mk = "｜".join(kl + mk_got)
        lines.append(f"`市场数据` {mk or '未取'}")
        ac_got, ac_miss = self._group(self.ACCOUNT_DIMS)
        lines.append(f"`账户数据` {'｜'.join(ac_got) if ac_got else '未取（仓位只能给公式，不能给具体金额）'}")
        miss = mk_miss + ac_miss
        if miss:
            lines.append(f"`缺失维度` ⚠️ {'、'.join(miss)}")
        # 取数跨度：这批数据不是同一瞬间的快照。盘口几秒就变样，跨度大的时候
        # 拿盘口和几分钟前的 K 线互相印证是会出错的，必须让用户看见。
        sp = self.spread()
        if sp >= self.SPREAD_WARN:
            lines.append(f"`取数跨度` ⚠️ {sp:.0f} 秒，非同一时点快照——盘口/逐笔可能已过期")
        return "\n".join(lines)
