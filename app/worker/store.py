"""Worker store compatibility layer for EventStore."""

import sqlite3
import app.events.store

# Ensure monkeypatching app.worker.store.sqlite3 modifies app.events.store.sqlite3
app.events.store.sqlite3 = sqlite3

from app.events.store import (
    EventRecord as Job,
    EventStore as JobStore,
    SCHEMA_VERSION,
    iso,
    sanitize_payload,
    utc_now,
)

__all__ = ["JobStore", "Job", "SCHEMA_VERSION", "utc_now", "iso", "sanitize_payload", "sqlite3"]
