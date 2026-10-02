# Validated Catalog Dialogue

This stage qualifies the catalog-selection path, not generative RAG answers,
embedding quality, a live Telegram bot or broker-restart failover.

## Fields And Values

The canonical names are the existing Russian SKU, category, description and
price fields. The exact English aliases are `sku`, `category`, `description`
and `price`. Field-name case and surrounding whitespace are normalized.
Duplicate aliases for the same field are rejected, even if their values agree.
Brand, multiple SKUs and arbitrary SQL/operator fields are not supported.

Text filter values must be nonempty UTF-8 strings of at most 100 characters,
without NUL; surrounding whitespace is removed. Matching is exact and
case-insensitive, not substring or semantic search. Price conditions use `<`,
`>`, `<=`, `>=` or `=`, with nonnegative 32-bit integers. Booleans, numeric
strings, floats, unsupported operators and impossible integer ranges fail.

The same validator runs at the extraction boundary, before RPC dispatch and
before direct database queries. Unsupported fields never silently broaden a
query. Explicit empty RPC filters still mean catalog browsing; that operation
is distinct from an unrecognized natural-language turn.

## Model Patch And State

The model response is one JSON object, optionally inside one complete `json`
code fence. Leading/trailing prose, duplicate keys, nonfinite numbers, arrays,
oversize output and malformed JSON are errors, not an empty patch.

An omitted field keeps the existing filter. Null, or the legacy Russian
no-preference marker, removes that field. A price object replaces the entire
previous price range; it is not recursively merged with stale bounds. The prompt
includes current validated filters so a model can preserve intended bounds.
This protocol does not prove that a real model will understand the user's intent.

`run_catalog_turn(query, previous_filters, extract=..., rpc_client=...)` is
stateless. It gives the extractor a copy of prior filters, validates the returned
patch and queries the catalog using another copy. It returns a `CatalogTurn`
containing filters, reply and a clarification flag. No caller-owned state is
modified, including nested price objects.

When all constraints are absent or explicitly cleared, the turn returns a
clarification with empty state without querying the catalog. An unchanged
nonempty filter set can still be searched. Zero matches with valid constraints
is a normal result, not an infrastructure error.

The caller owns persistence. The Telegram text handler sends the
reply before saving the new filter state. Failed extraction, lookup or reply
does not intentionally advance that state. Delivery followed by a state-store
failure is still ambiguous. The adapter now serializes same-session handlers
inside one process. Durable state, cross-process serialization, replay protection
and exactly-once notification guarantees are not implemented here.

## HTTP And RPC Limits

- User text: at most 4,000 characters. Parsed model patch: at most 4,096 UTF-8
  bytes. Custom HTTP envelope: at most 16 KiB, streamed with a running byte cap.
- The configured endpoint receives POST `{"content": "<prompt>"}` and returns
  `{"res_content": "<JSON filter patch>"}`. Only this envelope is accepted.
  Endpoint configuration comes from an explicit argument or `LLM_URL` at call
  time, never from the user query. URL credentials/query strings/fragments are
  rejected. HTTP is allowed for trusted local/private services; use reviewed
  HTTPS and authentication before sending data across untrusted networks.
- Aiohttp uses a 30-second total request timeout by default, at most 5 seconds
  for connection and 10 seconds between reads. Redirects and retries are
  disabled. Environment proxies are ignored and compressed replies are rejected.
- The dialogue has a 45-second asynchronous deadline covering extraction and
  lookup. Caller cancellation propagates. These are not process-level limits
  for blocking code. The broker worker offloads SQL to an owned thread and drains
  it before shutdown; database deadlines are documented in `worker-lifecycle.md`.
- Worker JSON requests above 16 KiB and duplicate keys are rejected before
  dispatch. The AMQP library has already received the body at that point; this
  is not a broker-wide message-memory limit.
- Database lookups default to 100 rows, ordered by SKU. The direct library
  supports an explicit 1..1,000 limit or `None` for a deliberately unbounded
  export. RPC filters cannot select that export mode. The optional vector indexer
  now requires an explicit reviewed JSON snapshot and does not open the database.
- Replies show at most three retrieved products, not every matching product,
  and are bounded to 3,500 UTF-16 units. There is no pagination/total-match count.

Errors expose fixed stages/codes rather than raw model replies, queries or
transport exception URLs. No payload logging or automatic retry was added.
The model prompt treats query content as data, but semantic prompt-injection
resistance and catalog data quality are not proven by structural validation.

## Verification And Follow-Up

The default suite uses synthetic values, SQLite, mocked RPC and actual temporary
loopback HTTP servers. It checks input normalization, patch isolation, timeouts,
cancellation, redirects, oversize responses and malformed model output.
The opt-in suite runs the same dialogue through loopback HTTP, actual RabbitMQ
and PostgreSQL, including follow-up constraints and recovery after SQL errors.

All model replies are predetermined fixtures. No weights, customer records,
provider credentials or Telegram delivery are involved. Real relevance/quality,
real model/embedding quality, multi-process delivery/concurrency, migrations,
authentication and operational deployment remain
separate qualification tasks. No existing database or queue was migrated here.

The bot lifecycle is exercised with the actual aiogram dispatcher and a strict
in-memory Telegram transport; see `bot-lifecycle.md`. Owned RabbitMQ application
restart scenarios and their limits are described in `broker-recovery.md`.
The optional local model/vector service and actual Chroma fixture coverage are
documented in `model-service.md`; the bot does not automatically use `/retrieve`.
