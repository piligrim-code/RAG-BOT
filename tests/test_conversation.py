import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from catalog_filters import CATEGORY, PRICE, SKU, FilterValidationError
from conversation import CatalogTurnError, run_catalog_turn
import llm


def test_followup_keeps_prior_constraints_without_mutating_inputs():
    previous = {"sku": "synthetic-a", "price": {"<=": 20}}
    snapshot = deepcopy(previous)
    extract = AsyncMock(return_value={"category": "Alpha"})
    rpc = SimpleNamespace(call=AsyncMock(return_value=[{SKU: "synthetic-a", PRICE: 10}]))
    turn = asyncio.run(run_catalog_turn("Synthetic follow-up", previous, extract=extract, rpc_client=rpc))
    assert turn.filters == {SKU: "synthetic-a", PRICE: {"<=": 20}, CATEGORY: "Alpha"}
    assert "synthetic-a" in turn.reply and previous == snapshot
    rpc.call.assert_awaited_once_with({"extract_catalog": turn.filters})


def test_empty_result_is_successful_turn_with_new_filters():
    rpc = SimpleNamespace(call=AsyncMock(return_value=[]))
    turn = asyncio.run(run_catalog_turn("Synthetic absent product", {},
        extract=AsyncMock(return_value={"sku": "absent"}), rpc_client=rpc))
    assert turn.filters == {SKU: "absent"} and "No matching products" in turn.reply


@pytest.mark.parametrize("previous,patch", [({}, {}), ({SKU: "synthetic-a"}, {"sku": None})])
def test_no_constraints_requests_clarification_instead_of_all_products(previous, patch):
    rpc = SimpleNamespace(call=AsyncMock())
    turn = asyncio.run(run_catalog_turn("Synthetic", previous,
        extract=AsyncMock(return_value=patch), rpc_client=rpc))
    assert turn.needs_clarification and turn.filters == {} and "Please specify" in turn.reply
    rpc.call.assert_not_awaited()


def test_invalid_extraction_never_queries_or_changes_state():
    previous = {CATEGORY: "Alpha"}
    rpc = SimpleNamespace(call=AsyncMock())
    with pytest.raises(FilterValidationError):
        asyncio.run(run_catalog_turn("Synthetic", previous,
            extract=AsyncMock(return_value={"brand": "unsupported"}), rpc_client=rpc))
    rpc.call.assert_not_awaited()
    assert previous == {CATEGORY: "Alpha"}


@pytest.mark.parametrize("stage", ["extraction", "catalog"])
def test_failure_is_sanitized_without_mutating_state(stage):
    previous = {PRICE: {"<": 50}}
    snapshot = deepcopy(previous)
    extract = AsyncMock(return_value={"sku": "synthetic-a"})
    rpc = SimpleNamespace(call=AsyncMock(return_value=[]))
    target = extract if stage == "extraction" else rpc.call
    target.side_effect = RuntimeError("synthetic-private-detail")
    with pytest.raises(CatalogTurnError) as caught:
        asyncio.run(run_catalog_turn("Synthetic", previous, extract=extract, rpc_client=rpc))
    assert caught.value.stage == stage and previous == snapshot
    assert "synthetic-private-detail" not in str(caught.value)


def test_extractor_cannot_mutate_previous_filters():
    previous = {PRICE: {"<": 50}}
    async def extract(query, filters):
        filters[PRICE]["<"] = 100
        return {}
    rpc = SimpleNamespace(call=AsyncMock(return_value=[]))
    turn = asyncio.run(run_catalog_turn("Synthetic", previous, extract=extract, rpc_client=rpc))
    assert turn.filters == previous == {PRICE: {"<": 50}}


def test_total_timeout_cancels_extractor():
    stopped = []
    async def extract(query, filters):
        try:
            await asyncio.sleep(10)
        finally:
            stopped.append(True)
    rpc = SimpleNamespace(call=AsyncMock())
    with pytest.raises(CatalogTurnError, match="extraction"):
        asyncio.run(run_catalog_turn("Synthetic", {}, extract=extract, rpc_client=rpc, timeout=0.02))
    assert stopped == [True]
    rpc.call.assert_not_awaited()


def test_caller_cancellation_propagates():
    async def scenario():
        started = asyncio.Event()
        async def extract(query, filters):
            started.set()
            await asyncio.sleep(10)
        rpc = SimpleNamespace(call=AsyncMock())
        task = asyncio.create_task(run_catalog_turn("Synthetic", {}, extract=extract, rpc_client=rpc))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        rpc.call.assert_not_awaited()
    asyncio.run(scenario())


def test_two_sessions_do_not_share_state():
    async def scenario():
        rpc = SimpleNamespace(call=AsyncMock(return_value=[]))
        first, second = await asyncio.gather(
            run_catalog_turn("A", {SKU: "synthetic-a"}, extract=AsyncMock(return_value={}), rpc_client=rpc),
            run_catalog_turn("B", {SKU: "synthetic-b"}, extract=AsyncMock(return_value={}), rpc_client=rpc),
        )
        assert first.filters == {SKU: "synthetic-a"} and second.filters == {SKU: "synthetic-b"}
    asyncio.run(scenario())


def test_legacy_slot_fill_returns_copy(monkeypatch):
    state = {"catalog_params": {PRICE: {"<": 50}}}
    snapshot = deepcopy(state)
    monkeypatch.setattr(llm, "extract_filter_patch", AsyncMock(return_value={"sku": "synthetic-a"}))
    result = asyncio.run(llm.slot_fill(state, SimpleNamespace(text="Synthetic")))
    assert result == {PRICE: {"<": 50}, SKU: "synthetic-a"} and state == snapshot


@pytest.mark.parametrize("query,timeout", [("", 45), ("x" * 4001, 45), ("text", 0),
                                        ("text", True), ("text", float("inf"))])
def test_invalid_turn_never_calls_services(query, timeout):
    extract = AsyncMock()
    rpc = SimpleNamespace(call=AsyncMock())
    with pytest.raises(ValueError):
        asyncio.run(run_catalog_turn(query, {}, extract=extract, rpc_client=rpc, timeout=timeout))
    extract.assert_not_awaited()
    rpc.call.assert_not_awaited()
