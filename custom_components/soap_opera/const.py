"""Constants for the SOAP Opera integration."""

DOMAIN = "soap_opera"

CONF_SPECIES = "species"
CONF_BREED = "breed"
CONF_DATE_OF_BIRTH = "date_of_birth"

# Idempotency time bucket for the log_dose funnel, in seconds. Captures with
# the same source and source reference whose administered_at falls in the
# same bucket are treated as one action (a double-tapped button, a
# re-delivered automation). Too wide, and two deliberate doses close together
# collapse into one; too narrow, and a slow retry gets through as a second
# dose. The min-interval guardrail backs this up either way. Tuning knob.
IDEMPOTENCY_BUCKET_SECONDS = 120

SERVICE_LOG_DOSE = "log_dose"
SERVICE_EXPORT = "export"

# Fired on the bus for every newly recorded administration (not for
# duplicates that collapsed into an existing one).
EVENT_DOSE_LOGGED = "soap_opera_dose_logged"

# Dispatcher signal, per regimen subentry, that drives its event entity.
SIGNAL_DOSE_LOGGED = "soap_opera_dose_logged_{}"
