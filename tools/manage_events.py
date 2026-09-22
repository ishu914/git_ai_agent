"""
tools/manage_events.py - EventStore maintenance CLI
====================================================

Manage the durable SQLite event store for the GitLab AI Agent.
This tool operates directly against the EventStore; no worker process is needed.

Usage (run from the repository root or /opt/git-ai-reviewer/application):

    python -m tools.manage_events --status
    python -m tools.manage_events --list-dead-letter
    python -m tools.manage_events --retry-job <JOB_ID>
    python -m tools.manage_events --cleanup-retention
    python -m tools.manage_events --backup /var/backups/git-ai-agent/events-$(date +%F).sqlite3
"""

import argparse
import sys

from app.config import get_settings
from app.events.store import EventStore


def _print_event(event) -> None:
    print(
        f"{event.event_id}  project={event.project_id}  mr=!{event.mr_iid}"
        f"  status={event.status}  attempts={event.attempt_count}"
        f"  created={event.created_at}  error={event.last_error or '-'}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GitLab AI Agent - EventStore maintenance CLI (no worker required)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--status", action="store_true", help="Show event queue status counts")
    parser.add_argument("--list-dead-letter", action="store_true", help="List dead-letter events")
    parser.add_argument("--retry-job", metavar="JOB_ID", help="Requeue one dead-letter event by ID")
    parser.add_argument(
        "--cleanup-retention",
        action="store_true",
        help="Purge completed/failed events older than EVENT_RETENTION_DAYS",
    )
    parser.add_argument(
        "--backup",
        metavar="PATH",
        help="Write a consistent hot backup of the SQLite database to PATH",
    )
    args = parser.parse_args()

    if not any(vars(args).values()):
        parser.print_help()
        sys.exit(0)

    settings = get_settings()
    store = EventStore(settings.database_path)

    if args.status:
        counts = store.status_counts()
        if not counts:
            print("No events recorded.")
            return
        for status, count in sorted(counts.items()):
            print(f"{status:20s} {count}")
        return

    if args.list_dead_letter:
        events = store.list_dead_letter()
        if not events:
            print("No dead-letter events.")
            return
        for event in events:
            _print_event(event)
        return

    if args.retry_job:
        try:
            event = store.requeue_dead_letter(args.retry_job)
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))
            return
        print(f"Requeued event {event.event_id}")
        return

    if args.cleanup_retention:
        removed = store.cleanup_retention(settings.event_retention_days)
        print(f"Removed {removed} historical events (older than {settings.event_retention_days} days).")
        return

    if args.backup:
        store.backup(args.backup)
        print(f"Backup written to: {args.backup}")
        return


if __name__ == "__main__":
    main()
