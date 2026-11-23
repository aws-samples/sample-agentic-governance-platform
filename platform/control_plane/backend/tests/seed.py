"""``seed`` plants a row through the store's table, bypassing the store's conditions."""

from core.state_store import StateStore


def seed(store: StateStore, item: dict) -> None:
    store._table.put_item(Item=item)
