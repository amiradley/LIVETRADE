from live_trade.mt5_client import ReadOnlyMt5Client


def test_client_exposes_no_order_mutation_methods():
    public_methods = {name for name in dir(ReadOnlyMt5Client) if not name.startswith("_")}
    assert public_methods.isdisjoint({"order_send", "order_modify", "order_delete", "close_position"})
