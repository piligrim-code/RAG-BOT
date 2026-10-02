from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from vector_backend import LocalBackend, _documents, _vectors, embedding_fingerprint, local_embedder
import vector_backend


def test_string_generation_invokes_llama_and_closes_model(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.gguf"
    path.write_bytes(b"synthetic fixture, not real model weights")
    model = SimpleNamespace(create_chat_completion=Mock(return_value={
        "choices": [{"message": {"content": '{"sku":"synthetic-a"}'}}]}), close=Mock())
    constructor = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=constructor))
    backend = LocalBackend(model_path=path)
    try:
        assert backend.invoke("generate", "Synthetic query") == {"res_content": '{"sku":"synthetic-a"}'}
        model.create_chat_completion.assert_called_once_with(
            messages=[{"role": "user", "content": "Synthetic query"}], max_tokens=512, temperature=0)
        assert constructor.call_args.kwargs["n_gpu_layers"] == 0
        with pytest.raises(ValueError, match="not configured"):
            backend.invoke("retrieve", "query")
    finally:
        backend.close()
    model.close.assert_called_once()


def test_partial_startup_closes_catalog_if_model_loading_fails(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.gguf"
    path.write_bytes(b"synthetic fixture")
    catalog = SimpleNamespace(close=Mock())
    monkeypatch.setattr(vector_backend, "ChromaCatalog", Mock(return_value=catalog))
    monkeypatch.setattr(vector_backend, "embedding_fingerprint", Mock(return_value="a" * 64))
    monkeypatch.setattr(vector_backend, "local_embedder", Mock(return_value=Mock()))
    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=Mock(side_effect=RuntimeError("Synthetic failure"))))
    with pytest.raises(RuntimeError, match="Synthetic failure"):
        LocalBackend(model_path=path, embedding_dir=tmp_path, index_dir=tmp_path)
    catalog.close.assert_called_once()


def test_embedder_requires_offline_local_assets_and_no_remote_code(tmp_path, monkeypatch):
    encoder = Mock(return_value=[[1.0, 0.0]])
    constructor = Mock(return_value=SimpleNamespace(encode=encoder))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=constructor))
    embed = local_embedder(tmp_path)
    assert embed(["Synthetic"]) == [[1.0, 0.0]]
    assert constructor.call_args.kwargs == {"device": "cpu", "local_files_only": True, "trust_remote_code": False}
    encoder.assert_called_once_with(["Synthetic"], normalize_embeddings=True, show_progress_bar=False)


def test_embedding_fingerprint_binds_names_and_bytes(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        embedding_fingerprint(tmp_path)
    path = tmp_path / "config.json"
    path.write_text("synthetic-v1")
    first = embedding_fingerprint(tmp_path)
    assert first == embedding_fingerprint(tmp_path)
    path.write_text("synthetic-v2")
    second = embedding_fingerprint(tmp_path)
    assert first != second
    path.rename(tmp_path / "other.json")
    assert embedding_fingerprint(tmp_path) != second


@pytest.mark.parametrize("values", [[], [None], [[True]], [[float("nan")]], [[0, 0]], [[1], [1, 2]]])
def test_bad_vectors_refused(values):
    with pytest.raises(ValueError):
        _vectors(values, len(values))


@pytest.mark.parametrize("documents", [[], [{"id": "a", "text": "x", "extra": 1}],
    [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}],
    [{"id": "a", "text": "\U0001f600" * 2000}]])
def test_bad_document_snapshot_refused(documents):
    with pytest.raises(ValueError):
        _documents(documents)
