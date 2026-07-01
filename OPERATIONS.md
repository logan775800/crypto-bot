# crypto-bot 运维手册

面向日常运维：巡检、更新、回滚、看日志、排障、密钥轮换。配合 [README.md](README.md) 一起看。

## 关键信息速查

| 项 | 值 |
|----|----|
| 服务器 | logan-master |
| 代码/部署目录 | `/data/crypto-bot` |
| 容器名 | `crypto-bot` |
| 运行方式 | `docker compose`（挂载源码 + 启动时 pip install） |
| Jenkins | https://logan-jenkins.22889.club （容器内运行） |
| Jenkins 任务 | `crypto-bot-notify`（审批通知）、`update-crypto-bot`（部署） |
| 审批群 chat_id | `-5339741894` |
| 管理员/审批人 user_id | `7774574457`（= `ADMIN_CHAT_ID`） |
| 启动成功标志(日志) | `Bot 启动中...`、`已加载 XXX 种币`、`命令菜单已设置` |

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

## 四、密钥轮换（建议定期或泄露后）

1. **BOT_TOKEN**：@BotFather → `/revoke` → 拿新 token → 更新 `.env` 的 `BOT_TOKEN` + Jenkins 凭据 `telegram-bot-token` → `docker compose up -d --force-recreate`。
2. **Jenkins API Token**：Jenkins 用户 → Security → 撤销旧 token、生成新 → 更新 `.env` 的 `JENKINS_API_TOKEN` → 重建容器。
3. **AI_API_KEY**：中转站后台重置 → 更新 `.env` → 重建。

> 改任何 `.env` 后都要 `docker compose up -d --force-recreate` 才生效。

---

## 五、数据与备份

- 运行数据在 `data.json`（预警、持仓、订阅等），已做原子写入。`backups/` 有每日自动备份。
- `data.json`、`backups/`、`.env` 都在 `.gitignore` 里，不会进仓库。
- 迁移服务器：拷 `.env` + `data.json` 到新机同目录即可（代码 `git clone`）。

---

## 六、健康检查说明

部署流水线判断「起没起来」的依据：容器日志在重启后出现 `Bot 启动中`（成功），或出现 `Traceback/ImportError/InvalidToken` 等（失败→自动回滚）。最多等约 6 分钟（首次 pip install 较慢属正常）。
