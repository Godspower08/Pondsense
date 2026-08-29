"""
PondSense - single-shot email check, for GitHub Actions.

run_forever() in email_reply_handler.py loops with time.sleep() -
that's fine for a terminal you keep open, but a scheduled CI job runs
once and exits. This calls the same underlying function
(check_and_reply_to_unread) that run_forever() calls internally, just
once, then lets the job finish. The workflow's cron schedule is what
provides the "every N minutes" behavior instead of an in-process loop.
"""

from email_reply_handler import check_and_reply_to_unread

if __name__ == "__main__":
    count = check_and_reply_to_unread(dry_run=False)
    print(f"Processed {count} message(s).")
