"""部署审批：机器人收到"确认"按钮后，远程触发部署任务（后端为 Jenkins）。

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
    """远程触发部署指定 tag。返回 (是否成功, 说明)。"""
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
        return False, "缺部署认证配置"
    try:
        # follow_redirects：部署服务触发成功后常返回 302/303 重定向到队列/构建页，
        # 跟随后拿到最终 2xx；同时把任何 <400 都视为"已触发"(3xx=已排队/重定向)。
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.post(url, params=params, auth=auth)
        if resp.status_code < 400:
            return True, "ok"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        logging.error(f"触发部署失败: {e}")
        return False, str(e)[:120]
