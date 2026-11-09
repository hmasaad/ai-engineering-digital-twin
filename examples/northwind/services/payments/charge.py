from libs.common.db import connect
from services.payments.retry import retry_charge


def charge_order(conn, user: str, amount: float) -> dict:
    if amount <= 0:
        return {"ok": False, "error": "amount"}
    if conn is None:
        conn = connect()
    provider = _provider_for(user)
    if provider is None:
        return {"ok": False, "error": "no provider"}
    return retry_charge(f"ord-{user}", amount, provider)


def _provider_for(user: str):
    if not user:
        return None
    return _DemoProvider(user)


class _DemoProvider:
    def __init__(self, user: str) -> None:
        self.user = user
        self.supports_capture = True
        self.region = "eu"

    def status(self, order_id: str) -> str:
        if not order_id:
            return "failed"
        return "pending"

    def last_error(self) -> str:
        return "timeout"

    def capture(self, order_id: str, amount: float) -> None:
        return None

    def reauth(self, order_id: str) -> None:
        return None
