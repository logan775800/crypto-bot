"""下单前检查 —— 把 `/checklist` 那张通用清单变成**针对这一单**的几行。

## 为什么要改

`/checklist` 是一段固定文字，写得没错，但它对每一单说的话都一样。
真正有用的是"**你这一单**在费率的付费那边、而且大家都站你这边"——
那要拿这个币此刻的数据去算，不是背诵条款。

## 分工（别和已有的重复）

实盘确认卡的 `rtrade._cost_block` 已经算了：止损距离、最大亏损、爆仓距离、
手续费滑点、净盈亏比。**那些这里一律不重复**。

这里只补它没有、而 `/checklist` 点名要看的两条：

  ④ 是不是拥挤交易 —— 大家都在同一边（多空比 + 资金费方向）＝ 你在补贴对面，
     反转时最惨。这一条以前**一处都没有**。
  ① 费率的方向和结算周期 —— 你在付费那边的话，1h 结算一天扣 24 次，
     和 8h 结算完全不是一个成本量级。

## 一条判定原则

**取不到数据就不说话**，不能拿"查不到"当"没问题"。
这类检查一旦出现假的"✅ 通过"，比不检查更危险——它会让人放心加仓。
"""
import logging

log = logging.getLogger(__name__)

# 多空比偏到这个程度算"一边倒"。1.5 是账户数口径下比较明显的失衡，
# 再低就常态化了（多数币日常在 0.8~1.3 之间晃）。
LSR_CROWDED = 1.5
# 资金费率年化超过这个数算"付费很贵"。0.01%/8h ≈ 11%/年是常态基准，
# 50% 已经是明显在补贴对面。
FUNDING_COSTLY_APR = 50.0


def funding_apr(rate, interval_h):
    """把"单次结算费率"换算成年化。

    **不换算就没法比大小**：同样 -1%，1h 结算一天扣 24 次，
    比 8h 结算狠三倍——而屏幕上那两个数字长得一模一样。
    """
    if rate is None or not interval_h or interval_h <= 0:
        return None
    return rate * (24.0 / interval_h) * 365.0


def funding_verdict(side, rate, interval_h):
    """你这一单在费率的哪一边。返回 (要不要提醒, 文案) 或 (False, None)。

    永续的规矩：费率为正 = 多头付给空头。所以做多遇正费率、做空遇负费率
    都是在**往外掏钱**。
    """
    if rate is None:
        return False, None
    paying = (side == "long" and rate > 0) or (side == "short" and rate < 0)
    apr = funding_apr(abs(rate), interval_h)
    cycle = f"每{interval_h:g}h结算" if interval_h else "结算周期未知"
    if not paying:
        if apr and apr >= FUNDING_COSTLY_APR:
            return True, f"资金费站你这边：{cycle}，年化约 +{apr:.0f}% 是别人付给你"
        return False, None
    if apr and apr >= FUNDING_COSTLY_APR:
        return True, (f"你在资金费的付费那边：{cycle}，年化约 -{apr:.0f}%。"
                      f"扛得越久漏得越多，这单要么快进快出要么换方向")
    return True, f"你在资金费的付费那边（{cycle}），成本不高但方向对你不利"


def crowding_verdict(side, lsr):
    """拥挤交易检查：你是不是和大多数散户站在同一边。

    多空比是**账户数**口径（做多账户数 ÷ 做空账户数），反映的是散户情绪。
    一边倒的那侧往往是被收割的那侧——这正是 /checklist 第 ④ 条说的
    「大家都在同一边＝你在补贴对面，反转最惨」。
    """
    if not lsr or lsr <= 0:
        return False, None
    # 把 <1 翻译成倍数，否则人脑读不出量级（0.46 到底算多极端？）
    if lsr >= LSR_CROWDED:
        crowd, mult = "long", lsr
    elif lsr <= 1 / LSR_CROWDED:
        crowd, mult = "short", 1 / lsr
    else:
        return False, None
    word = "多头" if crowd == "long" else "空头"
    other = "空头" if crowd == "long" else "多头"
    if crowd == side:
        return True, (f"拥挤交易：{word}账户数是{other}的 {mult:.1f} 倍，"
                      f"而你也在{word}这边。一边倒的那侧通常是被收割的那侧")
    return True, (f"散户{word}拥挤（是{other}的 {mult:.1f} 倍），"
                  f"你站在人少的一边——反向参考下这算加分项")


async def build(symbol, side, lev=None):
    """给这一单生成检查行。返回 list[str]，取不到数据就返回空列表。

    **绝不返回"检查通过"这种话**：取不到数据和真的没问题是两回事，
    把前者说成后者会让人放心加仓，比不检查更危险。
    """
    import httpx
    base = symbol.upper().replace("USDT", "") or symbol.upper()
    inst = f"{base}USDT"
    lines = []

    async with httpx.AsyncClient(timeout=8) as client:
        # 费率：币安 premiumIndex 一次给出当前费率；结算周期复用 detail 那套
        # （Bybit 直接给 fundingInterval，OKX 靠两次结算时间相减，别重写一遍）
        rate = interval_h = None
        try:
            r = await client.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                                 params={"symbol": inst})
            if r.status_code == 200:
                rate = float(r.json().get("lastFundingRate")) * 100  # 转成百分数
        except Exception as e:
            log.info(f"[precheck] {inst} 费率取数失败: {e}")
        try:
            from handlers import detail as _d
            fi = await _d.get_funding_interval(base)
            if isinstance(fi, dict):
                interval_h = fi.get("hours")
        except Exception as e:
            log.info(f"[precheck] {inst} 结算周期取数失败: {e}")

        hit, txt = funding_verdict(side, rate, interval_h)
        if hit:
            lines.append("• " + txt)

        # 拥挤度：多空比没有批量接口，这里就查这一个币（lsratio 那边同理）
        try:
            from handlers import lsratio
            got = await lsratio._lsr_binance(client, inst)
            if got:
                hit, txt = crowding_verdict(side, got[0])
                if hit:
                    lines.append("• " + txt)
        except Exception as e:
            log.info(f"[precheck] {inst} 拥挤度取数失败: {e}")

    return lines


def block(lines):
    """把检查行拼成确认卡上的一段。没有可说的就返回空串——
    不要为了"看起来做了检查"而印一行废话。"""
    if not lines:
        return ""
    return "\n\n📋 这一单的检查\n" + "\n".join(lines)
