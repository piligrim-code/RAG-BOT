"""Test the importable catalog adapter, without a Telegram connection."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from catalog_filters import FilterValidationError, SKU
from conversation import CatalogTurn, CatalogTurnError
from main import catalog_reply


def handler_parts():
    runner = AsyncMock(return_value=CatalogTurn({SKU: "synthetic-a"}, "Synthetic reply"))
    state = SimpleNamespace(get_data=AsyncMock(return_value={"catalog_params": {}}),
                            update_data=AsyncMock())
    message = SimpleNamespace(text="Synthetic query", answer=AsyncMock())
    async def handler():
        with patch("main.run_catalog_turn", runner):
            await catalog_reply(message, state, Mock(), Mock())
    return handler, runner, message, state


def test_handler_updates_state_only_after_reply_returns():
    handler, runner, message, state = handler_parts()
    async def delivered(reply):
        assert reply == "Synthetic reply"
        state.update_data.assert_not_awaited()
    message.answer.side_effect = delivered
    asyncio.run(handler())
    runner.assert_awaited_once()
    state.update_data.assert_awaited_once_with(catalog_params={SKU: "synthetic-a"})


def test_delivery_failure_does_not_advance_state():
    handler, _, message, state = handler_parts()
    message.answer.side_effect = RuntimeError("Synthetic delivery failure")
    with pytest.raises(RuntimeError, match="delivery"):
        asyncio.run(handler())
    state.update_data.assert_not_awaited()


@pytest.mark.parametrize("error", [FilterValidationError("Synthetic invalid filter"),
                                 CatalogTurnError("catalog")])
def test_failed_turn_does_not_advance_state(error):
    handler, runner, message, state = handler_parts()
    runner.side_effect = error
    asyncio.run(handler())
    state.update_data.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert "Synthetic invalid filter" not in message.answer.call_args.args[0]
