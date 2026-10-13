"""Web Push 发送。

浏览器订阅存在 ``push_subscriptions`` 里，发的时候按成员挑出来逐条发。
推送端点是浏览器厂商的服务（Google/Mozilla/Apple），我们只调用，不维护。

配置来自 ``SGU_ACW_VAPID_PRIVATE_KEY`` / ``SGU_ACW_VAPID_PUBLIC_KEY`` /
``SGU_ACW_VAPID_SUBJECT``（mailto: 或 https:，标准要求的归属标识）。

没配密钥时 ``send`` 直接返回 0，不报错——推送是可降级的能力，站点其他
功能不受影响。
"""

from __future__ import annotations

import json
import logging

from pywebpush import WebPushException, webpush
from sqlmodel import Session, select

from .config import get_settings
from .models import PushSubscription

logger = logging.getLogger("sgu_acw.push")


def public_key() -> str:
    return get_settings().vapid_public_key


def configured() -> bool:
    s = get_settings()
    return bool(s.vapid_private_key and s.vapid_public_key)


def send_to_user_ids(
    db: Session,
    user_ids: list[int] | set[int],
    payload: dict,
) -> dict:
    """给一批成员的所有设备推一条。返回送达统计。"""
    if not configured() or not user_ids:
        return {"sent": 0, "failed": 0, "dead": 0}
    subs = db.exec(
        select(PushSubscription).where(PushSubscription.user_id.in_(user_ids))  # type: ignore[attr-defined]
    ).all()
    s = get_settings()
    sent = failed = dead = 0
    body = json.dumps(payload, ensure_ascii=False)
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=body,
                vapid_private_key=s.vapid_private_key,
                vapid_claims={"sub": s.vapid_subject},
                # TTL=0（pywebpush 默认）会被 WNS 拒：它要求 TTL>=1 配合
                # 缓存策略，收到 0 直接 400「Ttl value conflicts with
                # X-WNS-Cache-Policy」。给 24h——设备离线重连还能补收。
                ttl=86400,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", 0) or 0
            if status in (404, 410):
                # 订阅已失效（浏览器清了权限或换设备），删掉免得下次白试。
                db.delete(sub)
                dead += 1
            else:
                failed += 1
                # 把响应头和 body 都打出来：各家推送服务的失败原因都藏
                # 在 X-WNS-*/Retry-After 之类的头里，光看状态码猜不出来。
                resp = exc.response
                body_snippet = (resp.text or "")[:200] if resp is not None else ""
                interesting = {"x-wns", "retry"}
                headers_snippet = (
                    {
                        k: v
                        for k, v in resp.headers.items()
                        if k.lower().startswith(tuple(interesting))
                    }
                    if resp is not None
                    else {}
                )
                logger.warning(
                    "webpush %s failed: %s status=%s headers=%s body=%s",
                    sub.endpoint[:60],
                    exc,
                    status,
                    headers_snippet,
                    body_snippet,
                )
    if dead:
        db.commit()
    return {"sent": sent, "failed": failed, "dead": dead}
