# CLAUDE.md

Invariants for working on SOAP Opera. Read before changing storage, config
flow, or integration setup/teardown.

- **Migrations are additive-only and forward-only.** One function per schema
  version increment, registered in `storage/migrations/__init__.py`. Never
  rename or repurpose a column, and never edit a migration once it has
  shipped — add a new one instead. Every intermediate version must be a
  valid resting state: the database consistent and the integration
  functional at every number in the sequence, because the schema version is
  committed after each step, not once at the end.

- **The medical record is permanent and never purged.** Telemetry
  (`AuditLog.capture_event`) and the JSONL log it writes are bounded and
  rotated. Keep that asymmetry — the database is the record of truth, the
  log is not.

- **Zero add-ons required.** The integration must work fully on a stock HAOS
  install. Any external database (Postgres mirror) is optional, never
  required for core functionality.

- **Prefer zero new manifest requirements.** SQLAlchemy Core is used because
  it ships with Home Assistant (recorder depends on it) and costs nothing.
  Justify any new `requirements` entry explicitly before adding one.

- **The integration must remain fully usable without the companion Lovelace
  card.** The card is a convenience layer, not a dependency.

- **All database work goes through `hass.async_add_executor_job`.** Nothing
  touches the event loop directly.

- **The external Postgres target is a rebuildable mirror, never a second
  primary.** On outage: mark it stale, stop writing to it, rebuild it on
  reconnect. Never queue writes for it.

- **Every domain record's primary key is a UUIDv7** (`sa.Uuid()`, Python-side
  default `uuid.uuid7`), not an autoincrement integer. Time-ordered, so
  SQLite inserts still append instead of fragmenting the B-tree the way
  random UUIDv4 would. It also means an id assigned in SQLite is the same id
  the Postgres mirror uses — no remapping table needed when the mirror is
  rebuilt. Internal bookkeeping tables that aren't domain records (e.g.
  `schema_version`) are exempt and stay plain autoincrement integers.

- **The database is authoritative; a config entry's data is just how it got
  collected.** `async_setup_entry` upserts the corresponding database row
  every time it runs (keyed on `entry_id`, so an edit updates rather than
  duplicates), and an update listener reloads the entry on any edit so that
  sync actually happens on a rename, not just at next restart. Any future
  entity should read from the database, not from `entry.data`.

- **A migration module's own Table objects are frozen snapshots, not the
  current shape.** Never import one migration's Table definition into
  another migration, and never let runtime code (queries, upserts) reuse a
  migration's Table object. `storage/database.py` keeps its own copy of each
  domain table representing the current cumulative shape; update it by hand
  in the same change that adds a migration altering that table.
  `schema_version` (in `storage/tables.py`) is the sole exception, shared
  between the runner and migration 001, because it is locked in shape
  forever and no migration will ever alter it.

- **SQLite connections get `_configure_sqlite_engine` before first use**
  (foreign_keys=ON, WAL, a busy_timeout, and real transactional DDL via the
  isolation_level=None + explicit `BEGIN IMMEDIATE` recipe — pysqlite
  otherwise autocommits DDL before SQLAlchemy's transaction even starts, and
  a plain `BEGIN` can deadlock a concurrent SELECT-then-write against a
  BEGIN IMMEDIATE writer). Any new engine construction must call it before
  the first connection is used.
