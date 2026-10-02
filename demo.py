"""Offline contract demo: no model, Telegram, broker or database connection."""
import asyncio
import json

from catalog_service import catalog_reply, dispatch_catalog_request
from catalog_filters import SKU, DESCRIPTION, PRICE
from conversation import run_catalog_turn


class SyntheticCatalog:
    def extract_catalog(self, filters):
        products = [
            {SKU: "sample-001", DESCRIPTION: "Synthetic cutting disc", PRICE: 100},
            {SKU: "sample-002", DESCRIPTION: "Synthetic drill", PRICE: 200},
        ]
        return [item for item in products if all(item.get(k) == v for k, v in filters.items())]


class LocalRpc:
    async def call(self, message):
        return dispatch_catalog_request(message, SyntheticCatalog())


async def main():
    rpc = LocalRpc()
    async def extract(query, previous):
        return {"sku": "sample-001"} if query == "select" else {"sku": None}
    first = await run_catalog_turn("select", {}, extract=extract, rpc_client=rpc)
    cleared = await run_catalog_turn("clear", first.filters, extract=extract, rpc_client=rpc)
    print(json.dumps({"matching": await catalog_reply({"sku": "sample-001"}, rpc),
                      "empty": await catalog_reply({"sku": "missing"}, rpc),
                      "dialogue": {"selected": first.reply, "cleared_filters": cleared.filters,
                                   "needs_clarification": cleared.needs_clarification}}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
