from libs.common.db import connect
from services.checkout.pricing import price_cart
from services.inventory.stock import reserve
from services.payments.charge import charge_order


def validate_cart(cart: list[dict]) -> bool:
    if not cart:
        return False
    for line in cart:
        if not line.get("sku"):
            return False
        if int(line.get("qty", 0)) <= 0:
            return False
    return True


def submit(cart: list[dict], user: str) -> dict:
    if not validate_cart(cart):
        return {"ok": False, "error": "invalid cart"}
    priced = price_cart(cart)
    if priced["total"] <= 0:
        return {"ok": False, "error": "nothing to charge"}
    conn = connect()
    held = []
    for line in cart:
        ok = reserve(line["sku"], int(line["qty"]))
        if not ok:
            return {"ok": False, "error": "out of stock", "held": held}
        held.append(line["sku"])
    receipt = charge_order(conn, user, priced["total"])
    if not receipt.get("ok"):
        return {"ok": False, "error": "payment failed", "held": held}
    return {"ok": True, "total": priced["total"], "receipt": receipt}
