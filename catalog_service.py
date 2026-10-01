"""Shared catalog RPC contract and bounded user-facing output."""
from db_calls import extract_gk


class CatalogRequestError(ValueError):
    pass


def dispatch_catalog_request(request, database):
    if not isinstance(request, dict) or set(request) != {"extract_catalog"}:
        raise CatalogRequestError("Unsupported catalog operation")
    filters = request["extract_catalog"]
    if not isinstance(filters, dict):
        raise CatalogRequestError("Catalog filters must be an object")
    return database.extract_catalog(filters)


async def catalog_reply(filters, rpc_client):
    context, first = await extract_gk(filters, rpc_client)
    if first is None:
        return "No matching products. Try changing the filters."
    # Telegram's text limit is 4096 characters; leave room for the prefix.
    return "Matching products:\n" + context[:3500]
