"""LP 撤出告警 —— 盯着链上监控里那些币的池子，有人抽水就立刻推。

## 为什么必须是「持续盯」而不是「查的时候看一眼」

`/tokensec` 已经能告诉你 LP 锁没锁、锁了多少（那是**快照**）。但真正让你归零的
不是"LP 没锁"这个状态，而是"LP 正在被抽走"这个**事件**——它发生在几分钟内，
等你想起来去查的时候，池子已经空了、你手上的币卖不出去。
状态查得到，事件必须推。

## 判据：不能直接比美元流动性（这是这个功能唯一的技术难点）

池子的美元流动性**本来就跟着币价走**。恒定乘积做市 x·y=k 下，
设池中代币 X 枚、报价币 Y 美元，价格 p = Y/X，池子总值 V = 2Y：

    Y = √(k·p)   ⟹   V ∝ √p

**币价跌 75%，没有任何人撤资，美元流动性也会自然掉 50%。**
直接拿"流动性掉了多少"当判据，等于每次砸盘都报一次跑路——
而砸盘恰恰是最常发生的事，这个告警会立刻变成噪音，然后被无视，
然后真跑路那次也一起被无视。

所以比的是**扣掉价格因素之后还剩多少**：

    预期流动性 = 基线流动性 × √(现价 / 基线价)
    抽水比例   = 1 − 实际流动性 / 预期流动性

只有这个比例超过门槛，才说明有人真的在往外搬钱。

## 其余护栏

- **基线只涨不跌**：有人加池子就抬基线；池子缩水不降基线，否则跑路会被
  "温水煮青蛙"式地一路合理化掉（每次只掉一点，基线跟着降，永远不触发）。
- **同币冷却**：抽水是个持续过程，不冷却会每轮都报。
- **报过要回补才重新武装**：和涨跌告警同一个套路，避免在门槛上抖动刷屏。
- **绝对地板**：池子本来就只有几千美元的，比例算出来再吓人也没意义，
  那种币在建监控时 `onchain` 那边已经警告过了。
"""
import logging
import math
import time

from storage import data, save_data

log = logging.getLogger(__name__)

# 扣掉价格因素后，流动性还少了这么多才算"有人在撤"。
# 0.35 = 池子被抽走三分之一以上。低于这个数的日常进出太常见（做市商调仓、
# 小额加减池），报出来全是噪音。
DRAIN_RATIO = 0.35
# 抽走这么多算「基本跑光了」，措辞和紧急程度都不一样
DRAIN_SEVERE = 0.70
# 池子低于这个数就不看了：几千美元的池子本来就随时会归零，
# 建监控的时候 onchain 那边已经警告过"这个盘子有多空"
MIN_LIQ = 5_000
# 同一个币多久内不重复报
COOLDOWN = 6 * 3600
# 回补到预期的这个比例以上，才重新武装（迟滞，防止在门槛上抖动刷屏）
REARM_RATIO = 0.85


def _state():
    return data.setdefault("rugwatch", {})


def expected_liq(base_liq, base_price, price):
    """按 x·y=k 推出「价格变成这样之后，池子本来该有多少美元」。

    V ∝ √p 是这个函数存在的全部理由，改之前先读模块开头那段。
    价格缺失或非正时返回基线本身（等于不做价格修正，宁可少报不误报）。
    """
    if not base_liq or base_liq <= 0:
        return None
    if not base_price or base_price <= 0 or not price or price <= 0:
        return base_liq
    return base_liq * math.sqrt(price / base_price)


def drain_pct(base_liq, base_price, price, liq):
    """扣掉价格因素后被抽走的比例（0~1）。算不出来返回 None。

    返回 0 表示没少甚至变多——池子变大不是坏事，不该有负数把人吓一跳。
    """
    exp = expected_liq(base_liq, base_price, price)
    if not exp or exp <= 0 or liq is None or liq < 0:
        return None
    return max(0.0, 1.0 - liq / exp)


def _onchain_watches():
    """从持续波动监控里挑出链上那些。

    **刻意复用 watchpct 的名单而不是另开一份订阅**：会盯某个链上币价格的人，
    就是会关心它跑不跑路的人。让他为同一个币再订阅一次纯属折腾，
    而且两份名单迟早会不一致。
    """
    out = []
    for chat_id, lst in (data.get("watchpct") or {}).items():
        for w in lst or []:
            if w.get("market") == "onchain" and w.get("symbol"):
                out.append((str(chat_id), w))
    return out


def _rec(addr):
    return _state().setdefault(addr, {})


def assess(addr, price, liq, now=None):
    """判定单个币这一轮该不该报。返回 (要不要报, 抽水比例, 是否严重)。

    顺带维护基线与武装状态，所以**每轮都要调用**，不能只在怀疑时调。
    """
    now = now or time.time()
    r = _rec(addr)
    base_liq, base_price = r.get("base_liq"), r.get("base_price")

    # 第一次见：只建基线，不判定（没有基线谈不上"少了多少"）
    if not base_liq:
        r.update(base_liq=liq, base_price=price, armed=True)
        return False, None, False

    d = drain_pct(base_liq, base_price, price, liq)
    if d is None:
        return False, None, False

    # 基线只涨不跌：池子变大就抬基线。缩水时**不降**——降了的话，
    # 分批慢慢抽水会被一路合理化成"新常态"，永远触发不了。
    exp = expected_liq(base_liq, base_price, price)
    if exp and liq > exp:
        r.update(base_liq=liq, base_price=price)

    # 回补了就重新武装
    if d <= (1 - REARM_RATIO):
        r["armed"] = True

    if liq is not None and liq < MIN_LIQ and (base_liq or 0) < MIN_LIQ:
        return False, d, False        # 本来就是个空池子，别拿它当事件报

    if d < DRAIN_RATIO or not r.get("armed", True):
        return False, d, False
    if now - r.get("last_alert", 0) < COOLDOWN:
        return False, d, False

    r["armed"] = False
    r["last_alert"] = now
    return True, d, d >= DRAIN_SEVERE


def format_alert(name, addr, chain, d, severe, liq, exp):
    """告警文案。纯文本发（合约地址带下划线的话 Markdown 会吃掉）。"""
    head = "🚨 流动性被抽走" if severe else "⚠️ 流动性明显减少"
    lines = [
        f"{head}：{name}",
        f"链：{chain}",
        f"合约：{addr}",
        "",
        f"扣掉币价变动之后，池子还少了约 {d * 100:.0f}%",
        f"当前流动性 ${liq:,.0f}（按币价推算本该有 ${exp:,.0f}）",
        "",
    ]
    if severe:
        lines.append("这是跑路(rug)最典型的样子：项目方把池子里的钱撤走，")
        lines.append("剩下的人手里的币**卖不出去**。别再补仓，先确认还能不能卖出。")
    else:
        lines.append("可能是做市商调仓，也可能是撤资的开始。")
        lines.append("池子变浅 = 同样的卖单滑点更大，先看一眼还能不能按预期价格出掉。")
    lines.append("")
    lines.append("口径：池子美元价值本来就随币价涨跌（V 正比于价格的平方根），")
    lines.append("这里报的是**扣掉价格因素之后**多出来的那部分减少。")
    return "\n".join(lines)


async def check_rugs(context):
    """后台任务：逐个链上监控标的看池子有没有被抽。

    ⚠️ 收集范围决定了谁会被检查到——这里盯的是 watchpct 里 market=onchain 的条目。
    以后要是新增了别处的链上持仓/订阅，必须同步扩这里，
    否则这个告警对那批人是完全隐形的。
    """
    watches = _onchain_watches()
    if not watches:
        return
    from handlers import onchain as oc

    # 同一个币可能被多个会话盯着，取一次数发多次
    by_addr = {}
    for chat_id, w in watches:
        by_addr.setdefault(w["symbol"], []).append((chat_id, w))

    changed = False
    for addr, subs in by_addr.items():
        try:
            price, t = await oc.price_of(addr)
        except Exception as e:
            log.info(f"[rugwatch] {addr[:12]} 取数失败: {e}")
            continue
        if price is None or not t:
            continue
        liq = t.get("liq")
        if liq is None:
            continue

        hit, d, severe = assess(addr, price, liq)
        changed = True
        if not hit:
            continue

        r = _rec(addr)
        exp = expected_liq(r.get("base_liq"), r.get("base_price"), price) or liq
        name = t.get("symbol") or subs[0][1].get("name") or addr[:10]
        chain = t.get("chain_key") or subs[0][1].get("chain") or "?"
        text = format_alert(name, addr, chain, d, severe, liq, exp)
        for chat_id, _w in subs:
            try:
                await context.bot.send_message(int(chat_id), text)
            except Exception as e:
                log.warning(f"[rugwatch] 推送失败 {chat_id}: {e}")

    if changed:
        save_data()
