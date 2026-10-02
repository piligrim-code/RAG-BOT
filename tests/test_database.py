import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine

import db_client
import rabbitmq
from catalog_filters import FilterValidationError, PRICE, SKU, CATALOG_LIMIT


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")
    def engine_factory(url, **kwargs):
        assert url.drivername == "postgresql+psycopg2"
        assert kwargs["connect_args"]["connect_timeout"] == 5
        assert kwargs["connect_args"]["options"] == (
            "-c statement_timeout=5000 -c lock_timeout=2000 "
            "-c idle_in_transaction_session_timeout=10000")
        assert kwargs["pool_pre_ping"] is True and kwargs["pool_timeout"] == 2
        assert kwargs["pool_size"] == 2 and kwargs["max_overflow"] == 0
        return engine
    monkeypatch.setattr(db_client, "create_engine", engine_factory)
    client = db_client.DBClient()
    with client.Session.begin() as session:
        session.add_all([
            db_client.Catalog(art="probe-a", cat="Alpha", descr="synthetic", price=10),
            db_client.Catalog(art="probe-b", cat="Beta", descr="synthetic", price=20),
        ])
    yield client
    client.close()


def test_real_sql_catalog_and_case_insensitive_filter(database):
    assert len(database.extract_catalog()) == 2
    assert len(database.extract_catalog({"\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f": "ALPHA"})) == 1
    assert len(database.extract_catalog({"\u0426\u0435\u043d\u0430": {"<": 15}})) == 1


@pytest.mark.parametrize("sku_first", [True, False])
@pytest.mark.parametrize("matching", [True, False])
def test_sku_does_not_override_other_filters_or_depend_on_key_order(database, sku_first, matching):
    sku = "\u0410\u0440\u0442\u0438\u043a\u0443\u043b"
    category = "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f"
    price = "\u0426\u0435\u043d\u0430"
    entries = [(sku, "probe-a"), (category, "Alpha" if matching else "Beta"), (price, {"<": 15})]
    filters = dict(entries if sku_first else reversed(entries))
    rows = database.extract_catalog(filters)
    assert [row[sku] for row in rows] == (["probe-a"] if matching else [])


@pytest.mark.parametrize("sku_first", [True, False])
def test_sku_does_not_bypass_price_filter(database, sku_first):
    entries = [("\u0410\u0440\u0442\u0438\u043a\u0443\u043b", "probe-a"), ("\u0426\u0435\u043d\u0430", {">": 15})]
    assert database.extract_catalog(dict(entries if sku_first else reversed(entries))) == []


def test_bad_filter_does_not_prevent_next_request(database):
    with pytest.raises(FilterValidationError):
        database.extract_catalog({"\u0426\u0435\u043d\u0430": "invalid"})
    assert len(database.extract_catalog()) == 2


@pytest.mark.parametrize("filters,expected", [({"sku": "PROBE-A"}, ["probe-a"]),
    ({"price": {"=": 10}}, ["probe-a"]), ({"price": {"<=": 10}}, ["probe-a"]),
    ({"price": {">=": 20}}, ["probe-b"])])
def test_normalized_and_inclusive_filters(database, filters, expected):
    assert [item[SKU] for item in database.extract_catalog(filters)] == expected


def test_invalid_filters_are_rejected_before_opening_session(database, monkeypatch):
    session = Mock()
    monkeypatch.setattr(database, "Session", session)
    with pytest.raises(FilterValidationError):
        database.extract_catalog({"brand": "unsupported"})
    session.assert_not_called()


def test_lookup_is_bounded_but_explicit_export_is_complete(database):
    with database.Session.begin() as session:
        session.add_all([db_client.Catalog(art=f"extra-{index:03d}", cat="Synthetic",
                        descr="Synthetic extra", price=1) for index in range(105)])
    result = database.extract_catalog()
    assert len(result) == CATALOG_LIMIT
    assert [item[SKU] for item in result] == sorted(item[SKU] for item in result)
    assert len(database.extract_catalog(limit=None)) == 107


@pytest.mark.parametrize("limit", [0, True, -1, 1001, 2.5])
def test_bad_result_limit(database, limit):
    with pytest.raises(ValueError):
        database.extract_catalog(limit=limit)


def test_failed_schema_initialization_disposes_engine(monkeypatch):
    engine = Mock()
    monkeypatch.setattr(db_client, "create_engine", lambda url, **kwargs: engine)
    monkeypatch.setattr(db_client.Base.metadata, "create_all", Mock(side_effect=RuntimeError("synthetic")))
    with pytest.raises(RuntimeError):
        db_client.DBClient()
    engine.dispose.assert_called_once_with()


@pytest.mark.parametrize("options", [
    {"statement_timeout_ms": 0}, {"statement_timeout_ms": True},
    {"statement_timeout_ms": 60001}, {"lock_timeout_ms": -1},
    {"lock_timeout_ms": 1.5}, {"lock_timeout_ms": 6000},
])
def test_invalid_deadlines_do_not_create_engine(monkeypatch, options):
    factory = Mock()
    monkeypatch.setattr(db_client, "create_engine", factory)
    with pytest.raises(ValueError):
        db_client.DBClient(**options)
    factory.assert_not_called()


def test_close_disposes_even_if_session_close_fails():
    client = db_client.DBClient.__new__(db_client.DBClient)
    client.session = Mock()
    client.engine = Mock()
    client.session.close.side_effect = RuntimeError("synthetic")
    with pytest.raises(RuntimeError):
        client.close()
    client.engine.dispose.assert_called_once_with()


@pytest.mark.parametrize("error", [RuntimeError("synthetic connect failure"), asyncio.CancelledError()])
def test_worker_main_closes_database_on_failure_or_cancellation(monkeypatch, error):
    import dotenv

    database = Mock()
    monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost/")
    monkeypatch.setattr(db_client, "DBClient", lambda: database)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: None)
    monkeypatch.setattr(rabbitmq, "serve_catalog", AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        asyncio.run(rabbitmq.main())
    database.close.assert_called_once_with()
