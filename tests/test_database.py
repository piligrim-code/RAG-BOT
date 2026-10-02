import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine

import db_client
import rabbitmq


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")
    def engine_factory(url, **kwargs):
        assert url.drivername == "postgresql+psycopg2"
        assert kwargs["connect_args"]["connect_timeout"] == 10
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
    with pytest.raises(AttributeError):
        database.extract_catalog({"\u0426\u0435\u043d\u0430": "invalid"})
    assert len(database.extract_catalog()) == 2


def test_failed_schema_initialization_disposes_engine(monkeypatch):
    engine = Mock()
    monkeypatch.setattr(db_client, "create_engine", lambda url, **kwargs: engine)
    monkeypatch.setattr(db_client.Base.metadata, "create_all", Mock(side_effect=RuntimeError("synthetic")))
    with pytest.raises(RuntimeError):
        db_client.DBClient()
    engine.dispose.assert_called_once_with()


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
