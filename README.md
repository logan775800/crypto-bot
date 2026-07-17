# crypto-bot · Telegram 加密货币行情机器人

一个基于 `python-telegram-bot` 的 Telegram 机器人：查币价、技术分析、**AI 对话+量化分析**、价格预警、波动/合约异动监控、**虚拟合约练手**、**Bybit 实盘交易台**，以及 OKX / 币安 / Bybit 交易所专区。行情数据源支持 **CoinGecko → OKX → 币安 → Bybit** 多级回退，并标注来源。

---

## 功能一览

### 行情 / 分析
- **查价**：直接发币名即可（现货+合约+资金费，带来源标注）；`/dashboard` 看板；`/top` 涨跌榜
- **分析**：`/analyze` 技术指标（RSI/均线/MACD/布林带）、`/ai BTC` AI 解读
- **交易所专区**：🔥 OKX / 🅱️ 币安 / 🟡 Bybit（新币榜、涨幅榜、资金费率、多空比、爆仓、合约行情）

### 💬 AI 助手（对话 + 量化）
- **群里 @机器人 或回复它** 即可连续对话；私聊 `/ask 问题` 或 `/menu → 💬 AI 助手`（连续会话）
- AI 可**自行调用 12 个只读数据工具**做真·多周期量化，而非口头点评：
  - `多周期K线`（EMA排列/斜率、**ATR14+止损距离**、RSI、摆动高低点与 HH-HL/LH-LL 结构、量能、VWAP、前高前低）
  - `OI 历史`（价/OI 四象限：新多进场 / 空头回补 / 新空堆积 / 多头平仓）
  - `资金费率`（历史/预测/是否极端 + 标记vs指数**基差**）
  - `订单簿`（买卖失衡、挂单墙）、`逐笔成交`（主动买卖 delta、大单方向）
  - `BTC/ETH 联动`、`清算数据`、`真实账户`（只读，仅管理员）
- `/resetchat` 清空对话记忆

### 监控 / 预警
- **持续波动监控**：`/watchpct BTC 2 合约` —— 每涨跌超 ±N% 提醒，报后以新价继续盯（OKX/Bybit 永续走 **WebSocket 秒级**）
- **合约异动告警**：`/watchcontract` 全交易所永续 ±20% 起分级告警（同币同档 6h 冷却，不刷屏）
- **价格预警**：菜单点选币种→方向→发价格，或 `/alert`
- **其它**：`/gasalert` Gas 跌破提醒、`/arbwatch` 跨所套利、`/track 0x地址` 巨鲸追踪

### 策略 / 交易
- **连涨连跌扫描**：`/upstreak 3 bybit`、`/downstreak`（多所永续日线同向）
- **合约风控清单**：`/checklist`（开仓前自查）
- **🎮 虚拟合约**（练手不碰真钱）：`/vopen BTC long 1000 10` 开多、`/vclose` 平仓、`/vpos` 持仓浮盈爆仓价、`/vhistory` 胜率、`/vreset`；含 0.05% 手续费、自动爆仓监控
- **🔴 Bybit 实盘交易台**（管理员·默认模拟盘）：`/trade` 点按钮开平仓；`/ropen` 限价开仓(带TP/SL、二次确认)、`/rclose`(强制 reduceOnly)、`/rpos`、`/rbal`、`/rtpsl`、`/rliqalert` 爆仓预警
- **网格**：`/gridstart`（Bybit 永续，管理员）

### 运维自查
- `/version` 版本+AI模型+Bybit状态+管理员　`/id` 会话与身份　`/whois <id>` 反查用户　`/migratechat <旧群id>` 迁移订阅

> 交互以 **/menu 菜单 + 底部常驻键盘** 为主，绝大多数功能点按钮即可，无需记命令。

---

## 目录结构

```
bot.py              入口：注册命令/回调/定时任务、/version /id /whois /migratechat、群升级自动迁移
config.py           配置（环境变量）+ VERSION + is_admin()（支持多管理员）
api.py              CoinGecko 数据源封装（缓存+限流）
indicators.py       技术指标（纯 Python 实现）
storage.py          JSON 持久化（原子写入）+ migrate_chat() 群升级迁移
bybit_trade.py      Bybit V5 私有客户端（HMAC签名/下单/持仓/杠杆，网格+实盘台共用）
handlers/
  menu.py           菜单 + 所有内联按钮回调（含部署审批、交易台、AI助手入口）
  quickprice.py     发币名直接查价（多级回退）+ 各引导流程的输入路由
  chat.py           💬 群内@对话 / /ask：AI + 12个只读工具（按调用者鉴权）
  marketdata.py     Bybit 公开行情 + 服务端算指标（K线/OI/资金费/盘口/逐笔/联动）
  ai.py             AI 网关：ask_ai / ask_ai_messages(多轮) / ask_ai_tools(函数调用循环)
  vtrade.py         🎮 虚拟合约交易（模拟盘、自动爆仓监控）
  rtrade.py         🔴 Bybit 实盘交易台（限价开+TP/SL、reduceOnly平仓、爆仓预警）
  grid.py           Bybit 永续网格策略
  watchpct.py       持续波动监控（多所取价，WS秒级+轮询兜底）
  contract_alert.py 全交易所合约异动分级告警（含推送冷却去重）
  contract_ws.py    OKX/Bybit 永续 WebSocket 实时流
  streak.py         连涨/连跌合约扫描　checklist.py 合约风控清单
  okx.py binance.py bybit.py    三家交易所专区
  price.py analysis.py alert.py portfolio.py market_alert.py
  news.py unlock.py summary.py movers.py ...  各类推送
  util.py           Markdown 转义 + 容错发送（safe_reply/safe_edit）
  deploy.py         审批部署：远程触发部署任务
Dockerfile / docker-compose.yml   容器部署
Jenkinsfile          部署流水线（按 tag 部署/回滚）
Jenkinsfile.notify   审批通知流水线（webhook 触发）
```

---

## 快速开始（部署）

```bash
git clone git@github.com:logan775800/crypto-bot.git
cd crypto-bot
cp .env.example .env      # 填入真实值
docker compose up -d
docker compose logs -f    # 看到 "Bot 启动中..." 即成功
```

- 运行方式：`docker compose`（容器挂载源码 + 启动时 `pip install` 再运行）
- 更新代码后重载：`docker compose up -d --force-recreate`（挂载卷需强制重建才会加载新代码）

### 环境变量（.env）

| 变量 | 说明 |
|------|------|
| `BOT_TOKEN` | Telegram 机器人 token（@BotFather） |
| `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` | AI 用的中转站配置（OpenAI 兼容）。换模型只改 `AI_MODEL` + 重建容器，无需改代码 |
| `ADMIN_CHAT_ID` | 管理员 user_id，**支持多个用逗号分隔**（如 `123,456`）：接运维告警 + 部署审批 + 实盘/账户权限 |
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | Bybit 实盘台/网格/AI查账户用。**只勾合约下单+持仓权限，绝不开提币**，并加服务器 IP 白名单 |
| `BYBIT_TESTNET` | `true`(默认)=模拟盘；改 `false` 才走实盘真钱 |
| `JENKINS_URL` / `JENKINS_JOB` | Jenkins 地址与部署任务名 |
| `JENKINS_USER` / `JENKINS_API_TOKEN` | 触发部署用的 Jenkins 用户 API Token（推荐） |
| `JENKINS_DEPLOY_TOKEN` | 备选：任务「触发远程构建」令牌 |
| `ETHERSCAN_API_KEY` | 巨鲸地址追踪用（免费申请 etherscan.io/apis） |

> `.env` 已被 `.gitignore` 忽略，切勿提交。群里发 `/id` 可查当前会话的 chat_id。

### 群里使用注意
- **必须**在 @BotFather 关闭隐私模式：`/mybots` → 选机器人 → **Bot Settings → Group Privacy → Turn off**。
  ⚠️ 改完还要**把机器人移出群再重新拉进去**，对已有群才生效。否则机器人在群里只收得到「命令 / @它 / 回复它」，**「发币名查价」「`币 百分比` 设监控」「发价格设预警」等纯文字流程会静默失效**。
- 群里 AI 对话：**@机器人**（开新话题）或**回复它的消息**（接着聊，不用重复 @）。
- **别开「匿名管理员」**：匿名发言时 Telegram 把发送者标成 `GroupAnonymousBot`(id `1087968824`)，会导致按钮流程的 per-user 状态对不上。

### 群升级为超级群
群升级后 chat_id 会变（变成 `-100...`），旧 id 推送会 400。机器人**已自动处理**：监听到升级会把所有订阅从旧 id 迁到新 id。历史遗留的可用 `/migratechat <旧群id>` 手动迁移。
> 注意 Jenkins 凭据 `telegram-approve-chat` 需手动改成新群 id。

---

## 版本号与回滚

- 版本号用 **语义化 tag `vX.Y.Z`**，每次推代码自动 +patch 并推到 GitHub。
- **回滚**：Jenkins `update-crypto-bot` → Build with Parameters → `TAG` 填旧版本（如 `v1.0.10`）→ Build。
- 该部署任务也可手动部署任意 tag（`TAG` 留空=最新）。

---

## CI/CD：带审批的自动部署

```
push 代码+tag
 → GitHub webhook 通知 Jenkins
 → crypto-bot-notify 任务：向审批群发「🆕 新版本 vX.Y.Z 是否部署？[✅确认][❌取消]」
 → 管理员群里点 ✅
 → 机器人校验审批人 → 远程触发 update-crypto-bot 任务
 → SSH 到服务器：git 切到该 tag → docker compose up -d --force-recreate → 健康检查（失败自动回滚）
 → 完成后群里回执「✅ 部署成功 / ❌ 部署失败」
```

**两个 Jenkins 任务分工**：
- `crypto-bot-notify`（webhook 触发）：只发审批消息，秒结束。
- `update-crypto-bot`（被机器人 API 触发）：真正部署，兼作手动部署/回滚工具。

**所需 Jenkins 凭据**：`github-token`（拉代码）、`crypto-bot-ssh`（SSH 到宿主机）、`telegram-bot-token` + `telegram-approve-chat`（通知任务发群消息）。

---

## 数据源与回退

查价/合约类功能按 **CoinGecko → OKX → 币安** 顺序回退，结果标注来源（如 `(OKX)` `(币安)`）。
- 币安**爆仓**无公开接口 → 提示改用 OKX；币安**新币榜**用合约上线时间近似。
- 币安 API 在部分地区（如中国大陆）可能被墙，连不上会优雅报错。

---

## 免责声明

所有数据仅供参考，不构成投资建议。
