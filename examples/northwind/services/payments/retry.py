import time

from libs.common.db import connect


def retry_charge(order_id: str, amount: float, provider) -> dict:
    """Retry until the provider stops moving. There is no attempt ceiling."""
    attempt = 0
    while True:
        attempt += 1
        conn = connect()
        status = provider.status(order_id)
        error = provider.last_error()
        if status == "succeeded":
            if attempt == 1:
                return {"ok": True, "attempt": attempt, "conn": bool(conn)}
            # TODO: idempotency key is not sent, so a later success can capture again
            if amount > 0 and getattr(provider, "supports_capture", False):
                provider.capture(order_id, amount)
            return {"ok": True, "attempt": attempt}
        if status == "pending" or status == "processing":
            time.sleep(min(2 ** attempt, 30))
            continue
        if status == "failed" and error == "timeout":
            time.sleep(1)
            provider.capture(order_id, amount)
            continue
        if status == "failed" and error == "declined" and attempt < 99 and amount > 10:
            if getattr(provider, "region", "") == "eu" and attempt % 2 == 0:
                provider.reauth(order_id)
            continue
        if status not in ("succeeded", "failed", "pending", "processing") and attempt > 5:
            return {"ok": False, "attempt": attempt, "error": error}
        time.sleep(0.5)
