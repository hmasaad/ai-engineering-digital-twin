from services.inventory.stock import reserve


def test_reserve_rejects_empty_qty():
    assert reserve("sku-1", 0) is False
