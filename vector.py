import os
import requests
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
from llama_cpp import Llama
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.vectorstores import Chroma
from langchain.embeddings import HuggingFaceEmbeddings
from db_client import DBClient

model_name = os.getenv("MODEL_NAME")
model_path = "./data/llama-2-13b.Q2_K.gguf"
llama = Llama(
    model_path=model_path, 
    n_ctx=8192,
    n_threads=8,
    n_gpu_layers=35,
    chat_format="llama-2"
    )

if os.path.exists("/data/catalog_vectordb"):
    vectordb = Chroma(
        persist_directory="/data/catalog_vectordb",
        embedding_function=HuggingFaceEmbeddings()
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

app = FastAPI()

class Prompt(BaseModel):
    content: str | list


class Query(BaseModel):
    content: str


@app.post("/generate")
def execute_prompt(prompt: Prompt):
    content = prompt.content
    if isinstance(content, str):
        messages = [{"role": "system", "content": content}]
    else:
        messages = content
        response = llama.create_chat_completion(messages=messages)
        res_content = response["choices"][0]["message"]["content"]
    return {"res_content": res_content}


@app.post("/retrieve")
def execute_prompt(query: Query):
    content = query.content
    res = retriever.get_relevant_documents(query=content)
    retr_elements = []
    for element in res:
        if element.page_content not in retr_elements:
            retr_elements.append(element.page_content)
    return {"response": retr_elements}


if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=8015)
#r