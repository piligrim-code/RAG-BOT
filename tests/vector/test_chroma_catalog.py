"""Opt-in actual Chroma checks with fixed synthetic vectors, no model download."""
import json
import os
from functools import partial

import pytest

from vector_backend import ChromaCatalog, MANIFEST, build_index

pytestmark = pytest.mark.skipif(os.environ.get("RAG_VECTOR_TESTS") != "1", reason="requires optional Chroma dependency")
FINGERPRINT = "a" * 64
DOCUMENTS = [{"id": "synthetic-a", "text": "Synthetic alpha product"},
             {"id": "synthetic-b", "text": "Synthetic beta product"}]


def embed(texts):
    return [[1.0, 0.0] if "alpha" in text else [0.0, 1.0] for text in texts]


class ChromaBackend:
    def __init__(self, path):
        self.catalog = ChromaCatalog(path, embed, FINGERPRINT)

    def invoke(self, operation, content):
        assert operation == "retrieve"
        return self.catalog.retrieve(content)

    def close(self):
        self.catalog.close()


def test_real_http_and_owned_process_retrieve_from_actual_chroma(tmp_path):
    from tests.http_service import running_service
    from vector import create_app
    path = tmp_path / "new-index"
    build_index(path, DOCUMENTS, embed, FINGERPRINT)
    with running_service(create_app(partial(ChromaBackend, path), retrieval_enabled=True, startup_timeout=15)) as client:
        response = client.post("/retrieve", json={"content": "alpha"})
        assert response.status_code == 200
        assert response.json()["response"][0] == "Synthetic alpha product"
    path.rename(tmp_path / "closed-index")


def test_real_index_reopen_query_and_close(tmp_path):
    path = tmp_path / "new-index"
    metadata = build_index(path, DOCUMENTS, embed, FINGERPRINT)
    assert metadata["dimension"] == 2 and metadata["document_count"] == 2
    catalog = ChromaCatalog(path, embed, FINGERPRINT)
    try:
        assert catalog.retrieve("alpha")["response"][0] == "Synthetic alpha product"
        assert catalog.retrieve("beta")["response"][0] == "Synthetic beta product"
    finally:
        catalog.close()
    catalog = ChromaCatalog(path, embed, FINGERPRINT)
    catalog.close()
    # Successful rename is an additional Windows file-handle regression check.
    path.rename(tmp_path / "closed-index")


def test_existing_index_never_overwritten_and_wrong_model_refused(tmp_path):
    path = tmp_path / "new-index"
    build_index(path, DOCUMENTS, embed, FINGERPRINT)
    marker = (path / MANIFEST).read_bytes()
    with pytest.raises(ValueError, match="must not exist"):
        build_index(path, DOCUMENTS, embed, FINGERPRINT)
    with pytest.raises(ValueError, match="metadata mismatch"):
        ChromaCatalog(path, embed, "b" * 64)
    assert (path / MANIFEST).read_bytes() == marker


def test_incomplete_index_and_tampered_metadata_are_refused(tmp_path):
    missing = tmp_path / "incomplete"
    missing.mkdir()
    with pytest.raises(ValueError, match="completion marker"):
        ChromaCatalog(missing, embed, FINGERPRINT)
    assert list(missing.iterdir()) == []
    path = tmp_path / "new-index"
    build_index(path, DOCUMENTS, embed, FINGERPRINT)
    metadata = json.loads((path / MANIFEST).read_text())
    metadata["document_count"] = 1
    (path / MANIFEST).write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="inconsistent"):
        ChromaCatalog(path, embed, FINGERPRINT)


def test_embedding_dimension_mismatch_is_not_a_different_search(tmp_path):
    path = tmp_path / "new-index"
    build_index(path, DOCUMENTS, embed, FINGERPRINT)
    catalog = ChromaCatalog(path, lambda texts: [[1.0, 0.0, 0.0]], FINGERPRINT)
    try:
        with pytest.raises(ValueError, match="embedding vector"):
            catalog.retrieve("alpha")
    finally:
        catalog.close()
