// crypto-bot 部署流水线（语义化版本 vX.Y.Z + tag 推送到 GitHub）
//
// 流程：确定版本号 → SSH 部署到宿主机(健康检查/失败自动回滚) → 打 tag 并推送 GitHub
//
// 用法：Build with Parameters
//   - 直接 Build（三项默认）           → 部署 BRANCH 最新，自动在上个版本上 +patch（如 v1.0.3 → v1.0.4）并推送 tag
//   - RELEASE_TAG 填 v1.1.0 / v2.0.0   → 手动指定这次发版号（发小版本/大版本时用）
//   - ROLLBACK_TAG 填 v1.0.2           → 回滚到该已有版本（不打新 tag）
//   发布的 tag 会推到 GitHub，网页 Tags/Releases 里可见；回滚就按 tag。
pipeline {
    agent { label 'built-in' }

    parameters {
        string(name: 'BRANCH', defaultValue: 'main', description: '正常部署时拉取的分支')
        string(name: 'RELEASE_TAG', defaultValue: '', description: '手动指定发版号(如 v1.1.0)；留空=自动在上个版本上 +patch')
        string(name: 'ROLLBACK_TAG', defaultValue: '', description: '回滚到已有版本(如 v1.0.2)；填了则忽略发版，直接回滚，不打新 tag')
    }

    environment {
        DEPLOY_HOST = '172.17.0.1'
        DEPLOY_USER = 'root'
        DEPLOY_PATH = '/data/crypto-bot'
        SSH_CRED    = 'crypto-bot-ssh'
        GIT_CRED    = 'github-token'
        GIT_REPO    = 'github.com/logan775800/crypto-bot.git'
    }

    options {
        timestamps()
        timeout(time: 20, unit: 'MINUTES')
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    stages {
        stage('确定版本号') {
            when { expression { params.ROLLBACK_TAG.trim() == '' } }
            steps {
                script {
                    env.NEW_TAG = sh(returnStdout: true, script: '''
git fetch --tags --force >/dev/null 2>&1 || true
if [ -n "$RELEASE_TAG" ]; then
    echo "$RELEASE_TAG"
else
    LATEST=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]*' | sort -V | tail -1)
    if [ -z "$LATEST" ]; then
        echo "v1.0.0"
    else
        v=${LATEST#v}
        MA=$(echo "$v" | cut -d. -f1)
        MI=$(echo "$v" | cut -d. -f2)
        PA=$(echo "$v" | cut -d. -f3)
        echo "v$MA.$MI.$((PA+1))"
    fi
fi
''').trim()
                    echo "本次将发布版本号: ${env.NEW_TAG}"
                }
            }
        }

        stage('部署到服务器') {
            steps {
                withCredentials([sshUserPrivateKey(credentialsId: env.SSH_CRED, keyFileVariable: 'KEYFILE')]) {
                    sh '''
set -e
ssh -i "$KEYFILE" -o StrictHostKeyChecking=accept-new "$DEPLOY_USER@$DEPLOY_HOST" "ROLLBACK_TAG='$ROLLBACK_TAG' BRANCH='$BRANCH' DEPLOY_PATH='$DEPLOY_PATH' bash -s" <<'REMOTE'
set -e
cd "$DEPLOY_PATH"

PREV=$(git rev-parse --short HEAD)
echo "==== 当前 commit: $PREV ===="

git fetch --all --prune --tags

if [ -n "$ROLLBACK_TAG" ]; then
    TARGET="$ROLLBACK_TAG"
    echo "==== 回滚到版本: $TARGET ===="
else
    TARGET="origin/$BRANCH"
    echo "==== 部署分支最新: $BRANCH ===="
fi

git reset --hard "$TARGET"
DEPLOYED=$(git rev-parse --short HEAD)
echo "==== 目标 commit: $DEPLOYED ===="

# --force-recreate 保证重建容器、重新加载新代码
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
echo "✅ 服务器部署成功: $DEPLOYED"
REMOTE
'''
                }
            }
        }

        stage('打版本号并推送 GitHub') {
            when { expression { params.ROLLBACK_TAG.trim() == '' } }
            steps {
                withCredentials([usernamePassword(credentialsId: env.GIT_CRED, usernameVariable: 'GH_USER', passwordVariable: 'GH_TOKEN')]) {
                    sh '''
set -e
git config user.email "jenkins@ci.local"
git config user.name  "jenkins"
if git rev-parse "$NEW_TAG" >/dev/null 2>&1; then
    echo "❌ 版本号 $NEW_TAG 已存在，请换一个 RELEASE_TAG"; exit 1
fi
git tag -a "$NEW_TAG" -m "release $NEW_TAG"
git push "https://$GH_USER:$GH_TOKEN@$GIT_REPO" "$NEW_TAG"
echo "✅ 已发布版本 $NEW_TAG 到 GitHub"
'''
                }
            }
        }
    }

    post {
        success {
            script {
                if (params.ROLLBACK_TAG.trim() != '') {
                    echo "✅ 已回滚到 ${params.ROLLBACK_TAG}"
                } else {
                    echo "✅ 部署完成，版本号: ${env.NEW_TAG}"
                }
            }
        }
        failure { echo '❌ 失败（若为启动失败，服务器已自动回滚，看上面日志）' }
    }
}
