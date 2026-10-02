"""Explicit offline model assets and a versioned, rebuild-only Chroma catalog."""
from contextlib import ExitStack
import hashlib
import json
import math
from pathlib import Path
import re

from catalog_filters import load_json_object, validate_text

COLLECTION = "rag_catalog_v1"
MANIFEST = "rag-index.json"


def embedding_fingerprint(directory):
    root = Path(directory)
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise ValueError("An ordinary local embedding model directory is required")
    files = sorted(root.rglob("*"))
    if not any(path.is_file() for path in files):
        raise ValueError("Embedding model directory is empty")
    digest = hashlib.sha256()
    for path in files:
        if path.is_symlink() or path.is_junction():
            raise ValueError("Embedding model links are not supported")
        if path.is_file():
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big") + relative)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _vectors(values, count, dimension=None):
    if hasattr(values, "tolist"):
        values = values.tolist()
    if not isinstance(values, list) or len(values) != count or not values:
        raise ValueError("Unexpected embedding batch")
    if not isinstance(values[0], list):
        raise ValueError("Invalid embedding vector")
    width = dimension or len(values[0])
    if not 1 <= width <= 8192:
        raise ValueError("Unsupported embedding dimension")
    result = []
    for row in values:
        if (not isinstance(row, list) or len(row) != width
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in row)):
            raise ValueError("Invalid embedding vector")
        norm = math.sqrt(sum(value * value for value in row))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("Invalid embedding norm")
        result.append([float(value / norm) for value in row])
    return result


def _fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError("Expected an embedding fingerprint")
    return value


def _documents(documents):
    if not isinstance(documents, list) or not 1 <= len(documents) <= 1000:
        raise ValueError("Expected 1..1000 reviewed documents")
    ids = set()
    for document in documents:
        if not isinstance(document, dict) or set(document) != {"id", "text"}:
            raise ValueError("Each document requires only id and text")
        validate_text(document["id"], 100)
        validate_text(document["text"], 4000)
        if document["id"] in ids or len(document["text"].encode("utf-8")) > 4096:
            raise ValueError("Duplicate document ID or oversized UTF-8 text")
        ids.add(document["id"])
    return documents


def _client(path, *, create=False):
    import chromadb
    from chromadb.config import Settings

    return chromadb.PersistentClient(path=str(path), settings=Settings(
        anonymized_telemetry=False, allow_reset=False,
        migrations="apply" if create else "validate"))


def build_index(path, documents, embed, fingerprint):
    """Create a NEW index only. Failures leave an incomplete directory for inspection."""
    documents = _documents(documents)
    fingerprint = _fingerprint(fingerprint)
    path = Path(path)
    if path.exists():
        raise ValueError("Index destination must not exist; rebuild into a new directory")
    vectors = _vectors(embed([item["text"] for item in documents]), len(documents))
    metadata = {"schema": 1, "embedding_fingerprint": fingerprint,
                "dimension": len(vectors[0]), "document_count": len(documents)}
    path.mkdir(parents=False, exist_ok=False)
    with _client(path, create=True) as client:
        collection = client.create_collection(COLLECTION, metadata=metadata,
            configuration={"hnsw": {"space": "cosine"}}, embedding_function=None)
        collection.add(ids=[item["id"] for item in documents],
                       documents=[item["text"] for item in documents], embeddings=vectors)
        if collection.count() != len(documents):
            raise ValueError("Incomplete index")
    # Publish the completion marker only after the client's successful close.
    with (path / MANIFEST).open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, sort_keys=True)
    return metadata


class ChromaCatalog:
    def __init__(self, path, embed, fingerprint):
        path = Path(path)
        if not path.is_dir() or path.is_symlink() or path.is_junction():
            raise ValueError("An existing reviewed index directory is required")
        marker = path / MANIFEST
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
            raise ValueError("Missing or invalid index completion marker; rebuild explicitly")
        metadata = load_json_object(marker.read_bytes(), limit=4096)
        if (set(metadata) != {"schema", "embedding_fingerprint", "dimension", "document_count"}
                or type(metadata["schema"]) is not int or metadata["schema"] != 1
                or metadata["embedding_fingerprint"] != _fingerprint(fingerprint)
                or type(metadata["dimension"]) is not int or not 1 <= metadata["dimension"] <= 8192
                or type(metadata["document_count"]) is not int or not 1 <= metadata["document_count"] <= 1000):
            raise ValueError("Index/model metadata mismatch")
        self.client = _client(path)
        try:
            self.collection = self.client.get_collection(COLLECTION, embedding_function=None)
            if self.collection.metadata != metadata or self.collection.count() != metadata["document_count"]:
                raise ValueError("Index is inconsistent with its completion marker")
            self.dimension, self.embed = metadata["dimension"], embed
        except BaseException:
            self.client.close()
            raise

    def retrieve(self, query):
        vector = _vectors(self.embed([query]), 1, self.dimension)
        result = self.collection.query(query_embeddings=vector,
            n_results=min(5, self.collection.count()), include=["documents"])
        documents, size = [], 0
        for text in result["documents"][0]:
            validate_text(text, 4000)
            if text in documents:
                continue
            # Budget JSON escaping too, not only the unescaped document bytes.
            encoded_size = len(json.dumps(text, ensure_ascii=False).encode("utf-8"))
            if size + encoded_size > 14000:
                break
            documents.append(text)
            size += encoded_size
        return {"response": documents}

    def close(self):
        self.client.close()


def local_embedder(directory):
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(str(Path(directory).resolve()), device="cpu",
                               local_files_only=True, trust_remote_code=False)
    return lambda texts: model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


class LocalBackend:
    def __init__(self, *, model_path, embedding_dir=None, index_dir=None):
        model_path = Path(model_path)
        if not model_path.is_file() or model_path.is_symlink() or model_path.suffix.lower() != ".gguf":
            raise ValueError("An existing local GGUF model file is required")
        if bool(embedding_dir) != bool(index_dir):
            raise ValueError("Set embedding directory and index directory together")
        self.resources = ExitStack()
        self.catalog = None
        try:
            if index_dir:
                fingerprint = embedding_fingerprint(embedding_dir)
                self.catalog = ChromaCatalog(index_dir, local_embedder(embedding_dir), fingerprint)
                self.resources.callback(self.catalog.close)
            from llama_cpp import Llama
            self.model = Llama(model_path=str(model_path.resolve()), n_ctx=8192,
                               n_threads=2, n_gpu_layers=0, verbose=False)
            self.resources.callback(self.model.close)
        except BaseException:
            self.resources.close()
            raise

    def invoke(self, operation, content):
        if operation == "retrieve":
            if self.catalog is None:
                raise ValueError("Retrieval is not configured")
            return self.catalog.retrieve(content)
        if operation != "generate":
            raise ValueError("Unsupported operation")
        messages = [{"role": "user", "content": content}] if isinstance(content, str) else content
        response = self.model.create_chat_completion(messages=messages, max_tokens=512, temperature=0)
        text = response["choices"][0]["message"]["content"]
        validate_text(text, 12000)
        return {"res_content": text}

    def close(self):
        self.resources.close()
