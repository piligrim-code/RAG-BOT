import asyncio
import json
import subprocess
import sys

import pytest

from model_process import MAX_REQUEST_BYTES
from tests.model_fixtures import SyntheticBackend
from tests.http_service import running_service
from vector import create_app


@pytest.fixture(scope="module")
def service():
    with running_service(create_app(SyntheticBackend, retrieval_enabled=True, startup_timeout=15)) as client:
        yield client


def test_import_does_not_load_assets_or_native_libraries():
    result = subprocess.run([sys.executable, "-c", "import sys, vector, vector_backend; "
        "assert not any(x in sys.modules for x in ('llama_cpp', 'chromadb', 'sentence_transformers')); "
        "assert not hasattr(vector, 'app')"], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("content", ["Synthetic query", [{"role": "user", "content": "Synthetic query"}]])
def test_generate_string_and_chat_share_working_contract(service, content):
    response = service.post("/generate", json={"content": content})
    assert response.status_code == 200
    assert json.loads(response.json()["res_content"]) == {"sku": "synthetic-a"}


def test_retrieval_and_health(service):
    assert service.get("/health").json() == {"ready": True, "retrieval": True}
    assert service.post("/retrieve", json={"content": "query"}).json() == {"response": ["Synthetic product"]}


def test_actual_model_client_accepts_server_string_contract(service):
    from catalog_filters import SKU
    from llm import extract_filter_patch
    result = asyncio.run(extract_filter_patch("Synthetic query", {}, url=str(service.base_url.join("/generate"))))
    assert result == {SKU: "synthetic-a"}


@pytest.mark.parametrize("payload", [{}, {"content": ""}, {"content": 3}, {"content": []},
    {"content": "x", "extra": "synthetic-private"}, {"content": [{"role": "tool", "content": "x"}]},
    {"content": [{"role": "user", "content": "x", "extra": 1}]}, {"content": "x" * 12001}])
def test_invalid_requests_are_generic_without_echoing_input(service, payload):
    response = service.post("/generate", json=payload)
    assert response.status_code == 400 and response.json() == {"error": "invalid_request"}


@pytest.mark.parametrize("body", [b"bad-json", b'{"content":"x","content":"y"}'])
def test_bad_wire_json(service, body):
    assert service.post("/generate", content=body).status_code == 400


def test_body_limit_applies_without_content_length(service):
    response = service.post("/generate", content=iter([b"x" * 40000, b"x" * 40000]))
    assert response.status_code == 413
    assert service.post("/generate", content=b"x" * (MAX_REQUEST_BYTES + 1)).status_code == 413


def test_backend_errors_do_not_echo_native_detail(service):
    response = service.post("/generate", json={"content": "fail"})
    assert response.status_code == 502 and response.json() == {"error": "backend_failure"}


def test_unconfigured_retrieval_does_not_call_backend():
    with running_service(create_app(SyntheticBackend, startup_timeout=15)) as client:
        response = client.post("/retrieve", json={"content": "crash"})
        assert response.status_code == 503 and response.json()["error"] == "retrieval_not_configured"
        assert client.get("/health").status_code == 200


def test_http_deadline_marks_service_unhealthy_until_restart():
    with running_service(create_app(SyntheticBackend, timeout=0.2, startup_timeout=15)) as client:
        response = client.post("/generate", json={"content": "sleep"})
        assert response.status_code == 504 and response.json() == {"error": "timeout"}
        assert client.get("/health").status_code == 503
        assert client.post("/generate", json={"content": "next"}).json() == {"error": "unavailable"}
