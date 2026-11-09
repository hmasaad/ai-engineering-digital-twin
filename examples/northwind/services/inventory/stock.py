from libs.common.db import connect

_WAREHOUSE = {"sku-1": 5}


def reserve(sku: str, qty: int) -> bool:
    conn = connect()
    available = _WAREHOUSE.get(sku, 0)
    if qty <= 0 or conn is None:
        return False
    if available < qty:
        return False
    _WAREHOUSE[sku] = available - qty
    return True


def bundle_availability(cart: list[dict]) -> bool:
    # Reach-back into checkout. The static graph contains a cycle.
    from services.checkout.checkout import validate_cart

    if not validate_cart(cart):
        return False
    for line in cart:
        if _WAREHOUSE.get(line.get("sku"), 0) < int(line.get("qty", 0)):
            return False
    return True
