# Database Startup

`DBClient` resolves configuration when it is constructed, not when the Python
module is imported. Arguments override environment variables; an explicitly
invalid value such as `None` is rejected, not replaced by an environment fallback.

The existing environment names remain supported: `username`, `password`, `host`,
`port`, `database`. All five are required. Username, password, host and database must be
nonempty strings without NUL/newline characters; username, host and database must
not be whitespace-only. Password whitespace is preserved. Port must be an integer
in 1..65535 or its ASCII decimal string. Invalid configuration fails before opening
an engine and the error identifies the field, not its value.

## Ordinary Worker

```sh
python rabbitmq.py
```

This requires `RABBITMQ_URL` as well as the complete DB configuration. Ordinary
database startup does not run `CREATE`, `ALTER`, `DROP`, migrations or imports.
It performs a zero-row SELECT of the required `products_trio` columns. A missing
table, incompatible column names, connection failure or permission failure stops
startup and disposes the engine, with a generic `DatabaseStartupError`.

This probe is not a complete schema/type/version migration audit, a row-quality
check or a proof of future availability. Review the role's schema search path
and permissions for the intended deployment. No catalog rows are returned by the
probe. Database statement/lock/connection limits still apply; see
`worker-lifecycle.md` for the independent bounds and shutdown ordering.

Use an appropriately restricted read-only role for the worker. The owned-service
suite verifies startup and lookup with SELECT/USAGE privileges and transactions
configured read-only, not a superuser-only runtime.

## Explicit Initialization

Only after selecting the intended database, reviewing its existing schema and
backups, and supplying a suitably authorized provisioning role:

```sh
python db_client.py init-schema
```

The command loads an optional untracked `.env` without overriding an existing
process environment, creates missing declared catalog tables and checks required
columns. It never drops tables, imports products, stores chat transcripts or
migrates existing columns. Re-running it against a compatible schema does not
replace existing data. It is not a substitute for a reviewed migration process.
An incompatible existing schema must be resolved separately; do not delete an
unknown table to make this command pass.

For library callers, `DBClient(..., create_schema=True)` is the same explicit
opt-in. Only a real boolean is accepted. The default is `False`; there is no
persistent environment switch that silently enables DDL for every worker startup.
Return to the restricted runtime credentials before starting the worker.

Configuration errors name only the invalid field. SQLAlchemy hides statement
parameters, and startup failures do not relay the driver exception to the normal
CLI traceback. Investigate authorized database/service logs separately; do not
paste connection strings or credentials into public issues.

## Resource And Compatibility Boundary

Each catalog request still owns a separate SQLAlchemy Session/transaction. The
unused shared Session is removed. `close()` is idempotent and final for this
client; later lookups fail rather than silently opening a fresh connection pool.
The caller must drain active work before closing, as the worker already does.

The historical `new_dialog` and `add_message` methods referenced models absent
from this snapshot and were not a working persistence API. They are removed;
the supported adapter retains temporary filters through `BotSessions`, not DB
transcripts. No chat-persistence schema is introduced by this change.

Tests cover missing/invalid configuration before engine creation, construction-time
environment resolution, ordinary startup without DDL, zero-row probing, explicit
initialization, failure cleanup and final close. The PostgreSQL suite starts with
a new owned database, first verifies ordinary startup refuses its missing schema,
initializes via the actual CLI, then uses normal startup. Its read-only-role test
creates and removes only a uniquely named role in the runner-owned PostgreSQL
container. No deployed role or database is changed by these tests.
