# SOAP Opera — Pet Medication & Clinical Record Tracking

## Pre-alpha — no stability guarantees

This project is in active early development and is not ready for use with data
you care about.

- **The database schema is unstable.** Migrations between pre-1.0 versions are
  not guaranteed. A release may require deleting your database and starting over.
- **Breaking changes land without notice or deprecation periods**, including to
  entity IDs, service signatures, and configuration.
- **Do not make this your only record.** Keep whatever you use today until 1.0.

Installing at this stage means accepting all of the above.

## Disclaimer

SOAP Opera is a reminder and record-keeping aid. It is not a medical device, not
a dosing authority, and must not be relied upon for clinical decisions. Always
follow your veterinarian's instructions.

SOAP Opera is a planned Home Assistant custom integration for pet medication
administration and clinical record tracking. The name is a pun on SOAP notes
(Subjective, Objective, Assessment, Plan), the standard clinical documentation
format — not the XML protocol.

## What it is

The common pattern for medication tracking in Home Assistant is an NFC tag
scan that fires an automation, which drops an entry on a calendar. That works
as a reminder, but it isn't a record: there's no structured history of what
was given, when, at what dose, or why a dose was skipped or changed.

SOAP Opera is meant to replace that pattern with an append-only medical
record: structured dose events, adherence tracking, inventory, and clinical
observations, stored as data rather than as calendar text.

## Why it exists

Two existing HACS integrations already cover adjacent ground and are worth
using today:

- **[magikh0e/ha-medication-reminder](https://github.com/magikh0e/ha-medication-reminder)**
  — scheduled and PRN dosing with supply tracking.
- **[isabellaalstrom/pet_health](https://github.com/isabellaalstrom/pet_health)**
  — pet health observations with amend/confirm/delete workflows.

If your needs match what those provide, use them — they're further along and
this project is not trying to duplicate that work. SOAP Opera exists because
three specific requirements didn't fit as an extension of either:

1. **Versioned regimens.** A dose change end-dates the old regimen and
   inserts a new one, rather than editing a value in place. Regimens are
   effective-dated, so the history can answer "did the increase from 60mg to
   90mg change anything?" instead of only ever showing the current dose.

2. **Idempotent variable dosing.** Some drugs are dosed in multiples of a
   tablet, adjusted by severity on a given day. Quantity is stated explicitly
   at confirmation time rather than inferred from a count of scans, so a
   duplicate NFC read can't silently double a dose in the record.

3. **Care episodes.** Boarding, a sitter, or a hospital stay is recorded as a
   custody period rather than left to show up as a run of missed doses. A
   day nobody could have scanned the tag should read as an honest gap, not
   as non-adherence.

## Design commitments

These are the constraints the project is being designed against, and the
reason it exists as a separate integration rather than a fork or a PR:

- **Runs on stock Home Assistant OS, no add-ons required.** An optional
  connection to an external Postgres or MariaDB instance is planned for
  people who want to point Grafana at their data, but nothing in the
  integration will require it.
- **Owns its own storage.** The integration will maintain its own SQLite
  database plus a plain-text, append-only audit log. It will not write to
  Home Assistant's recorder database.
- **UI-configured only.** Setup and configuration through config flow and
  config subentries. No YAML.
- **NFC tags bound by scanning, not by ID.** Tags are associated with a pet
  or regimen by scanning them during setup. No hardcoded tag IDs, no
  user-authored automations required to make a scan mean something.
- **Data export ships early, not eventually.** Your animal's medical history
  is yours. An export service is planned as a first-class, early feature so
  the record is never trapped in the integration's database.

## Requirements

Home Assistant **2025.3.0** or later. That's the release that added the
config subentry APIs (`ConfigSubentry`, `async_get_supported_subentry_types`);
this integration is designed around one config entry per animal with regimens
as subentries of it, so the minimum version is pinned to when that API
actually landed rather than guessed.

## License

MIT.
