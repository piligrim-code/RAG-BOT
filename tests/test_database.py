import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, event, inspect

import db_client
import rabbitmq
from catalog_filters import FilterValidationError, PRICE, SKU, CATALOG_LIMIT

CONFIG = dict(username="synthetic-user", password="synthetic-test-only", host="127.0.0.1",
              port=5432, database="synthetic-catalog")


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
    client = db_client.DBClient(**CONFIG, create_schema=True)
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
        db_client.DBClient(**CONFIG, create_schema=True)
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
        db_client.DBClient(**CONFIG, **options)
    factory.assert_not_called()


def test_close_is_final_and_does_not_keep_a_shared_session():
    client = db_client.DBClient.__new__(db_client.DBClient)
    client._closed = False
    client.engine = Mock()
    client.close()
    client.close()
    client.engine.dispose.assert_called_once_with()
    with pytest.raises(RuntimeError, match="closed"):
        client.extract_catalog()
    assert not hasattr(client, "session") and not hasattr(client, "new_dialog") and not hasattr(client, "add_message")


@pytest.mark.parametrize("field", list(CONFIG))
def test_missing_configuration_fails_before_engine_creation(monkeypatch, field):
    for key, value in CONFIG.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv(field)
    factory = Mock()
    monkeypatch.setattr(db_client, "create_engine", factory)
    with pytest.raises(ValueError):
        db_client.DBClient()
    factory.assert_not_called()


@pytest.mark.parametrize("field,value", [("username", ""), ("database", None), ("password", None),
    ("password", "synthetic\nprivate"), ("host", " "), ("port", True), ("port", 0),
    ("port", 65536), ("port", "not-a-port"), ("port", 5432.0), ("port", "+5432")])
def test_explicit_invalid_config_does_not_fall_back_to_environment(monkeypatch, field, value):
    monkeypatch.setenv(field, str(CONFIG[field]))
    factory = Mock()
    monkeypatch.setattr(db_client, "create_engine", factory)
    with pytest.raises(ValueError) as caught:
        db_client.DBClient(**dict(CONFIG, **{field: value}))
    assert "synthetic" not in str(caught.value) and "not-a-port" not in str(caught.value)
    factory.assert_not_called()


def test_config_reads_current_environment_not_import_snapshot(monkeypatch):
    for key, value in CONFIG.items():
        monkeypatch.setenv(key, str(value))
    engine = create_engine("sqlite://")
    db_client.Base.metadata.create_all(engine)
    observed = []
    def factory(url, **kwargs):
        observed.append(url)
        assert kwargs["hide_parameters"] is True
        return engine
    monkeypatch.setattr(db_client, "create_engine", factory)
    client = db_client.DBClient()
    assert observed[0].database == CONFIG["database"] and observed[0].port == 5432
    client.close()
    monkeypatch.setenv("database", "synthetic-second")
    # A fresh in-memory schema is necessary after dispose, independently of config.
    db_client.Base.metadata.create_all(engine)
    client = db_client.DBClient()
    assert observed[1].database == "synthetic-second"
    client.close()


def test_normal_startup_has_only_zero_row_select_and_no_ddl(monkeypatch):
    engine = create_engine("sqlite://")
    db_client.Base.metadata.create_all(engine)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, many:
                 statements.append((statement, parameters)))
    monkeypatch.setattr(db_client, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(db_client.Base.metadata, "create_all", Mock(side_effect=AssertionError("Unexpected DDL")))
    client = db_client.DBClient(**CONFIG)
    try:
        assert len(statements) == 1 and statements[0][0].startswith("SELECT")
        assert "LIMIT" in statements[0][0] and statements[0][1][0] == 0
    finally:
        client.close()


def test_missing_schema_is_not_silently_created(monkeypatch):
    engine = create_engine("sqlite://")
    disposed = Mock(wraps=engine.dispose)
    monkeypatch.setattr(engine, "dispose", disposed)
    monkeypatch.setattr(db_client, "create_engine", lambda *args, **kwargs: engine)
    with pytest.raises(db_client.DatabaseStartupError):
        db_client.DBClient(**CONFIG)
    disposed.assert_called_once()
    assert inspect(engine).get_table_names() == []
    engine.dispose()


@pytest.mark.parametrize("flag", [1, "true", None])
def test_schema_creation_requires_boolean(monkeypatch, flag):
    factory = Mock()
    monkeypatch.setattr(db_client, "create_engine", factory)
    with pytest.raises(ValueError, match="boolean"):
        db_client.DBClient(**CONFIG, create_schema=flag)
    factory.assert_not_called()


def test_schema_cli_requires_explicit_command(monkeypatch, capsys):
    import dotenv
    constructor = Mock()
    monkeypatch.setattr(db_client, "DBClient", constructor)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: None)
    with pytest.raises(SystemExit):
        db_client.main([])
    constructor.assert_not_called()
    db_client.main(["init-schema"])
    constructor.assert_called_once_with(create_schema=True)
    constructor.return_value.close.assert_called_once()
    assert "No catalog rows imported" in capsys.readouterr().out


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
