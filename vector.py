"""Explicit local generation/retrieval service; no asset work at import."""
import argparse
from contextlib import asynccontextmanager
from functools import partial
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from catalog_filters import FilterValidationError, load_json_object, validate_text
from model_process import MAX_REQUEST_BYTES, ModelProcess, ModelProcessError


def _content(payload, operation):
    if set(payload) != {"content"}:
        raise FilterValidationError("Expected only content")
    content = payload["content"]
    if operation == "retrieve" or isinstance(content, str):
        return validate_text(content, 4000 if operation == "retrieve" else 12000)
    if not isinstance(content, list) or not 1 <= len(content) <= 16:
        raise FilterValidationError("Expected bounded chat messages")
    total = 0
    for message in content:
        if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or message["role"] not in ("system", "user", "assistant")):
            raise FilterValidationError("Invalid chat message")
        total += len(validate_text(message["content"], 12000))
    if total > 12000:
        raise FilterValidationError("Chat messages too large")
    return content


def create_app(factory, *, retrieval_enabled=False, timeout=30, startup_timeout=120):
    @asynccontextmanager
    async def lifespan(app):
        runtime = ModelProcess(factory, timeout=timeout, startup_timeout=startup_timeout)
        app.state.runtime = runtime
        try:
            await runtime.start()
            yield
        finally:
            await runtime.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        ready = app.state.runtime.ready
        return JSONResponse({"ready": ready, "retrieval": retrieval_enabled},
                            status_code=200 if ready else 503)

    async def dispatch(request, operation):
        if operation == "retrieve" and not retrieval_enabled:
            return JSONResponse({"error": "retrieval_not_configured"}, status_code=503)
        try:
            length = request.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_REQUEST_BYTES):
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    return JSONResponse({"error": "request_too_large"}, status_code=413)
                body.extend(chunk)
            content = _content(load_json_object(bytes(body), limit=MAX_REQUEST_BYTES), operation)
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            result = await app.state.runtime.invoke(operation, content)
            if operation == "generate":
                if not isinstance(result, dict) or set(result) != {"res_content"}:
                    raise ValueError("Invalid backend response")
                validate_text(result["res_content"], 12000)
            else:
                if (not isinstance(result, dict) or set(result) != {"response"}
                        or not isinstance(result["response"], list) or len(result["response"]) > 5):
                    raise ValueError("Invalid backend response")
                for document in result["response"]:
                    validate_text(document, 4000)
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid_backend_response"}, status_code=502)
        except ModelProcessError as error:
            status = 504 if error.code == "timeout" else 503 if error.code in ("busy", "unavailable") else 502
            return JSONResponse({"error": error.code}, status_code=status)
        return JSONResponse(result)

    @app.post("/generate")
    async def generate(request: Request):
        return await dispatch(request, "generate")

    @app.post("/retrieve")
    async def retrieve(request: Request):
        return await dispatch(request, "retrieve")

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Serve explicitly supplied local assets on loopback")
    serve.add_argument("--model", required=True)
    serve.add_argument("--embedding-dir")
    serve.add_argument("--index-dir")
    serve.add_argument("--port", type=int, default=8015)
    index = commands.add_parser("index", help="Build a new index from reviewed JSON documents")
    index.add_argument("--documents", required=True)
    index.add_argument("--embedding-dir", required=True)
    index.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    from vector_backend import LocalBackend, _documents, build_index, embedding_fingerprint, local_embedder
    if args.command == "serve":
        if not 1024 <= args.port <= 65535:
            parser.error("port must be in [1024, 65535]")
        if bool(args.embedding_dir) != bool(args.index_dir):
            parser.error("embedding-dir and index-dir must be supplied together")
        import uvicorn
        factory = partial(LocalBackend, model_path=args.model,
                          embedding_dir=args.embedding_dir, index_dir=args.index_dir)
        uvicorn.run(create_app(factory, retrieval_enabled=bool(args.index_dir)),
                    host="127.0.0.1", port=args.port, workers=1, access_log=False)
    else:
        if Path(args.output).exists():
            parser.error("output must be a new index directory")
        path = Path(args.documents)
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            parser.error("documents must be a reviewed JSON file at most 4 MiB")
        payload = load_json_object(path.read_bytes(), limit=4 * 1024 * 1024)
        if set(payload) != {"documents"}:
            parser.error("document file requires only a documents array")
        documents = _documents(payload["documents"])
        fingerprint = embedding_fingerprint(args.embedding_dir)
        metadata = build_index(args.output, documents, local_embedder(args.embedding_dir), fingerprint)
        print(json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    main()
