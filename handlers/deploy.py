"""部署审批：机器人收到"确认"按钮后，远程触发 Jenkins 部署任务。

优先用「用户 + API Token」的 Basic 认证（最可靠，不受 CSRF/匿名权限影响）；
没配用户则退回「构建令牌」方式。
"""
import logging
import httpx
from config import (
    JENKINS_URL, JENKINS_JOB, JENKINS_DEPLOY_TOKEN,
    JENKINS_USER, JENKINS_API_TOKEN,
)


async def trigger_deploy(tag):
    """远程触发 Jenkins 部署指定 tag。返回 (是否成功, 说明)。"""
    if not JENKINS_URL:
        return False, "缺 JENKINS_URL"
    url = f"{JENKINS_URL.rstrip('/')}/job/{JENKINS_JOB}/buildWithParameters"
    params = {"TAG": tag}
    auth = None
    if JENKINS_USER and JENKINS_API_TOKEN:
        auth = (JENKINS_USER, JENKINS_API_TOKEN)      # 推荐：API Token
    elif JENKINS_DEPLOY_TOKEN:
        params["token"] = JENKINS_DEPLOY_TOKEN         # 备选：构建令牌
    else:
        return False, "缺 Jenkins 认证(JENKINS_USER+JENKINS_API_TOKEN 或 JENKINS_DEPLOY_TOKEN)"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, params=params, auth=auth)
        if resp.status_code in (200, 201):
            return True, "ok"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        logging.error(f"触发 Jenkins 部署失败: {e}")
        return False, str(e)[:120]
