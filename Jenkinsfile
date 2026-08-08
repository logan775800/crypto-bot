// crypto-bot 部署流水线（按版本 tag 部署 / 回滚）
//
// 版本号由开发者推代码时打好 tag（vX.Y.Z）并推到 GitHub；本流水线只负责按 tag 部署。
//
// 用法：Build with Parameters
//   - 直接 Build（TAG 留空）  → 部署 GitHub 上最新的版本 tag
//   - TAG 填 v1.0.2           → 部署/回滚到该版本
//
// 部署到宿主机：SSH 过去 git 切到该 tag + 重建容器 + 健康检查，失败自动回滚到部署前的状态。
pipeline {
    agent { label 'built-in' }

    parameters {
        string(name: 'TAG', defaultValue: '', description: '要部署的版本 tag（如 v1.0.2）；留空=部署最新 tag。回滚就填要回到的旧 tag。')
    }

    environment {
        DEPLOY_HOST = '172.17.0.1'
        DEPLOY_USER = 'root'
        DEPLOY_PATH = '/data/crypto-bot'
        SSH_CRED    = 'crypto-bot-ssh'
    }

    options {
        timestamps()
        timeout(time: 20, unit: 'MINUTES')
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    stages {
        stage('部署到服务器') {
            steps {
                withCredentials([sshUserPrivateKey(credentialsId: env.SSH_CRED, keyFileVariable: 'KEYFILE')]) {
                    sh '''
set -e
ssh -i "$KEYFILE" -o StrictHostKeyChecking=accept-new "$DEPLOY_USER@$DEPLOY_HOST" "TAG='$TAG' DEPLOY_PATH='$DEPLOY_PATH' bash -s" <<'REMOTE'
set -e
cd "$DEPLOY_PATH"

PREV=$(git rev-parse --short HEAD)
echo "==== 当前 commit: $PREV ===="

git fetch --all --prune --tags --force

if [ -n "$TAG" ]; then
    TARGET="$TAG"
else
    TARGET=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]*' | sort -V | tail -1)
    [ -z "$TARGET" ] && { echo "❌ 仓库里没有任何版本 tag，先推一个"; exit 1; }
fi
echo "==== 部署版本: $TARGET ===="

git reset --hard "$TARGET"
DEPLOYED=$(git rev-parse --short HEAD)
echo "==== 目标 commit: $DEPLOYED ===="

# 依赖已固化在镜像层：requirements.txt 没变时 build 命中缓存、几乎不耗时，变了才重装。
echo "==== 构建镜像 ===="
docker compose build || { echo "❌ 镜像构建失败，回滚代码"; git reset --hard "$PREV"; exit 1; }

# 先跑单元测试再动运行中的容器：不过就中止 + 把代码回退回去，线上完全不受影响。
# 用 docker run（而非 compose run）避免与 container_name 撞名。
#
# ⚠️ -e DATA_FILE 是必须的：这里把**生产目录**挂进 /app，而 DATA_FILE 默认就是
# /app/data.json。不覆盖它的话，测试里的清理夹具会把订阅、风控参数直接写回
# 生产数据（2026-08-07 实际发生过：每次部署都清空合约异动订阅并禁用实盘下单）。
# conftest 里也做了同样的隔离，这里是第二道——两处都改坏才可能再出事。
echo "==== 单元测试 ===="
docker run --rm -v "$PWD":/app -w /app -e DATA_FILE=/tmp/pytest_data.json \
    crypto-bot:local python -m pytest -q tests/ || {
    echo "❌ 单元测试未通过，中止部署（运行中的容器未改动），代码回退到 $PREV"
    git reset --hard "$PREV"
    exit 1
}

# 先杀掉所有残留容器（含游离在 compose 之外的僵尸），再重建。
# 否则残留的旧容器会和新容器同时长轮询同一个 bot token，Telegram 把消息随机
# 分给两边 → 新命令时灵时不灵（旧容器没有该 handler 就静默丢弃）。这是历史踩过的坑。
#
# 为什么按 name 也扫一遍：`--filter ancestor=crypto-bot:local` 会把这个 tag
# 解析成**当前**镜像 ID，而 `docker compose build` 每次都生成新镜像并把 tag
# 指过去——上一个容器的镜像 ID 从此不再匹配，这条过滤器就扫不到它了。
# 2026-08-08 双实例复发就是这么漏的。两个过滤器取并集才扫得干净。
echo "==== 清理残留容器（防双实例抢 token）===="
{ docker ps -aq --filter ancestor=crypto-bot:local
  docker ps -aq --filter "name=^/crypto-bot"; } | sort -u | xargs -r docker rm -f >/dev/null 2>&1 || true
# --force-recreate 保证重建容器、重新加载新代码；--remove-orphans 清掉孤儿服务容器
SINCE=$(date -u +%Y-%m-%dT%H:%M:%S)
docker compose up -d --force-recreate --remove-orphans

echo "==== 健康检查(最多约6分钟) ===="
ok=0
for i in $(seq 1 36); do
    sleep 10
    logs=$(docker compose logs --since "$SINCE" 2>/dev/null || true)
    echo "$logs" | grep -q "Bot 启动中" && { ok=1; break; }
    echo "$logs" | grep -qE "Traceback|ModuleNotFoundError|SyntaxError|ImportError|InvalidToken" && { echo "检测到启动错误"; break; }
done

if [ "$ok" != "1" ]; then
    echo "❌ 新版本未正常启动，自动回滚到 $PREV"
    git reset --hard "$PREV"
    { docker ps -aq --filter ancestor=crypto-bot:local
  docker ps -aq --filter "name=^/crypto-bot"; } | sort -u | xargs -r docker rm -f >/dev/null 2>&1 || true
    docker compose up -d --force-recreate --remove-orphans
    exit 1
fi
echo "✅ 部署成功: 版本 $TARGET (commit $DEPLOYED)"
REMOTE
'''
                }
            }
        }
    }

    post {
        always {
            script {
                def tag = (params.TAG && params.TAG.trim()) ? params.TAG.trim() : '最新版本'
                def msg = (currentBuild.currentResult == 'SUCCESS') ?
                    "✅ 部署成功: ${tag}" :
                    "❌ 部署失败: ${tag}（若为启动失败，服务器已自动回滚）"
                try {
                    withCredentials([
                        string(credentialsId: 'telegram-bot-token', variable: 'BOT'),
                        string(credentialsId: 'telegram-approve-chat', variable: 'CHAT')
                    ]) {
                        withEnv(["TG_MSG=${msg}"]) {
                            sh '''
set +x
BOT=$(printf %s "$BOT" | tr -d '[:space:]')
CHAT=$(printf %s "$CHAT" | tr -d '[:space:]')
curl -s -X POST "https://api.telegram.org/bot$BOT/sendMessage" \
  --data-urlencode "chat_id=$CHAT" \
  --data-urlencode "text=$TG_MSG" >/dev/null || true
'''
                        }
                    }
                } catch (e) {
                    echo "结果通知发送失败(忽略): ${e}"
                }
            }
        }
    }
}
