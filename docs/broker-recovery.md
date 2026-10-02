# Broker Recovery

## Request Semantics

The catalog RPC client uses one exclusive callback queue per connection. Before
reusing it, the client checks both transport connectivity and channel state.
A disconnected transport fails pending calls with `RpcRemoteError`. Other
transport errors and the overall request deadline can also end a call.
Failed calls remove their correlation entries; late replies are ignored.

The next call discards stale resources and creates a new connection and callback
queue. It may still fail if the broker is not yet available. There is no automatic
application retry, background request queue or promise that every call made
during recovery will succeed. Closing a client cancels its pending calls.

The worker uses a robust connection and restores the consumer. A lost delivery
channel does not terminate the worker solely because its reply or acknowledgement
cannot be sent. An obsolete delivery is not intentionally published on a restored
channel. Cancellation still propagates after SQL draining, including when a
channel error occurs while exiting the message processing context.

An unanswered request may already have reached SQL. RabbitMQ can redeliver an
unacknowledged message; the worker has no persisted deduplication ledger. The
supported catalog operation is read-only. Do not extend this contract to writes
without explicit idempotency and delivery design. Durable queue metadata is not
durable RPC requests, exactly-once processing or retained replies.

## Disposable Checks

`python tools/run_integration.py` creates and owns the test broker and database.
Two scenarios stop/start the RabbitMQ application inside that running container:

1. An established but idle client loses its connection; after consumer restoration,
   a new call succeeds with a different callback queue.
2. A request is held inside a synthetic SQL-read wrapper when the application
   stops. That client call fails, the read is released, and a new call succeeds
   after restoration. The worker must remain alive and pending calls must clear.

The control helper verifies the run nonce, container owner label, immutable
container ID, running state, test database identity and exact loopback endpoint
before invoking RabbitMQ control commands as its service user. Commands use the
verified local Docker endpoint, not a mutable remote context. Only the test-owned
broker is eligible. The runner removes owned containers and anonymous volumes on
ordinary success/failure; a forced kill or failed Docker engine can prevent that.

## Operational Limits

These scenarios are application stop/start tests, not a container/host reboot,
cluster/quorum failover, TLS rotation, partition, storage loss or production load
qualification. They use synthetic products, a disposable single broker and no
Telegram or real model service. They do not establish a production recovery-time
objective.

Supervise both worker and adapter processes. A permanently closed worker
connection or unexpectedly ended consumer is an error, not successful shutdown.
Allow SQL draining on graceful shutdown, and define an external hard-stop policy
for arbitrary driver/network stalls. Observe process health and broker consumer
counts without logging customer payloads or connection credentials. Review
backups, retention, authentication and broker access separately before deployment.
