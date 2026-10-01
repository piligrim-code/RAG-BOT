# RAG-BOT

A historical Telegram sales-assistant prototype. The tested public slice is
catalog lookup and its RabbitMQ request/reply contract, not a complete RAG
product. The old diagram and model/vector experiments are historical context.

## Offline Demo

Python 3.12, no account, model, broker, database or network required:

```sh
python demo.py
```

This runs the same catalog dispatcher and reply formatter against two
explicitly synthetic products. The output contains a matching result and a
no-results message. It is not an LLM quality or live integration benchmark.

## Tests

In a dedicated virtual environment:

```sh
python -m pip install -r requirements-test.txt
python -m pytest tests -q
```

Tests cover empty/malformed responses, the `extract_catalog` contract,
callback queues, connection reuse, deadlines, cancellation and pending-call
cleanup. All broker calls are mocked. CI runs on Python 3.12, Linux and Windows.

## Corrected Catalog Contract

- Request: `{"extract_catalog": {}}`, response: a JSON array of product objects.
- Unsupported operations return a generic error envelope; no raw exception or
  request payload is sent back or logged by the worker.
- The request queue is `catalog_store`; replies use a server-named exclusive
  callback queue per client connection. Clients do not consume request messages.
- Each call has an overall 15-second deadline, including connect and publish.
  Failed/cancelled calls release their correlation entries. Calls are not retried.
- Empty results produce a normal no-products response; matching context is
  limited to three products and user-facing text stays below Telegram's limit.
- The current Telegram path uses an in-memory dialog ID, not the unimplemented
  legacy `new_dialog`/`add_message` database API.

## Optional Live Setup

`requirements.txt` lists dependencies for the catalog worker and Telegram
adapter. Installing them is not an end-to-end deployment qualification.

Configure these privately in the process environment or an untracked `.env`:

| Component | Configuration |
| --- | --- |
| RabbitMQ client/worker | `RABBITMQ_URL` (required, no embedded default password) |
| Telegram adapter | `TOKEN`, `ADMIN_ID` |
| PostgreSQL legacy adapter | `username`, `password`, `host`, `port`, `database` |
| Model slot extractor | `LLM_URL` |

The slot extractor expects the historical custom HTTP API: POST
`{"content": "..."}` and response `{"res_content": "<JSON filters>"}`.
This is not the native llama.cpp API and no model service is included.
Requests time out after 30 seconds; raw responses and filter values are no
longer appended to `/data/log.txt`.

With separately provisioned services and reviewed synthetic data:

```sh
python -m pip install -r requirements.txt
python rabbitmq.py
# In a separate configured process:
python main.py
```

Do not use real customer data as the first integration test. Database schema
migrations, model-output normalization against the SQL schema, authentication,
reconnection under broker restarts, vector retrieval, production concurrency
and data-retention policies still need a separate review. The stored legacy
dialog helper methods reference models not supplied by this snapshot and are
not part of the corrected catalog contract.

No live Telegram, database, broker or LLM was contacted in the regression suite.
