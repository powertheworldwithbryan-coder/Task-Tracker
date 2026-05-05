"""
daily_tasks.py — Run this script daily (via Windows Task Scheduler)
It auto-generates the daily to-do list and prints a reminder summary
of any urgent/overdue tasks to the console (also logged to a file).
"""

import sqlite3
import os
import sys
from datetime import date, datetime

DB_PATH  = os.path.join(os.path.dirname(__file__), "tasks.db")
LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "daily_run.log")


def get_db():
    if not os.path.exists(DB_PATH):
        print("Database not found. Start the Flask app at least once first.")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_str():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_daily(conn, target_date: str) -> list:
    rows = conn.execute(
        """SELECT * FROM tasks
           WHERE status NOT IN ('Done', 'Cancelled')
             AND (
                 priority IN ('Critical', 'High')
                 OR (deadline != '' AND deadline IS NOT NULL AND deadline <= ?)
             )""",
        (target_date,),
    ).fetchall()

    existing_ids = {
        r["task_id"]
        for r in conn.execute(
            "SELECT task_id FROM daily_todos WHERE daily_date = ?", (target_date,)
        ).fetchall()
    }

    added = []
    now = now_str()
    for task in rows:
        if task["id"] in existing_ids:
            continue
        conn.execute(
            """INSERT INTO daily_todos (daily_date, task_id, title, status, pending_on, deadline, priority, remarks, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                target_date,
                task["id"],
                task["title"],
                task["status"],
                task["pending_on"],
                task["deadline"],
                task["priority"],
                task["remarks"],
                now,
            ),
        )
        added.append(dict(task))
    conn.commit()
    return added


def get_reminders(conn, today: str) -> list:
    rows = conn.execute(
        """SELECT * FROM tasks
           WHERE priority IN ('Critical', 'High')
             AND status NOT IN ('Done', 'Cancelled')
             AND (deadline <= ? OR deadline = '' OR deadline IS NULL)
           ORDER BY CASE priority WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 ELSE 3 END, deadline ASC""",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def log(message: str, log_lines: list):
    print(message)
    log_lines.append(message)


def main():
    today = date.today().isoformat()
    log_lines = []

    log(f"=== Task Tracker Daily Run: {today} ===", log_lines)

    with get_db() as conn:
        added = generate_daily(conn, today)
        log(f"\n[Daily To-Do] Generated {len(added)} new entries for {today}:", log_lines)
        for t in added:
            log(f"  [{t['priority']}] {t['title']} (due: {t['deadline'] or 'no deadline'})", log_lines)
        if not added:
            log("  (All applicable tasks already in today's list.)", log_lines)

        reminders = get_reminders(conn, today)
        log(f"\n[Reminders] {len(reminders)} urgent tasks pending:", log_lines)
        for t in reminders:
            overdue_tag = " ⚠ OVERDUE" if (t["deadline"] and t["deadline"] < today) else (" 📌 DUE TODAY" if t["deadline"] == today else "")
            log(f"  [{t['priority']}] {t['title']} | By: {t['pending_on'] or 'N/A'} | Deadline: {t['deadline'] or 'N/A'}{overdue_tag}", log_lines)

    # Write log
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n\n")

    print(f"\nLog saved → {LOG_PATH}")


if __name__ == "__main__":
    main()
