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

async def _check_source(context, key, url, name):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            ok = resp.status_code == 200
    except Exception:
        ok = False

    was_ok = _health[key]
    if was_ok and not ok:
        # 从正常变故障
        _health[key] = False
        await notify_admin(context, f"🔴 数据源异常\n{name} 无法访问，部分功能可能受影响")
    elif not was_ok and ok:
        # 从故障恢复
        _health[key] = True
        await notify_admin(context, f"🟢 数据源恢复\n{name} 已恢复正常")
