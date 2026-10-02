# RAG-BOT

A historical Telegram sales-assistant prototype. The tested public slice is
validated catalog dialogue, model-service HTTP parsing and RabbitMQ/SQL lookup,
not a complete RAG product. The old diagram and model/vector experiments are
historical context; real model quality and Telegram delivery remain unqualified.

## Offline Demo

Python 3.12, no account, model, broker, database or network required:

```sh
python demo.py
```

This runs the same catalog dispatcher and reply formatter against two
explicitly synthetic products. The output contains a matching result and a
no-results message, plus a synthetic selection/reset dialogue. Clearing all
filters asks for clarification rather than querying the entire catalog.
It is not an LLM quality or live integration benchmark.

## Tests

In a dedicated virtual environment:

```sh
python -m pip install -r requirements-test.txt
python -m pytest tests -q
```

Tests cover empty/malformed responses, the `extract_catalog` contract,
callback queues, connection reuse, deadlines, cancellation and pending-call
cleanup, strict filter normalization and nonmutating follow-up state. The default
suite mocks broker calls, uses in-memory SQLite for SQL/resource tests and creates
temporary loopback HTTP servers with synthetic model replies. No public model
service or Telegram account is used. Live SQL/AMQP tests are skipped unless explicitly
enabled. CI runs the default suite on Python 3.12, Linux and Windows.

## Disposable Integration Test

With a local Linux Docker engine running and test requirements installed:

```sh
python tools/run_integration.py
```

This creates dedicated PostgreSQL and RabbitMQ containers with generated
credentials and randomly allocated loopback-only ports. It inserts two
synthetic products and tests actual SQL/AMQP request/reply, filters, empty
results, concurrent clients, malformed requests, SQL failure recovery,
timeouts, cancellation and a new call after a closed client connection.
It also exercises a synthetic model HTTP endpoint through multi-turn filter
updates, real AMQP and real SQL; duplicate/oversize wire requests and unknown
filters are rejected, and an actual SQL error is followed by a successful request.
Database sessions are scoped per request; worker shutdown closes its database.
The PostgreSQL adapter selects the declared psycopg2 driver explicitly and
bounds each connection attempt to ten seconds.

The runner removes only its labeled containers and their anonymous volumes,
including on ordinary test failures. Public dependency images stay cached.
Remote Docker contexts and `DOCKER_HOST` overrides are refused. A hard process
kill or Docker outage can prevent cleanup; the runner reports any owned names
that could not be removed. Docker access is privileged: use a trusted local
engine or disposable CI runner. CI has a separate Linux integration job.

This is not Telegram, LLM, vector-search, TLS, broker-restart failover or
production-load qualification. The database adapter still uses synchronous
SQL inside the worker and only the catalog path is supported. Test output
records dependency image IDs; tags and Python requirement ranges are not a
fully locked production environment.

## Corrected Catalog Contract

- Request: `{"extract_catalog": {}}`, response: a JSON array of product objects.
- Recognized filters are combined with AND, including SKU. The order of JSON
  keys must not change the result; SKU does not bypass category or price filters.
- Russian field names and explicit English aliases are normalized consistently
  before model/RPC/SQL use. Unknown fields, alias collisions, malformed prices,
  contradictory ranges and duplicate JSON keys fail instead of being ignored.
- Catalog lookups return at most 100 rows ordered by SKU. The legacy vector
  export opts into `limit=None` explicitly; that experiment remains unqualified.
- Unsupported operations return a generic error envelope; no raw exception or
  request payload is sent back or logged by the worker.
- The request queue is durable `catalog_store`; replies use a server-named exclusive
  callback queue per client connection. Clients do not consume request messages.
- Each call has an overall 15-second deadline, including connect and publish.
  Failed/cancelled calls release their correlation entries. Calls are not retried.
- Empty results produce a normal no-products response; matching context is
  limited to three products and user-facing text is capped at 3,500 UTF-16 units.
- The current Telegram path uses an in-memory dialog ID, not the unimplemented
  legacy `new_dialog`/`add_message` database API.

An existing non-durable `catalog_store` cannot be redeclared as durable. For
that legacy deployment, stop producers, drain/inspect pending work, stop the
old worker and coordinate queue replacement before starting this version.
The application never deletes an existing queue or enables deprecated broker
features automatically. Test only in a disposable environment first. Durable
queue metadata does not make these transient RPC messages persistent, and the
client does not promise retries or exactly-once processing.

## Optional Live Setup

`requirements.txt` lists dependencies for the catalog worker and Telegram
adapter. Installing them is not an end-to-end deployment qualification.

Configure these privately in the process environment or an untracked `.env`:

| Component | Configuration |
| --- | --- |
| RabbitMQ client/worker | `RABBITMQ_URL` (required, no embedded default password) |
| Telegram adapter | `BOT_TOKEN`, `ADMIN_ID` |
| PostgreSQL legacy adapter | `username`, `password`, `host`, `port`, `database` |
| Model slot extractor | `LLM_URL` |

The slot extractor expects the historical custom HTTP API: POST
`{"content": "..."}` and response `{"res_content": "<JSON filters>"}`.
This is not the native llama.cpp API. The historical `vector.py` experiment is
not a qualified deployment of this contract; provision/review the model service
separately. Requests have a 30-second total timeout, shorter connect/read limits,
no redirects/retries, no environment proxy use, and a 16 KiB response limit.
Raw responses and filter values are not logged by the extractor.

`conversation.run_catalog_turn` validates an explicit patch, preserves omitted
filters and returns new state only after a successful lookup or clarification.
Malformed model output cannot fall back to an unfiltered query. The Telegram
handler saves the returned state after its reply call succeeds. This does not
provide atomic Telegram delivery or serialization of simultaneous same-chat
turns. See `docs/catalog-dialogue.md` for supported fields and recovery limits.

With separately provisioned services and reviewed synthetic data:

```sh
python -m pip install -r requirements.txt
python rabbitmq.py
# In a separate configured process:
python main.py
```

Do not use real customer data as the first integration test. Database schema
migrations, real model interpretation quality, authentication,
reconnection under broker restarts, vector retrieval, production concurrency
and data-retention policies still need a separate review. The stored legacy
dialog helper methods reference models not supplied by this snapshot and are
not part of the corrected catalog contract.

No Telegram or real LLM is contacted in either suite. HTTP fixtures listen on
loopback only; the opt-in suite also uses its runner's disposable DB and broker.
