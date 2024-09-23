from tqdm.autonotebook import tqdm
import os
import requests
from pydantic import BaseModel
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.vectorstores import Chroma
from langchain.embeddings import OpenAIEmbeddings
from langchain.embeddings.huggingface import HuggingFaceEmbeddings
from bot.db_client import DBClient
if os.path.exists("/data/catalog_vectordb"):
    vectordb = Chroma(
        persist_directory="/data/catalog_vectordb",
        embedding_function=OpenAIEmbeddings()
    )
else:
    db_client = DBClient()
    catalog = db_client.extract_catalog()
    texts = ["\n".join([f"{param_name}: {param_value}"
            for param_name, param_value in cat.items()]) for cat in catalog]

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=10000, chunk_overlap=50)
    documents = text_splitter.create_documents(texts)

    vectordb = Chroma.from_documents(
        documents,
        embedding=HuggingFaceEmbeddings(model_name="all-mpnet-base-v2"),
        persist_directory="/data/catalog_vectordb",
    )

vectordb.persist()
retriever = vectordb.as_retriever(search_kwargs={"k": 5})