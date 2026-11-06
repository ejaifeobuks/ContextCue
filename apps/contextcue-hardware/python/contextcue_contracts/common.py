"""Rules common to every ContextCue contract (software baseline section 5)."""

from enum import Enum

SCHEMA_VERSION = "1.0"


class Quality(str, Enum):
    VALID = "valid"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
