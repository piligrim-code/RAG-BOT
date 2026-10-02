"""Shared catalog RPC contract and bounded user-facing output."""
from db_calls import extract_gk
from catalog_filters import FilterValidationError, normalize_filters


class CatalogRequestError(ValueError):
    pass


def dispatch_catalog_request(request, database):
    if not isinstance(request, dict) or set(request) != {"extract_catalog"}:
        raise CatalogRequestError("Unsupported catalog operation")
    filters = request["extract_catalog"]
    if not isinstance(filters, dict):
        raise CatalogRequestError("Catalog filters must be an object")
    try:
        filters = normalize_filters(filters)
    except FilterValidationError:
        raise CatalogRequestError("Invalid catalog filters") from None
    return database.extract_catalog(filters)


async def catalog_reply(filters, rpc_client):
    context, first = await extract_gk(filters, rpc_client)
    if first is None:
        return "No matching products. Try changing the filters."
    # Also bound UTF-16 units so non-BMP text cannot double the message size.
    message = "Matching products (showing up to 3):\n" + context
    return message.encode("utf-16-le")[:7000].decode("utf-16-le", errors="ignore")
