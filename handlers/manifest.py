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

    # ── 记账 ────────────────────────────────────────────────
    def record(self, name, args, result):
        ok = not any(w in (result or "") for w in _DEGRADED)
        self.calls.append((name, ok, (result or "")[:80]))
        if name == "get_klines":
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
        """违规时给模型的重写指令。只说改什么，不替它改。"""
        return ("你上一版回答违反了数据清单约束：\n"
                + "\n".join(f"{i+1}. {v}" for i, v in enumerate(violations))
                + "\n\n请重写这份回答：删掉所有依赖缺失维度的判断，并在开头一行说明"
                  "「本次缺少 XX 数据，相关结论已略去」。其余基于真实取到的数据的内容保留。"
                  "不要为了凑完整而编造，宁可短。")

    # ── 给用户看的 ──────────────────────────────────────────
    def header(self):
        """贴在回答顶部的一行清单，让用户自己能核对结论的地基。"""
        if not self.calls:
            return ""
        ok_n = sum(1 for _, ok, _ in self.calls if ok)
        got = [DIMENSIONS[k][0] for k in self.available]
        miss = [DIMENSIONS[k][0] for k in self.unavailable if self._called(k)]
        parts = [f"`数据 {ok_n}/{len(self.calls)} 项可用`"]
        if self.ivs:
            ivs = "/".join(iv for iv, ok in self.ivs.items() if ok)
            if ivs:
                parts.append(f"K线 {ivs}")
        if got:
            parts.append("｜".join(got))
        if miss:
            parts.append(f"⚠️ 缺：{'、'.join(miss)}")
        return "　".join(parts)
