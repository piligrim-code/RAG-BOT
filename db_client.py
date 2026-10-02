"""Explicit PostgreSQL catalog connection and per-request transaction ownership."""
import argparse
import os
from sqlalchemy import create_engine, func, select, Column, Integer, String
from sqlalchemy.engine import URL
from sqlalchemy.orm import declarative_base, sessionmaker
from catalog_filters import normalize_filters, CATALOG_LIMIT


_UNSET = object()


class DatabaseStartupError(RuntimeError):
    pass


def _connection_url(**values):
    values = {key: os.environ.get(key) if value is _UNSET else value for key, value in values.items()}
    for key in ("username", "password", "host", "database"):
        value = values[key]
        if (not isinstance(value, str) or not value or any(char in value for char in "\x00\r\n")
                or (key != "password" and not value.strip())):
            raise ValueError(f"Missing or invalid database configuration: {key}")
    port = values["port"]
    if isinstance(port, str) and port.isascii() and port.isdecimal() and len(port) <= 5:
        port = int(port)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Database port must be an integer in [1, 65535]")
    values["port"] = port
    return URL.create(drivername="postgresql+psycopg2", **values)

Base = declarative_base()

class Catalog(Base):
    __tablename__ = 'products_trio'
    art = Column(String(100), primary_key = True)
    cat = Column(String(100))
    descr = Column(String(100))
    price = Column(Integer())

class DBClient:
    def __init__(self, username=_UNSET, password=_UNSET, host=_UNSET, port=_UNSET,
                       database=_UNSET, *, create_schema=False,
                       statement_timeout_ms=5000, lock_timeout_ms=2000):
        if type(create_schema) is not bool:
            raise ValueError("create_schema must be an explicit boolean")
        for value in (statement_timeout_ms, lock_timeout_ms):
            if type(value) is not int or not 1 <= value <= 60000:
                raise ValueError("Database deadlines must be integers between 1 and 60000 ms")
        if lock_timeout_ms > statement_timeout_ms:
            raise ValueError("Lock deadline must not exceed the statement deadline")
        url = _connection_url(username=username, password=password, host=host, port=port, database=database)
        self.engine = create_engine(
            url, pool_pre_ping=True, pool_size=2, max_overflow=0, pool_timeout=2, hide_parameters=True,
            connect_args={"connect_timeout": 5, "options":
                f"-c statement_timeout={statement_timeout_ms} "
                f"-c lock_timeout={lock_timeout_ms} "
                "-c idle_in_transaction_session_timeout=10000"})
        self._closed = False
        try:
            if create_schema:
                Base.metadata.create_all(self.engine)
            # Probe required columns without returning catalog rows or running DDL.
            with self.engine.connect() as connection:
                connection.execute(select(Catalog).limit(0))
            self.Session = sessionmaker(bind=self.engine)
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise DatabaseStartupError("Database startup failed; check connection and catalog schema") from None
            raise

    def close(self):
        if not self._closed:
            self._closed = True
            self.engine.dispose()

    def extract_catalog(self, parameters=None, *, limit=CATALOG_LIMIT):
        if self._closed:
            raise RuntimeError("Database client is closed")
        parameters = normalize_filters({} if parameters is None else parameters)
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
            raise ValueError("limit must be an integer from 1 to 1000, or None for an explicit export")
        # Each RPC owns its transaction; a failed query cannot poison the next.
        with self.Session() as session:
            f_catalog = session.query(Catalog)
            if parameters:
                for param_name, param_value in parameters.items():
                    if param_name == "Артикул":
                        f_catalog = f_catalog.filter(func.lower(Catalog.art) == func.lower(param_value))
                    elif param_name == "Категория":
                        f_catalog = f_catalog.filter(func.lower(Catalog.cat) == func.lower(param_value))
                    elif param_name == "Описание":
                        f_catalog = f_catalog.filter(func.lower(Catalog.descr) == func.lower(param_value))
                    elif param_name == "Цена":
                        for sign, value in param_value.items():
                            if sign == "<":
                                f_catalog = f_catalog.filter(Catalog.price < value)
                            elif sign == ">":
                                f_catalog = f_catalog.filter(Catalog.price > value)
                            elif sign == "<=":
                                f_catalog = f_catalog.filter(Catalog.price <= value)
                            elif sign == ">=":
                                f_catalog = f_catalog.filter(Catalog.price >= value)
                            elif sign == "=":
                                f_catalog = f_catalog.filter(Catalog.price == value)
            return [{
                "Артикул": item.art,
                "Категория": item.cat,
                "Описание": item.descr,
                "Цена": item.price,
            } for item in f_catalog.order_by(Catalog.art).limit(limit).all()]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Initialize only the explicitly configured catalog schema")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-schema", help="Create missing catalog tables; never migrate or import rows")
    parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv()
    client = DBClient(create_schema=True)
    client.close()
    print("Catalog schema checked/initialized. No catalog rows imported.")


if __name__ == "__main__":
    main()
