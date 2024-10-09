import json
import os
import uuid
import sqlalchemy.dialects.postgresql as postgresql
from sqlalchemy import create_engine, func, Column, Integer, Float, String, DateTime, Text, ForeignKey
from sqlalchemy.engine import URL
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime


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
            drivername="postgresql",
            username=username,
            password=password,
            host=host,
            port=port,
            database=database
        )
        engine = create_engine(url)
        connection = engine.connect()
        Base.metadata.create_all(engine)

        Session = sessionmaker(bind=engine)
        self.session = Session()
    def extract_catalog(self, parameters=None):
        f_catalog = self.session.query(Catalog)
        print(f"Query: {f_catalog}")
        if parameters:
            for param_name, param_value in parameters.items():
                if param_name == "Артикул":
                    f_catalog = f_catalog.filter(func.lower(Catalog.art) == func.lower(param_value))
                    break
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
        f_catalog = f_catalog.all()
        print(f"Result: {f_catalog}")  
        f_catalog_dicts = []
        for f_cat in f_catalog:
            f_catalog_dicts.append({
                "Артикул": f_cat.art,
                "Категория": f_cat.cat,
                "Описание": f_cat.descr,
                "Цена": f_cat.price
            })
        return f_catalog_dicts

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