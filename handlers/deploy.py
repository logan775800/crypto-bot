"""部署审批：机器人收到"确认"按钮后，远程触发 Jenkins 部署任务。"""
import logging
import httpx
from config import JENKINS_URL, JENKINS_JOB, JENKINS_DEPLOY_TOKEN


async def trigger_deploy(tag):
    """远程触发 Jenkins 部署指定 tag。返回 (是否成功, 说明)。"""
    if not JENKINS_URL or not JENKINS_DEPLOY_TOKEN:
        return False, "Jenkins 未配置(缺 JENKINS_URL / JENKINS_DEPLOY_TOKEN)"
    url = f"{JENKINS_URL.rstrip('/')}/job/{JENKINS_JOB}/buildWithParameters"
    params = {"token": JENKINS_DEPLOY_TOKEN, "TAG": tag}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, params=params)
        if resp.status_code in (200, 201):
            return True, "ok"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        logging.error(f"触发 Jenkins 部署失败: {e}")
        return False, str(e)[:120]
