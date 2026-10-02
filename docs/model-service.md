# Local Model And Vector Service

The public HTTP contract remains `POST /generate` with `{"content":"..."}` and
`{"res_content":"..."}` in response. The previous string-input bug is fixed:
strings and explicit chat messages both invoke the model. `POST /retrieve` returns
`{"response":["document text", "..."]}` when a reviewed index is configured.
The catalog bot currently uses structured SQL filters; it does not automatically
add vector retrieval to the bot workflow or claim semantic search quality.

## Explicit Assets

Importing `vector` or `vector_backend` does not load native models, connect to a
database, open an index or download weights. There is no module-global ASGI app
with implicit paths. Only the explicit CLI or an injected trusted backend factory
constructs resources. The old LangChain convenience imports and implicit catalog
export are no longer used; the underlying vector store remains Chroma.

Use a dedicated Python 3.12+ environment. Actual native inference needs:

```sh
python -m pip install -r requirements-model.txt
python vector.py serve --model models/reviewed.gguf
```

Installing llama-cpp-python may require a platform-specific native build toolchain.
No weights or dataset are supplied/downloaded by this repository. Review asset
origin, license, format and chat template before supplying local files. The native
adapter defaults to CPU, two threads, 8,192 context tokens and at most 512 output
tokens with temperature zero. These are configuration bounds, not a quality or
latency benchmark. A structurally valid prompt can still exceed a model-specific
token limit. Real GGUF inference and SentenceTransformer quality require separate
qualification with approved assets; CI does not pretend its fixtures are models.

The server binds only `127.0.0.1`, port 8015 by default, with one ASGI worker and
one owned model process. Set the catalog adapter's `LLM_URL` to
`http://127.0.0.1:8015/generate`. No authentication/TLS is added to this local service.
Do not expose or reverse-proxy it to an untrusted network without separate access,
rate-limit and transport controls. Local multi-user access is also a deployment
concern. `/health` exposes only readiness and whether retrieval is configured.

## Rebuild-Only Index

Prepare a reviewed JSON snapshot, not a path to a live database:

```json
{"documents":[
  {"id":"synthetic-a","text":"Synthetic alpha product"},
  {"id":"synthetic-b","text":"Synthetic beta product"}
]}
```

The snapshot accepts at most 1,000 unique IDs and 4 MiB of JSON. Each document is
bounded to 4,000 characters and 4,096 UTF-8 bytes. Split larger documents explicitly;
there is no silent full-database export, truncation or automatic migration.
With an existing parent directory `indexes`:

```sh
python vector.py index --documents data/reviewed-documents.json --embedding-dir models/reviewed-embedding --output indexes/catalog-v1
python vector.py serve --model models/reviewed.gguf --embedding-dir models/reviewed-embedding --index-dir indexes/catalog-v1
```

The embedding directory must contain ordinary local files, not symbolic links or
junctions. Its fingerprint includes relative filenames and bytes. Loading uses
`local_files_only=True`, `trust_remote_code=False` and CPU; this is not a security
guarantee for arbitrary untrusted model artifacts. Reviewed local assets are still
required. Vectors are normalized and indexed with cosine distance, with no Chroma
default embedding function or model auto-download.

Index creation refuses an existing output directory, opens only a new Chroma
collection, verifies its count, closes the client and finally writes a completion
marker. Failure leaves an incomplete new directory for inspection; it is not
deleted, overwritten or treated as ready. Opening an index checks marker schema,
embedding fingerprint, dimensions, collection metadata and document count. Chroma
migrations are validated, not applied, on the serving path. Old unmarked indexes
must be rebuilt into a new directory; no automatic in-place conversion is offered.

Serving does not add/delete catalog documents. Chroma can still maintain its own
local database/index files; this is not an immutable filesystem mount. Do not run
concurrent writers against a serving index. Fingerprints detect incompatible
assets, not malicious local tampering or a hostile administrator. Retrieval returns
up to five distinct documents within a 14,000-byte JSON-string budget. Fewer may
be returned to fit the budget. The response is bounded, not a recall benchmark.

## Admission, Deadlines And Cleanup

HTTP bodies are capped at 64 KiB while streaming; duplicate keys, unknown fields,
invalid message roles and oversized text are rejected before model invocation.
Successful IPC responses are capped at 16 KiB. Model exceptions return fixed error
codes rather than raw prompts, filesystem paths or native exception text.

Only one native operation is admitted at a time; additional calls receive 503
`busy` without queueing. Startup handshake defaults to 120 seconds and an operation
to 30 seconds. A timeout or async caller cancellation retires the owned process, closes
its pipe and drains the IPC thread; no request is replayed. Subsequent requests get
503 `unavailable`, and health becomes 503 until the service is explicitly restarted.
A completed backend exception is a 502 and does not automatically replay the call.
An HTTP client disconnect does not necessarily cancel its server-side task; that
operation can continue until its ordinary deadline. This is distinct from explicit
task cancellation and is not claimed as an immediate disconnect abort.

Graceful idle shutdown asks the backend to close its model and Chroma client.
An active/stalled process is terminated; kill is the final fallback. Join waits
can add up to six seconds to cleanup, beyond the request deadline. Repeated caller
cancellation cannot skip cleanup. OS process creation/termination failure remains
an external supervision concern; these are not hard real-time guarantees.

The model process runs under the service account's permissions. It is lifecycle
isolation, not an execution sandbox or a CPU/RAM/filesystem security boundary.
Forced termination may skip native finalizers. Keep the serving index separately
backed up and treat the service account, local assets and backend factory as trusted.
A hard kill of the parent can bypass Python cleanup and leave a child process.
Configure the external supervisor to stop the entire owned process group/job,
not just the HTTP parent. Unrelated processes must never be included in that scope.

## Verification

Default tests run actual loopback HTTP, spawn real worker processes with synthetic
backends, and cover generation contract, malformed/oversized input, native failures,
stall/exit, cancellation, admission and cleanup. The native model adapter is checked
with an injected constructor, not real weights.

Actual local Chroma checks are separate:

```sh
python -m pip install -r requirements-vector-test.txt
python tools/run_vector_tests.py
```

These build temporary indexes with fixed two-dimensional synthetic vectors, test
reopening, query order, metadata refusal and file-handle release, and traverse actual
HTTP -> owned process -> Chroma. CI runs them on Linux and Windows. No learned
embedding, real relevance, GPU, customer catalog or production load is tested.
