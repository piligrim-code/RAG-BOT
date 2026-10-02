import json
import os
import uuid
import sqlalchemy.dialects.postgresql as postgresql
from sqlalchemy import create_engine, func, Column, Integer, Float, String, DateTime, Text, ForeignKey
from sqlalchemy.engine import URL
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from catalog_filters import normalize_filters, CATALOG_LIMIT


username = os.getenv("username")
password = os.getenv("password")
host = os.getenv("host")
port = os.getenv("port")
database = os.getenv("database")

Base = declarative_base()

class Catalog(Base):
    __tablename__ = 'products_trio'
    art = Column(String(100), primary_key = True)
    cat = Column(String(100))
    descr = Column(String(100))
    price = Column(Integer())

class DBClient:
    def __init__(self, username=username, password=password, host=host, port=port,
                       database=database):
        url = URL.create(
            drivername="postgresql+psycopg2",
            username=username,
            password=password,
            host=host,
            port=port,
            database=database
        )
        self.engine = create_engine(url, connect_args={"connect_timeout": 10})
        try:
            Base.metadata.create_all(self.engine)
            self.Session = sessionmaker(bind=self.engine)
            self.session = self.Session()
        except BaseException:
            self.engine.dispose()
            raise

    def close(self):
        try:
            self.session.close()
        finally:
            self.engine.dispose()

    def extract_catalog(self, parameters=None, *, limit=CATALOG_LIMIT):
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

    def new_dialog(self, user_id):
        dialog = Dialog(user_id=user_id)
        self.session.add(dialog)
        self.session.commit()
        return dialog.id.hex

    def add_message(self, dialog_id, role, message):
        dialog_uuid = uuid.UUID(dialog_id)
        message = Message(dialog_id=dialog_uuid, role=role, text=message)
        self.session.add(message)
        self.session.commit()
        #r
