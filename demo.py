"""Offline contract demo: no model, Telegram, broker or database connection."""
import asyncio
import json

from catalog_service import catalog_reply, dispatch_catalog_request


class SyntheticCatalog:
    def extract_catalog(self, filters):
        products = [
            {"sku": "sample-001", "description": "Synthetic cutting disc", "price": 100},
            {"sku": "sample-002", "description": "Synthetic drill", "price": 200},
        ]
        return [item for item in products if all(item.get(k) == v for k, v in filters.items())]


class LocalRpc:
    async def call(self, message):
        return dispatch_catalog_request(message, SyntheticCatalog())


async def main():
    rpc = LocalRpc()
    print(json.dumps({"matching": await catalog_reply({"sku": "sample-001"}, rpc),
                      "empty": await catalog_reply({"sku": "missing"}, rpc)}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
