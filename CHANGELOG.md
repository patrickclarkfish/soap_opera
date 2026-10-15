# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

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
