# crypto-bot 运维手册

面向日常运维：巡检、更新、回滚、看日志、排障、密钥轮换。配合 [README.md](README.md) 一起看。

## 关键信息速查

| 项 | 值 |
|----|----|
| 机器人 | **@cryptocurrencyuu_bot**（显示名 Cryptocurrency_bot） |
| 服务器 | logan-master |
| 代码/部署目录 | `/data/crypto-bot` |
| 容器名 | `crypto-bot` |
| 运行方式 | `docker compose`（挂载源码 + 启动时 pip install） |
| Jenkins | https://logan-jenkins.22889.club （容器内运行） |
| Jenkins 任务 | `crypto-bot-notify`（审批通知）、`update-crypto-bot`（部署） |
| 审批群 chat_id | **`-1003950673952`**（已升级为超级群；旧的 `-5339741894` 已失效） |
| 管理员/审批人 | `ADMIN_CHAT_ID`，**支持多个逗号分隔**，当前 `7774574457,927669631` |
| AI 模型 | `AI_MODEL`（当前 `gpt-5.6-terra-openai-compact`，中转站 kuaipao.ai/v1） |
| 启动成功标志(日志) | `Bot 启动中...`、`已加载 XXX 种币`、`命令菜单已设置` |

> **线上自查最快的方式：私聊机器人发 `/version`** —— 一条消息告诉你「当前版本 / AI模型是否配置 / Bybit 模拟盘还是实盘还是未配置 / 管理员几人 / 你是不是管理员」。版本号对不上就是部署没生效，不用再靠猜。

> 以下命令默认在服务器 `/data/crypto-bot` 目录执行。

---

## 一、日常常用命令

```bash
docker compose ps                 # 看容器状态(Up 才正常)
docker compose logs --tail=50     # 看最近日志
docker compose logs -f            # 实时日志(Ctrl+C 退出)
docker compose restart            # 重启
docker compose up -d --force-recreate   # 重建容器(改了 .env / compose 后必用)
docker compose exec crypto-bot env | grep -E "JENKINS|BOT|ADMIN"   # 看容器内环境变量
```

日志太吵时按需过滤：
```bash
docker compose logs --tail=500 | grep -E "ERROR|Traceback|出错"
docker compose logs -f | grep --line-buffered -E "ERROR|出错"
docker compose logs --since 10m | grep -E "..."
```

---

## 二、更新与回滚

### 正常更新（走审批流程，推荐）
开发者 push 代码后，群里会弹「🆕 新版本 vX.Y.Z 是否部署？」→ 管理员点 ✅ → 自动部署 → 群里回执。**运维平时只需在群里点确认。**

### 手动部署 / 回滚（Jenkins）
`update-crypto-bot` → Build with Parameters：
- `TAG` 留空 = 部署最新版本
- `TAG` 填 `v1.0.10` = 部署/回滚到该版本

### 命令行应急部署（Jenkins 不可用时）
```bash
cd /data/crypto-bot
git fetch --tags && git reset --hard v1.0.10   # 换成目标版本
docker compose up -d --force-recreate
docker compose logs -f          # 确认 "Bot 启动中..."
```

---

## 三、故障排查（症状 → 排查 → 解决）

### 1. 机器人整个没反应
```bash
docker compose ps          # 容器是否 Up
docker compose logs --tail=80
```
- 容器不在/退出 → `docker compose up -d --force-recreate`
- 日志有 `Traceback` → 看报错；多为代码问题，回滚到上一个好版本：`git reset --hard <上个tag> && docker compose up -d --force-recreate`
- 日志有 `InvalidToken` / `Unauthorized` → BOT_TOKEN 失效或错误，检查 `.env`

### 2. 某功能「获取失败/查询失败/分析失败」
```bash
docker compose logs --since 15m | grep 出错
```
- `分析出错 / xxx出错: ...` 多为**数据源限流或超时**（CoinGecko 免费额度有限），一般偶发，稍后重试。
- 若持续，看是不是 CoinGecko 挂了（`curl -s https://api.coingecko.com/api/v3/ping`）。
- Markdown 渲染类错误已做容错（自动降级纯文本），一般不会再出现「失败」。

### 3. 币安专区 / 币安回退 连不上
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://fapi.binance.com/fapi/v1/ping
```
- 非 200 / 超时 → 服务器网络到不了币安（常见于中国大陆 IP 被墙）。需给容器配代理，或换能访问币安的网络。OKX 不受影响。

### 4. 群里查价/底部键盘在群里不生效
- 去 @BotFather → `/setprivacy` → 选机器人 → **Disable**（关闭隐私模式），否则群里非命令消息机器人收不到。

### 5. CI/CD 审批链路排查（按环节）

**a) push 后群里没弹审批消息**
- 看 GitHub repo → Settings → Webhooks → Recent Deliveries：红❌ 说明没通到 Jenkins。
- 看 Jenkins `crypto-bot-notify` 有没有触发、日志报什么：
  - 没触发 → 该任务没勾「GitHub hook trigger」，或 SCM 配的不是 Git。
  - `Telegram 返回 HTTP 404` → `telegram-bot-token` 凭据填错（必须是纯 token，无多余字）。
  - `HTTP 400 chat not found` → `telegram-approve-chat` 群ID 不对，或机器人不在群里。
  - **群升级成 supergroup 后群ID会变**（变 `-100...`）→ 群里 `/id` 重新拿ID，更新 `telegram-approve-chat` 凭据。

**b) 点 ✅ 后报错**
- `触发部署失败：Jenkins 未配置` → 容器没读到 JENKINS_ 变量：
  ```bash
  docker compose exec crypto-bot env | grep JENKINS   # 看有没有
  ```
  没有 → `.env` 缺变量 或 改完没重建。补 `.env` 后 `docker compose up -d --force-recreate`。
- `触发部署失败：HTTP 403` → Jenkins 认证不足。用 **Jenkins 用户 API Token**（`JENKINS_USER`+`JENKINS_API_TOKEN`），且该用户对 `update-crypto-bot` 有 Build 权限。
- `触发部署失败：HTTP 404` → `JENKINS_URL` 或 `JENKINS_JOB` 名字不对。
- 按钮没了（之前点过一次失败）→ 失败会保留「🔁 重试」按钮；若确实没了，`crypto-bot-notify` 点 Build Now 重发。

**c) 部署任务本身失败**
- 看 `update-crypto-bot` 日志：
  - `can't cd to ...` → `DEPLOY_PATH` 不对。
  - SSH 相关 → `crypto-bot-ssh` 凭据/宿主机 authorized_keys 问题。
  - 健康检查失败 → 会**自动回滚**到部署前版本并重启，任务标红。看日志里 `检测到启动错误` 上下文定位新版本为何起不来。

---

## 三之二、常见变更操作（踩过坑，按这个来）

### A. 更换 Telegram 机器人
代码零改动，但**三处必须同步**，否则链路会断：
1. @BotFather `/newbot` 拿新 token
2. 服务器 `.env` 改 `BOT_TOKEN=<新>` → `docker compose up -d --force-recreate`
3. ⚠️ **Jenkins 凭据 `telegram-bot-token` 改成同一个新 token** —— 审批消息由 Jenkins 用它发出，按钮点击又由机器人接收，两边**必须是同一个 bot**，否则「点确认部署没反应」
4. 把新机器人拉进群（含审批群）；**BotFather 关 Group Privacy 后移出群再重新拉一次**
5. 私聊订阅者需重新 `/start` 新机器人（机器人不能主动私信没打过招呼的人）；群订阅因群 id 不变而自动保留

> 数据不丢：虚拟仓/持仓/预警按 **user_id** 存，换机器人 user_id 不变。

### B. 更换 AI 模型
只改 `.env`，**不用改代码、不用走 Jenkins**：
```bash
# 例：AI_MODEL=gpt-5.6-terra-openai-compact
vi .env && docker compose up -d --force-recreate
```
- 中转站是 **OpenAI 兼容**（`/chat/completions`）。新模型**必须支持工具调用(function calling)**，否则群里 AI 的实时数据能力会自动降级为纯聊天。
- 中转站令牌建议：**模型限制留空**（支持所有模型）+ **过期时间设永不过期**，免得被锁死在一个没货的模型上。
- 排障：`403`=令牌不允许该模型/模型名写错；`503`=中转站上游临时没货（换个模型或等）。Claude 系正确 id 是 `claude-opus-4-8`（**中划线**，不是 `4.8`）。

### C. 群升级为超级群（chat_id 会变）
- 机器人**已自动迁移**订阅（监听升级事件 → 旧 id 全量迁到新 id → 群里回执）。
- 历史遗留（升级发生在该功能之前）：管理员在目标群发 **`/migratechat <旧群id>`** 手动迁。
- ⚠️ **Jenkins 凭据 `telegram-approve-chat` 要手动改成新群 id**，否则审批消息发不出（报 `400 group chat was upgraded`，响应里 `migrate_to_chat_id` 就是新 id）。

### D. 群里纯文字功能失效（发币名不出价、设监控没反应）
99% 是**隐私模式没关**：BotFather → `/mybots` → Bot Settings → **Group Privacy → Turn off** → **移出群再重新拉进去**。
自查：群里发一个纯 `BTC`，出价格=隐私已关；没反应=还开着。

### E. 别开「匿名管理员」
匿名发言时 Telegram 把发送者标成 `GroupAnonymousBot`（user_id `1087968824`，不是人）。若「点按钮时是本人、发文字时是匿名」，按 user_id 存的引导状态会对不上，流程静默失败。群 → 管理员 → 自己 → 关掉「保持匿名」。

---

## 四、密钥轮换（建议定期或泄露后）

1. **BOT_TOKEN**：@BotFather → `/revoke` → 拿新 token → 更新 `.env` 的 `BOT_TOKEN` + **Jenkins 凭据 `telegram-bot-token`（必须同一个）** → `docker compose up -d --force-recreate`。
2. **Jenkins API Token**：Jenkins 用户 → Security → 撤销旧 token、生成新 → 更新 `.env` 的 `JENKINS_API_TOKEN` → 重建容器。
3. **AI_API_KEY**：中转站后台重置 → 更新 `.env` → 重建。
4. **BYBIT_API_KEY/SECRET**：Bybit 后台重建（**只勾合约下单+持仓，绝不勾提币**，加服务器 IP 白名单）→ 更新 `.env` → 重建。先用 `BYBIT_TESTNET=true` 验证：`docker compose exec crypto-bot python bybit_trade.py` 冒烟（只读不下单）。

> 改任何 `.env` 后都要 `docker compose up -d --force-recreate` 才生效。

---

## 五、数据与备份

- 运行数据在 `data.json`（预警、持仓、订阅等），已做原子写入。`backups/` 有每日自动备份。
- `data.json`、`backups/`、`.env` 都在 `.gitignore` 里，不会进仓库。
- 迁移服务器：拷 `.env` + `data.json` 到新机同目录即可（代码 `git clone`）。

---

## 六、健康检查说明

部署流水线判断「起没起来」的依据：容器日志在重启后出现 `Bot 启动中`（成功），或出现 `Traceback/ImportError/InvalidToken` 等（失败→自动回滚）。最多等约 6 分钟（首次 pip install 较慢属正常）。
