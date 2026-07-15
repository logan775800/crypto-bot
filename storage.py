import os
import json
import logging
from config import DATA_FILE

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"读取数据出错: {e}")
    return {"alerts": [], "holdings": {}, "broadcast_chats": []}

# 全局数据
data = load_data()
# 确保新字段存在（兼容旧数据文件）
data.setdefault("alerts", [])
data.setdefault("holdings", {})
data.setdefault("broadcast_chats", [])  # 订阅了定时播报的群/私聊id列表
data.setdefault("market_watch", [])     # 订阅市场异动告警的chat_id
data.setdefault("alerted_coins", {})    # 已告警的币 {symbol: 时间戳}（冷却用）
data.setdefault("known_coins", [])      # 已知的OKX币种列表（检测新币用，旧版遗留）
data.setdefault("known_coins_ex", {})   # 各所已知币 {交易所: [币]}（多所新币检测）
data.setdefault("last_volumes_ex", {})  # 各所上轮成交额 {交易所: {币: 额}}（多所放量检测）
data.setdefault("coin_tiers", {})       # 分级告警：每个币已告警的台阶
data.setdefault("user_prefs", {})       # 用户偏好 {chat_id: {follows:[], threshold:20, quiet:[start,end]}}
data.setdefault("last_surge", {})       # 上轮异动币
data.setdefault("last_volumes", {})     # 上轮成交量（放量检测用）
data.setdefault("news_subs", [])        # 订阅新闻推送的chat_id
data.setdefault("pushed_news", [])      # 已推送的新闻链接（去重）
data.setdefault("unlock_subs", [])      # 订阅解锁提醒的chat_id
data.setdefault("alerted_unlocks", [])  # 已提醒的解锁事件（去重）
data.setdefault("summary_subs", [])     # 订阅每日总结的chat_id
data.setdefault("analysis_subs", [])    # 订阅每日分析推送的chat_id
data.setdefault("holding_watch", {})    # 持仓异动提醒 {uid: chat_id}
data.setdefault("holding_alerted", {})  # 持仓异动冷却记录
data.setdefault("gas_subs", {})         # gas提醒订阅 {chat_id: {"threshold":gwei,"armed":bool}}
data.setdefault("arb_subs", {})         # 套利监控订阅 {chat_id: {"threshold":pct}}
data.setdefault("arb_alerted", {})      # 套利告警冷却 {sym: 时间戳}
data.setdefault("whale_addr", {})       # 巨鲸地址追踪 {chat_id: {addr: {"label":..,"last":块高}}}
data.setdefault("whale_min", {})        # 地址追踪最小美元阈值 {chat_id: usd}
data.setdefault("ti_alerts", [])        # 技术指标告警订阅 [{chat_id,symbol,rsi_state,ma_state}]
data.setdefault("contract_watch", [])   # 订阅全交易所合约异动告警的chat_id
data.setdefault("contract_tiers", {})   # 合约分级告警记录 {交易所_币: {tier,dir,ts}}
data.setdefault("grids", {})            # Bybit 永续网格 {chat_id:symbol: {区间/档位/挂单/成交/利润...}}
data.setdefault("watchpct", [])         # 持续波动监控 [{chat_id,symbol,pct,base,src,last_ts}]
data.setdefault("vtrade", {})           # 虚拟合约交易 {uid: {balance, positions{sym:{...}}, history[], chat_id}}
data.setdefault("rtrade_alert", {})     # 实盘爆仓预警 {enabled, threshold, chat_id, cooldown{sym:ts}}

def save_data():
    # 原子写入：先写临时文件再 os.replace，避免写盘中途被打断（多个定时任务并发保存）
    # 导致 data.json 只写了一半而损坏，下次启动整份数据丢失
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        logging.error(f"保存数据出错: {e}")
