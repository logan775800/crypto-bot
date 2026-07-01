// crypto-bot 部署流水线
// 作用：在 Jenkins 容器里通过 SSH 连到宿主机，git 更新代码 + 重建容器 + 健康检查，失败自动回滚。
// 版本号：每次正常部署自动打递增 tag（main-1.01 / main-1.02 ...），回滚按版本号即可。
//
// 用法：Build with Parameters
//   - 直接 Build（三项默认）        → 部署 BRANCH 最新，并自动生成新版本号
//   - 勾选 ROLLBACK                → 回到"上一个版本号"
//   - VERSION 填版本号(如 main-1.02) → 回滚/部署到该版本（也可填分支名或完整 SHA）
//   每次构建日志都会列出"已有版本号"，方便挑选要回滚哪个。
pipeline {
    agent { label 'built-in' }   // 在 Jenkins(容器)本机执行，仅用于发起 SSH

    parameters {
        booleanParam(name: 'ROLLBACK', defaultValue: false, description: '勾选=回到上一个版本号（忽略 BRANCH/VERSION）')
        string(name: 'BRANCH', defaultValue: 'main', description: '正常部署时拉取的分支')
        string(name: 'VERSION', defaultValue: '', description: '回滚用：填版本号如 main-1.02（也可填分支名/完整SHA）；留空=部署分支最新')
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
ssh -i "$KEYFILE" -o StrictHostKeyChecking=accept-new "$DEPLOY_USER@$DEPLOY_HOST" "ROLLBACK='$ROLLBACK' BRANCH='$BRANCH' VERSION='$VERSION' DEPLOY_PATH='$DEPLOY_PATH' bash -s" <<'REMOTE'
set -e
cd "$DEPLOY_PATH"

PREV=$(git rev-parse --short HEAD)
echo "==== 当前 commit: $PREV ===="
echo "---- 已有版本号(最近10个) ----"
git tag -l 'main-*' | sort -V | tail -10 || true
echo "------------------------------"

git fetch --all --prune

MAKE_TAG=0
if [ "$ROLLBACK" = "true" ]; then
    TARGET=$(git tag -l 'main-*' | sort -V | tail -2 | head -1)
    [ -z "$TARGET" ] && { echo "❌ 没有可回退的历史版本号"; exit 1; }
    echo "==== 回到上一个版本号: $TARGET ===="
elif [ -n "$VERSION" ]; then
    TARGET="$VERSION"
    echo "==== 部署指定版本: $TARGET ===="
else
    TARGET="origin/$BRANCH"
    MAKE_TAG=1
    echo "==== 部署分支最新: $BRANCH ===="
fi

git reset --hard "$TARGET"
DEPLOYED=$(git rev-parse --short HEAD)
echo "==== 目标 commit: $DEPLOYED ===="

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

# 仅正常部署时自动打一个递增版本号 tag（回滚/指定版本不打新号）
if [ "$MAKE_TAG" = "1" ]; then
    LAST=$(git tag -l 'main-*' | sed 's/^main-//' | sort -V | tail -1)
    if [ -z "$LAST" ]; then
        NEXT="1.01"
    else
        NEXT=$(awk -v n="$LAST" 'BEGIN{printf "%.2f", n+0.01}')
    fi
    NEWTAG="main-$NEXT"
    git tag -f "$NEWTAG" HEAD
    echo "✅ 部署成功: $DEPLOYED   新版本号: $NEWTAG"
else
    echo "✅ 部署成功: $DEPLOYED   (版本: $TARGET)"
fi
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
