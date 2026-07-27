from __future__ import annotations

from collections.abc import Sequence

import httpx

from workflow.config import Settings
from workflow.probes.storage import ProbeValidationError


async def send_wecom_probe(
    settings: Settings,
    message: str,
    mentioned_userids: Sequence[str],
) -> dict[str, object]:
    if not settings.t0_allow_wecom_send:
        return {
            "configured": bool(settings.wecom_group_webhook_url),
            "sent": False,
            "reason": "T0_ALLOW_WECOM_SEND=false，未发送真实群消息。",
        }
    if settings.wecom_group_webhook_url is None:
        raise ProbeValidationError("未配置 WECOM_GROUP_WEBHOOK_URL。")
    if not message.strip():
        raise ProbeValidationError("企业微信探针消息不能为空。")

    known_userids = settings.known_wecom_userids()
    requested = {userid.strip() for userid in mentioned_userids if userid.strip()}
    unknown = requested - known_userids
    if unknown:
        raise ProbeValidationError(
            f"存在未在 .env 中登记的企业微信 userid：{', '.join(sorted(unknown))}"
        )
    if not requested:
        raise ProbeValidationError("至少提供一个已配置的企业微信 userid。")

    payload = {
        "msgtype": "text",
        "text": {
            "content": f"[T0 技术验证] {message.strip()}",
            "mentioned_list": sorted(requested),
        },
    }
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        response = await client.post(
            settings.wecom_group_webhook_url.get_secret_value(),
            json=payload,
        )
        response.raise_for_status()
        result = response.json()

    sent = result.get("errcode") == 0
    return {
        "configured": True,
        "sent": sent,
        "wecom_errcode": result.get("errcode"),
        "wecom_errmsg": result.get("errmsg"),
        "mentioned_count": len(requested),
    }
