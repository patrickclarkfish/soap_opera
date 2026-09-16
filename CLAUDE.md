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
