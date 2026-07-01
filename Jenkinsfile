// crypto-bot 部署流水线
// 作用：在 Jenkins 容器里通过 SSH 连到宿主机，git 更新代码 + 重建容器 + 健康检查，失败自动回滚。
// 用法：Build with Parameters
//   - 直接 Build            → 拉取 BRANCH 最新代码并部署
//   - 勾选 ROLLBACK         → 回滚到上一次成功部署的版本
//   - 填写 COMMIT           → 部署指定 commit（精确回滚）
pipeline {
    agent { label 'built-in' }   // 在 Jenkins(容器)本机执行，仅用于发起 SSH

    parameters {
        booleanParam(name: 'ROLLBACK', defaultValue: false, description: '勾选=回滚到上一次成功部署的版本（忽略 BRANCH/COMMIT）')
        string(name: 'BRANCH', defaultValue: 'main', description: '正常部署时拉取的分支')
        string(name: 'COMMIT', defaultValue: '', description: '可选：部署指定 commit（填了则忽略 BRANCH）')
    }

    environment {
        DEPLOY_HOST = '172.17.0.1'        // Jenkins 容器访问宿主机的地址（docker 网桥网关）
        DEPLOY_USER = 'root'
        DEPLOY_PATH = '/data/crypto-bot'  // 宿主机上仓库路径（含 docker-compose.yml）
        SSH_CRED    = 'crypto-bot-ssh'    // Jenkins 里的 SSH 私钥凭据 ID
    }

    options {
        timestamps()
        timeout(time: 20, unit: 'MINUTES')
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    stages {
        stage('部署') {
            steps {
                withCredentials([sshUserPrivateKey(credentialsId: env.SSH_CRED, keyFileVariable: 'KEYFILE')]) {
                    // 整段用单引号，Groovy 不解析；参数/environment/KEYFILE 都由 Jenkins 注入成 shell 环境变量
                    sh '''
set -e
ssh -i "$KEYFILE" -o StrictHostKeyChecking=accept-new "$DEPLOY_USER@$DEPLOY_HOST" "ROLLBACK='$ROLLBACK' BRANCH='$BRANCH' COMMIT='$COMMIT' DEPLOY_PATH='$DEPLOY_PATH' bash -s" <<'REMOTE'
set -e
cd "$DEPLOY_PATH"

PREV=$(git rev-parse HEAD)
echo "==== 当前版本(回滚点): $PREV ===="

git fetch --all --prune

if [ "$ROLLBACK" = "true" ]; then
    TARGET=$(cat .deploy_prev 2>/dev/null || true)
    [ -z "$TARGET" ] && { echo "❌ 无可回滚版本(.deploy_prev 为空)"; exit 1; }
    echo "==== 回滚到: $TARGET ===="
elif [ -n "$COMMIT" ]; then
    TARGET="$COMMIT"
    echo "==== 部署指定提交: $TARGET ===="
else
    TARGET="origin/$BRANCH"
    echo "==== 部署分支最新: $BRANCH ===="
fi

git reset --hard "$TARGET"
DEPLOYED=$(git rev-parse --short HEAD)
echo "==== 目标版本: $DEPLOYED ===="

# --force-recreate 保证重建容器、重新加载挂载进去的新代码
SINCE=$(date -u +%Y-%m-%dT%H:%M:%S)
docker compose up -d --force-recreate

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

# 仅正常部署时记录回滚点（回滚操作不覆盖）
[ "$ROLLBACK" != "true" ] && echo "$PREV" > .deploy_prev
echo "✅ 部署成功: $DEPLOYED"
REMOTE
'''
                }
            }
        }
    }

    post {
        success { echo '✅ 部署完成' }
        failure { echo '❌ 部署失败（若为启动失败已自动回滚，看上面日志）' }
    }
}
