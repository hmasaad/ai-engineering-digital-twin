from services.checkout.pricing import price_cart


def test_price_cart_sums_lines():
    assert price_cart([{"price": 10, "qty": 2}])["total"] == 20
