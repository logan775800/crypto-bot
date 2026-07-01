import os

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

# 管理员chat_id（运维告警接收 + 部署审批人）
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

# Jenkins 部署审批：点"确认"按钮后由机器人远程触发 Jenkins 部署任务
JENKINS_URL = os.environ.get("JENKINS_URL", "")            # 如 https://logan-jenkins.22889.club
JENKINS_JOB = os.environ.get("JENKINS_JOB", "update-crypto-bot")
JENKINS_DEPLOY_TOKEN = os.environ.get("JENKINS_DEPLOY_TOKEN", "")  # 任务"触发远程构建"令牌(备选)
JENKINS_USER = os.environ.get("JENKINS_USER", "")                  # Jenkins 用户名(推荐用API Token方式)
JENKINS_API_TOKEN = os.environ.get("JENKINS_API_TOKEN", "")        # 该用户的 API Token

# 巨鲸地址追踪（Etherscan V2 API，免费key：etherscan.io/apis）
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
