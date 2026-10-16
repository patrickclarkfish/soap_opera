# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added

- Migration 002: `medication` and `regimen` tables. Regimens are versioned
  (a dosing change end-dates the current row and inserts a successor) and
  effective-dated, with at most one in force per animal and medication.
  Schema and storage helpers only; nothing user-facing yet.
- Regimens as config subentries of an animal. Pick an existing medication
  or define one inline. Editing a dosing field records a new version from
  an effective date (default today). Editing the indication or prescribing
  vet corrects the current version. Removing a regimen end-dates it and
  keeps its history.
- Migration 003: `administration`, the record of each dose (counted units,
  strength per unit, and canonical amount), and `capture_event`, the
  capture telemetry behind idempotency.
- `soap_opera.log_dose` service: the one funnel for recording a dose.
  Attribution comes from the calling user automatically. Idempotency is
  enforced by the database. Guardrails (minimum interval, single-dose
  ceiling, rolling 24-hour total) warn and record but never block, and an
  override is recorded. It returns the administration id and the guardrail
  outcomes. Every dose is written to the audit log and fires
  `soap_opera_dose_logged`.
- Per regimen: a "log dose" button and an "administration" event entity.
- `soap_opera.export` service (admin only). Writes every table, as JSON or
  as one CSV per table, to a new directory inside the config directory,
  with a manifest recording the schema version. It reads one consistent
  snapshot, writes atomically, and never overwrites.

### Fixed

- The pre-migration backup is now taken with `VACUUM INTO` to
  `soap_opera.db.v<N>.backup`, named for the schema version it was taken
  from. It no longer overwrites an earlier backup: if a migration fails and
  is retried, the backup from the first attempt is kept. It also captures
  data still in the WAL file. The old checkpoint-then-copy failed outright
  inside the explicit `BEGIN IMMEDIATE` transaction.
- The engine is disposed if a migration fails, instead of leaking.
- Audit log writes are flushed and fsynced. A write failure is logged as a
  warning instead of propagating to the caller.

### Changed

- One `AuditLog` is created alongside the shared database on
  `SoapOperaData`. Nothing writes to it yet.

## [0.1.0] - 2026-09-16

### Added

- Repository structure, integration skeleton, and storage + migration
  machinery for the SOAP Opera custom integration.
- Config flow collecting one animal per config entry (name, species, optional
  breed, optional date of birth), registering one device per entry.
- Shared SQLite database, owned at the integration level and refcounted
  across config entries, built on SQLAlchemy Core.
- Migration runner modeled on Home Assistant's recorder: sequential,
  one-increment-at-a-time application with a per-step commit and a
  downgrade guard.
- Migration 001, which creates only the `subjects` table and schema version
  tracking. The rest of the data model lands in 002 and later.
- Append-only JSONL audit log.
- CI: hassfest and HACS validation, plus ruff / mypy / pytest.

[Unreleased]: https://github.com/patrickclarkfish/soap_opera/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/patrickclarkfish/soap_opera/releases/tag/v0.1.0
