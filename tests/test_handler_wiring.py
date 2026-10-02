"""Execute only the trusted text-handler AST, without importing the legacy bot."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from catalog_filters import FilterValidationError, SKU
from conversation import CatalogTurn, CatalogTurnError


def handler_parts():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
    handlers = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == "run_catalog_turn" for call in ast.walk(node))]
    assert len(handlers) == 1
    handler = handlers[0]
    handler.decorator_list = []
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, handler], type_ignores=[]))
    runner = AsyncMock(return_value=CatalogTurn({SKU: "synthetic-a"}, "Synthetic reply"))
    namespace = {"run_catalog_turn": runner, "extract_filter_patch": Mock(),
                 "rpc_client": Mock(), "FilterValidationError": FilterValidationError, "logging": Mock()}
    exec(compile(module, "<isolated catalog handler>", "exec"), namespace)
    state = SimpleNamespace(get_data=AsyncMock(return_value={"catalog_params": {}}),
                            update_data=AsyncMock())
    message = SimpleNamespace(text="Synthetic query", from_user=SimpleNamespace(id=123),
                              answer=AsyncMock())
    return namespace[handler.name], runner, message, state


def test_handler_updates_state_only_after_reply_returns():
    handler, runner, message, state = handler_parts()
    async def delivered(reply):
        assert reply == "Synthetic reply"
        state.update_data.assert_not_awaited()
    message.answer.side_effect = delivered
    asyncio.run(handler(message, state))
    runner.assert_awaited_once()
    state.update_data.assert_awaited_once_with(catalog_params={SKU: "synthetic-a"})


def test_delivery_failure_does_not_advance_state():
    handler, _, message, state = handler_parts()
    message.answer.side_effect = RuntimeError("Synthetic delivery failure")
    with pytest.raises(RuntimeError, match="delivery"):
        asyncio.run(handler(message, state))
    state.update_data.assert_not_awaited()


@pytest.mark.parametrize("error", [FilterValidationError("Synthetic invalid filter"),
                                 CatalogTurnError("catalog")])
def test_failed_turn_does_not_advance_state(error):
    handler, runner, message, state = handler_parts()
    runner.side_effect = error
    asyncio.run(handler(message, state))
    state.update_data.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert "Synthetic invalid filter" not in message.answer.call_args.args[0]
