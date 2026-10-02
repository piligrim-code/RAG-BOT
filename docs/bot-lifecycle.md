# Bot Lifecycle

## Supported Adapter

`main.py` is import-safe: no token lookup, dotenv loading, logging setup, Bot,
dispatcher or RPC connection is created at import. `create_dispatcher` accepts
its RPC client, extractor, optional operator ID and session store explicitly.
The command-line entry point reads configuration and owns the polling resources.
Tests use aiogram 3.31; the dependency floor excludes older unqualified lifecycle
implementations. Version ranges are not a fully locked deployment environment.

The supported path is private-chat text lookup with follow-up filters. `/start`,
`/cancel` and `/forget` clear the local session before sending confirmation.
`/help` describes the supported query fields. Unknown commands and attachments
do not reach the extractor. Historical category buttons that only changed text
without applying a catalog filter are no longer offered. Existing callbacks are
acknowledged with a prompt to use text or `/start`.

Operator forwarding is disabled without `ADMIN_ID`. `/operator` explains that
the next text and Telegram user ID will be forwarded. `/cancel` cancels that
state. Attachments, commands and oversized Unicode text are not forwarded.
Questions are not saved in FSM state. Once forwarding returns successfully, the
operator state clears before confirmation to avoid repeating the forwarding just
because confirmation fails. A transport error can still leave delivery uncertain;
there is no automatic retry or exactly-once guarantee. Configure this only after
reviewing who controls the destination and applicable data-handling requirements.

## Session Bounds

`BotSessions` combines aiogram storage and event isolation for one process:

- Default capacity is 1,000 sessions. New sessions receive a busy response when
  full; existing active state is not silently evicted to admit another user.
- Default idle TTL is 1,800 seconds, measured after the last handler releases its
  lock. Expired idle records are pruned on subsequent storage access, not by a
  background timer. `/forget` and shutdown explicitly clear their records.
- A session lock covers state read, extraction, lookup, reply and successful
  state update. Waiting/cancelled handlers cannot remove an active lock. Different
  sessions can proceed independently. Active/waiting sessions do not expire.
- Stored values are copied on read/write. Normal storage contains only validated
  filter objects and optional operator state, not raw transcripts or questions.
- Polling admits at most 32 concurrent update tasks. This bounds admission, not
  fairness, rate limiting, a performance benchmark or protection against all abuse.

State is volatile and lost on restart. There is no Redis, cross-process lock,
persistent update-deduplication ledger or webhook qualification. Run only one
polling process per bot token for this adapter. A process crash after sending a
reply but before saving filters remains an uncertain-delivery boundary.

## Shutdown

The application shields the polling supervisor from caller cancellation and
requests its ordinary stop first. This avoids leaving its internal long-poll task
behind. Startup failure and cancellation also execute owned cleanup: cancel/drain
active update handlers, close FSM storage, close RPC, then close the Telegram
session. Repeated caller cancellation does not interrupt that cleanup.

Telegram requests use a 15-second session timeout; aiogram long polling can use
its own longer polling deadline. Model/dialogue/RPC deadlines are separate.
An arbitrary stalled library or cleanup callback can still prevent graceful
completion; define external supervision and a hard-stop policy separately.

## Evidence Boundary

Default tests use the real dispatcher, filters, FSM middleware, polling supervisor
and typed Telegram methods with a strict in-memory transport that cannot send
HTTP or download files. They cover follow-up state, concurrent users, delivery
failure, capacity/expiry, cancellation, repeated cancellation, operator opt-in,
unsupported input and cleanup. One opt-in test passes dispatcher text through a
loopback synthetic model endpoint, real owned RabbitMQ and real owned PostgreSQL,
then verifies the synthetic reply and reset.

These checks do not send a Telegram message, use a real model or claim live
Bot API permission, webhook, relevance, vector retrieval or load qualification.
Review real-service configuration, model quality, secrets, rate limits, backups,
retention and operator access separately before using customer data.
