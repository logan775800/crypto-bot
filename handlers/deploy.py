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


# 光报「HTTP 503」看不出该重试还是该改配置——前者点一下就行，后者点一百次也没用。
# 这几个码在这条链路上的含义是确定的，直接说清楚。
_WHY = {
    401: "认证被拒（Jenkins 用户或 API Token 失效）——重试没用，要改配置",
    403: "没有触发构建的权限（或 Token 过期）——重试没用，要改配置",
    404: "找不到这个部署任务（JENKINS_JOB 名字对不上）——重试没用，要改配置",
    500: "部署系统内部错误，看它的日志",
    502: "网关拿不到部署系统的响应（多半正在重启）——稍等重试",
    503: "部署系统暂时不可用（通常是它正在启动/重启，启动期间对所有请求都回这个）"
         "——等一分钟点重试",
    504: "部署系统响应超时（可能正忙）——稍等重试",
}


def explain(status):
    return _WHY.get(status, f"HTTP {status}")


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
        logging.error(f"触发部署失败 HTTP {resp.status_code}: {resp.text[:200]}")
        return False, f"HTTP {resp.status_code} · {explain(resp.status_code)}"
    except httpx.TimeoutException:
        logging.error("触发部署超时")
        return False, "连不上部署系统（超时）——它可能没在运行，稍等重试"
    except Exception as e:
        logging.error(f"触发部署失败: {e}")
        return False, f"{str(e)[:100]}（网络或部署系统的问题，代码没动）"
