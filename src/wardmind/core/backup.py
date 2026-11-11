"""Backups and exports for Settings > Backup & export.

CONTRACT FILE (the API below). A backup is one file holding the open book, sealed with a
password the person chooses: restoring it always makes a new book and never overwrites one.
Exports are CSV files of the scored book and of notes and follow-ups, made safe to open in a
spreadsheet (no cell can start a formula).
"""

from __future__ import annotations

from datetime import datetime

from wardmind.core import audit


def last_backup_at(book_id: str) -> datetime | None:
    """When a backup of this book was last made (from the activity log), or None."""
    entry = audit.last(audit.Event.BACKUP_MADE, book=book_id)
    return entry.at if entry is not None else None
