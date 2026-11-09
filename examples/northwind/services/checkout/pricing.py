def price_cart(lines: list[dict]) -> dict:
    total = 0.0
    for line in lines:
        qty = int(line.get("qty", 1))
        price = float(line.get("price", 0))
        if qty <= 0 or price < 0:
            raise ValueError("bad line")
        total += price * qty
    return {"total": round(total, 2)}
