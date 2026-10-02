# Catalog Worker Lifecycle

The broker consumer remains asynchronous, but the existing psycopg2/SQLAlchemy
database interface is synchronous. `CatalogExecutor` moves catalog dispatch to
one owned execution thread so database work does not block the consumer's event
loop, heartbeats or unrelated asynchronous tasks.

## Admission And Ownership

- One executor admits at most one active request. Additional direct submissions
  fail immediately rather than entering an unbounded thread-pool queue.
- The broker still uses prefetch one and sequential request handling. This is
  bounded concurrency, not a throughput or multi-worker load qualification.
- Every lookup owns a SQLAlchemy Session. The executor owns its thread only;
  the caller owns the database. `serve_catalog` drains and closes the executor
  before its caller can dispose the database in `main`.
- Once close starts, new work and context re-entry are refused. Close is
  idempotent. Completed operation errors do not leave the executor slot occupied.

## Cancellation And Shutdown

Cancellation cannot stop an already executing synchronous database call.
Awaiting dispatch uses a shielded future. When cancellation arrives, the
executor waits for the operation to finish, consumes its outcome, releases
the slot and re-raises cancellation. Repeated cancellation does not dispose
resources while the database thread is still using them.

Close likewise drains pending work before joining the owned thread. A cancelled
close still completes that cleanup and then propagates cancellation. No query
is automatically replayed. There is no attempt to forcibly terminate Python
threads or asynchronously dispose a connection used by another thread.

An arbitrary stuck callback, broken network path or driver/platform stall can
still delay draining. Database deadlines reduce ordinary SQL waits but do not
constitute a universal hard process deadline. Production supervision must allow
graceful draining and define a separate hard-stop policy. Forced process kills
are outside the graceful-cleanup guarantee. Database construction is still a
synchronous startup step before the broker consumer starts handling traffic.
It now requires complete explicit configuration and probes required columns with
zero returned rows, without DDL. Schema initialization is a separate command;
see `database-startup.md`. Startup probe failures dispose the engine and stop the
worker before it begins consuming requests.

## Database Limits

New PostgreSQL connections use explicit driver options:

- Connection attempt: 5 seconds.
- Statement execution: 5,000 ms by default.
- Lock wait: 2,000 ms by default.
- Idle transaction: 10,000 ms.
- Pool: two connections, no overflow, two-second checkout wait and pre-ping.

`DBClient` permits explicit positive integer statement/lock deadlines up to
60,000 ms; the lock deadline cannot exceed the statement deadline. Invalid
configuration is rejected before engine creation. Options apply to newly created
connections, including schema initialization, not to an unrelated deployment.
The settings are separate limits, not one aggregate end-to-end request deadline.

The statement and lock checks are exercised against real disposable PostgreSQL.
After a deadline error, the failed Session is closed/rolled back and a new
request can succeed. The SQLAlchemy pool is not an automatic replay mechanism
for failed business queries; health checks concern connection checkout.

## Late Replies

An RPC client may time out or close its exclusive callback queue while a read
continues. Worker reply publication is explicitly non-mandatory, so a vanished
reply queue does not require successful routing before serving later requests.
The reply is disposable and is not retained/retried for another client.

This does not make request/reply messages durable or provide exactly-once
results. Correlation IDs and client deadlines continue to determine which
pending call can consume a reply. The owned application-restart scenarios and
their limits are described in `broker-recovery.md`.

## Verification

Default tests use controlled thread events to verify event-loop responsiveness,
single-job admission, failure-slot release, cancellation, repeated cancellation
and cancelled-close cleanup. No live database is used in those tests.

The opt-in PostgreSQL/RabbitMQ suite checks a real statement timeout, a real
exclusive table-lock timeout while an asynchronous heartbeat continues, a
successful next request and a late reply after the client queue was removed.
All resources and rows belong to the disposable runner. These checks do not
claim production load, arbitrary network-partition recovery or real Telegram/LLM
operation. No existing database settings or queue was changed by this stage.
