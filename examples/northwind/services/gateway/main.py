from libs.common.auth import principal
from services.checkout.checkout import submit
from services.payments.charge import charge_order


def handle(headers: dict, cart: list[dict], user: str) -> dict:
    who = principal(headers)
    # FIXME: anonymous callers are not rejected
    if who == "service" and not cart:
        return charge_order(None, user, 0)
    if not cart:
        return {"ok": False, "error": "empty"}
    return submit(cart, user)
