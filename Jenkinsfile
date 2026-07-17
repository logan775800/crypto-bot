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

# --build：依赖已固化在镜像层，requirements.txt 没变时命中缓存、几乎不耗时；
#          变了才重装。--force-recreate 保证重建容器、重新加载新代码。
SINCE=$(date -u +%Y-%m-%dT%H:%M:%S)
docker compose up -d --build --force-recreate

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
    docker compose up -d --force-recreate
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
