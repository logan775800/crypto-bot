import os

# 当前代码版本（每次发布 tag 时同步 bump，/version 用它报告线上到底跑的是哪版）
VERSION = "v1.0.74"

TOKEN = os.environ["BOT_TOKEN"]
DATA_FILE = "/app/data.json"

# 基础币种（保底，启动时会被动态列表覆盖/扩充）
COIN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "SOL": "solana", "XRP": "ripple", "DOGE": "dogecoin",
    "ADA": "cardano", "USDT": "tether",
}

# 动态更新币种列表（启动时调用）
def update_coins(new_mapping):
    COIN_IDS.update(new_mapping)

# 定时播报配置
BROADCAST_HOUR = 9
BROADCAST_MINUTE = 0
BROADCAST_COINS = ["BTC", "ETH", "BNB", "SOL"]

# AI 中转站配置
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_BASE_URL = os.environ.get("AI_BASE_URL", "")
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")

# 管理员chat_id（运维告警接收 + 部署审批人）。支持多个：逗号分隔，如 "7774574457,1087968824"
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
_admin_list = [s.strip() for s in ADMIN_CHAT_ID.split(",") if s.strip()]
ADMIN_IDS = set(_admin_list)                       # 所有管理员 id（字符串集合）
PRIMARY_ADMIN_ID = _admin_list[0] if _admin_list else ""  # 第一个：运维告警默认发给他

def is_admin(uid):
    """uid 是否管理员。未配置 ADMIN_CHAT_ID 时不限制（方便测试）。"""
    return (not ADMIN_IDS) or str(uid) in ADMIN_IDS

# Jenkins 部署审批：点"确认"按钮后由机器人远程触发 Jenkins 部署任务
JENKINS_URL = os.environ.get("JENKINS_URL", "")            # 如 https://logan-jenkins.22889.club
JENKINS_JOB = os.environ.get("JENKINS_JOB", "update-crypto-bot")
JENKINS_DEPLOY_TOKEN = os.environ.get("JENKINS_DEPLOY_TOKEN", "")  # 任务"触发远程构建"令牌(备选)
JENKINS_USER = os.environ.get("JENKINS_USER", "")                  # Jenkins 用户名(推荐用API Token方式)
JENKINS_API_TOKEN = os.environ.get("JENKINS_API_TOKEN", "")        # 该用户的 API Token

# 巨鲸地址追踪（Etherscan V2 API，免费key：etherscan.io/apis）
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")

# Bybit 实盘网格（V5 API，永续合约）。⚠️ 默认模拟盘，务必先在模拟盘验证再切实盘。
# BYBIT_TESTNET=true(默认) 走模拟盘 api-testnet.bybit.com；设为 false 才是实盘。
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.environ.get("BYBIT_TESTNET", "true")
