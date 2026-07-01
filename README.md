# crypto-bot · Telegram 加密货币行情机器人

一个基于 `python-telegram-bot` 的 Telegram 机器人：查币价、技术分析、AI 解读、价格预警、持仓记录、市场异动/新闻/解锁推送，以及 OKX / 币安交易所专区。数据源支持 **CoinGecko → OKX → 币安** 三级回退，并标注来源。

---

## 功能一览

- **行情**：直接发币名即可查价（现货+合约，带来源标注）；`/dashboard` 市值 Top15 看板；`/top` 涨跌榜 Top15
- **分析**：`/analyze` 技术指标（RSI/均线/MACD/布林带/支撑阻力）、`/ai` AI 解读
- **交易所专区**：🔥 OKX / 🅱️ 币安（新币榜、涨幅榜、资金费率、多空比、爆仓、合约行情）
- **价格预警**：菜单里点选币种→方向→发价格，或 `/alert`；支持查看/取消
- **Gas 提醒**：`/gas` 多链 gas；`/gasalert 15` 跌破阈值主动通知
- **套利监控**：`/arb` 多所比价(扣手续费净价差)；`/arbwatch 0.8` 跨所净价差告警
- **巨鲸地址追踪**：`/track 0x地址` 关注地址，ETH/代币转账即时通知（需 Etherscan key）
- **持仓**（私聊）：`/buy` `/sell` `/portfolio` `/ranking`
- **订阅推送**：市场异动告警、新闻、代币解锁、每日总结/播报
- **实用工具**：恐惧贪婪指数、Gas、巨鲸监控、多所比价

> 交互以 **/menu 菜单 + 底部常驻键盘** 为主，绝大多数功能点按钮即可，无需记命令。

---

## 目录结构

```
bot.py              入口：注册命令/回调/定时任务
config.py           配置（从环境变量读取）
api.py              CoinGecko 数据源封装（缓存+限流）
indicators.py       技术指标（纯 Python 实现）
storage.py          JSON 持久化（原子写入）
handlers/
  menu.py           菜单 + 所有内联按钮回调
  quickprice.py     发币名直接查价（三级回退）
  price.py          /price /top /info /compare /calc
  analysis.py       技术分析 / 多周期 / KDJ
  ai.py             AI 解读
  okx.py            OKX 专区（查不到自动回退币安）
  binance.py        币安专区
  alert.py          价格预警
  portfolio.py      持仓
  market_alert.py   市场异动扫描
  news.py unlock.py summary.py movers.py ...  各类推送
  util.py           Markdown 转义 + 容错发送
  deploy.py         审批部署：触发 Jenkins
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
| `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` | AI 解读用的中转站配置（可选） |
| `ADMIN_CHAT_ID` | 管理员 chat_id：接运维告警 + **唯一部署审批人** |
| `JENKINS_URL` / `JENKINS_JOB` | Jenkins 地址与部署任务名 |
| `JENKINS_USER` / `JENKINS_API_TOKEN` | 触发部署用的 Jenkins 用户 API Token（推荐） |
| `JENKINS_DEPLOY_TOKEN` | 备选：任务「触发远程构建」令牌 |
| `ETHERSCAN_API_KEY` | 巨鲸地址追踪用（免费申请 etherscan.io/apis） |

> `.env` 已被 `.gitignore` 忽略，切勿提交。群里发 `/id` 可查当前会话的 chat_id。

### 群里使用注意
- 需在 @BotFather 关闭机器人隐私模式（`/setprivacy` → Disable），群里「发币名查价」「底部键盘」才生效。

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
