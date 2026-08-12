import logging
import httpx
from telegram.ext import ContextTypes
from config import ADMIN_IDS

# 数据源健康状态（内存记录，检测状态变化）
_health = {"coingecko": True, "okx": True}

async def notify_admin(context, text):
    """发告警给所有管理员"""
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=int(aid), text=text)
        except Exception as e:
            logging.error(f"管理员告警发送失败 {aid}: {e}")

# 启动告警（post_init后发一次）
async def startup_notify(context: ContextTypes.DEFAULT_TYPE):
    from config import VERSION
    await notify_admin(context, f"🟢 Bot 已启动/重启（版本 {VERSION}）\n所有功能已加载，开始运行")

# 数据源健康检查（定时调用）
async def health_check(context: ContextTypes.DEFAULT_TYPE):
    # 检查 CoinGecko
    await _check_source(context, "coingecko",
        "https://api.coingecko.com/api/v3/ping", "CoinGecko行情源")
    # 检查 OKX
    await _check_source(context, "okx",
        "https://www.okx.com/api/v5/public/time", "OKX交易所源")

log = logging.getLogger(__name__)

# 连续失败多少次才报警。单次探测就报的话，一个 10 秒超时就能触发——
# 而误报会让人开始忽略告警，那比不报还糟。
FAIL_STREAK = 3
_fails = {}

# 各数据源挂掉时**具体**哪些功能受影响。"部分功能可能受影响"等于没说，
# 用户既不知道该停手还是照用，也不知道该验证什么。
IMPACT = {
    "coingecko": ("现货报价、涨跌榜、技术分析、每日播报。"
                  "价格预警/持仓监控会自动切到 Bybit 永续价（有基差但能用）"),
    "okx": "OKX 专区、清算数据、部分合约行情（币安/Bybit 路径不受影响）",
}


async def _check_source(context, key, url, name):
    """探一次源。区分三种结果：正常 / 限流 / 真不通。

    429 是**我们自己调太快**，不是源挂了——把它算成故障会天天误报，
    而且指向错误的排查方向。
    """
    status = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            status = resp.status_code
    except Exception as e:
        log.debug(f"健康检查 {name} 异常: {e}")
    if status == 429:
        log.warning(f"{name} 被限流(429)——是调用频率问题，不算数据源故障")
        return
    ok = status == 200

    if ok:
        _fails[key] = 0
        if not _health[key]:
            _health[key] = True
            await notify_admin(context, f"🟢 数据源恢复\n{name} 已恢复正常")
        return

    _fails[key] = _fails.get(key, 0) + 1
    if _fails[key] < FAIL_STREAK or not _health[key]:
        return          # 还没连续失败够次数，或已经报过了
    _health[key] = False
    extra = ""
    if key == "coingecko":
        from api import fallback_recent
        if fallback_recent():
            extra = "\n✅ 已自动切到 Bybit 兜底源，取价功能仍在工作"
    await notify_admin(context,
        f"🔴 数据源异常\n{name} 连续 {_fails[key]} 次探测失败"
        f"（间隔 5 分钟，非瞬时抖动）\n\n受影响：{IMPACT.get(key, '未知')}{extra}")
