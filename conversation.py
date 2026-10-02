"""One stateless catalog turn; caller commits returned state after delivery."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import math

from catalog_filters import FilterValidationError, merge_filter_patch, normalize_filters, validate_text
from catalog_service import catalog_reply


@dataclass(frozen=True)
class CatalogTurn:
    filters: dict
    reply: str
    needs_clarification: bool = False


class CatalogTurnError(RuntimeError):
    def __init__(self, stage):
        self.stage = stage
        super().__init__(f"Catalog turn failed during {stage}; not retried")


async def run_catalog_turn(query, previous_filters, *, extract, rpc_client, timeout=45):
    validate_text(query, 4000)
    previous = normalize_filters(previous_filters)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("timeout must be a finite number in (0, 120]")
    if not callable(extract) or not callable(getattr(rpc_client, "call", None)):
        raise ValueError("Explicit extractor and RPC client are required")
    stage = "extraction"
    try:
        async with asyncio.timeout(timeout):
            patch = await extract(query, deepcopy(previous))
            filters = merge_filter_patch(previous, patch)
            if not filters:
                return CatalogTurn({}, "Please specify a SKU, category, description or price range.", True)
            stage = "catalog"
            reply = await catalog_reply(deepcopy(filters), rpc_client)
        return CatalogTurn(filters, reply)
    except FilterValidationError:
        raise
    except Exception:
        raise CatalogTurnError(stage) from None
