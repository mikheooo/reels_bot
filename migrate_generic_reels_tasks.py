#!/usr/bin/env python3
"""
Migration script for historical generic Reels tasks:
- Replaces generic "[Идея из видео (Reels)]" titles in BACKLOG.md with informative titles.
- Updates H1 in corresponding ~/.hermes/plans/idea_*.md files.
- Synchronizes / upserts corresponding Task records in PostgreSQL DB.

Safety Features:
- Idempotent: Can be run multiple times safely.
- Reversible: Creates timestamped backups of BACKLOG.md and idea_*.md files before modifying.
- Preserves all metadata, checkbox status ([ ], [~], [x]), and source URLs.
- Supports --dry-run to inspect planned changes without applying them.
"""

import os
import sys
import re
import uuid
import shutil
import argparse
import asyncio
from datetime import datetime
from pathlib import Path

# Add reels_bot root to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from app.worker.tasks import extract_tasks_from_analysis, clean_title_str, is_valid_title
from app.db.models import Task, Job
from app.core.config import settings
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select

PLANS_DIR = Path(os.path.expanduser("~/.hermes/plans"))
BACKLOG_PATH = PLANS_DIR / "BACKLOG.md"

def backup_file(file_path: Path, backup_dir: Path):
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    bak_name = f"{file_path.name}.bak_{timestamp}"
    dest = backup_dir / bak_name
    shutil.copy2(file_path, dest)
    return dest

def plan_migration(plans_dir: Path = PLANS_DIR, backlog_path: Path = BACKLOG_PATH):
    if not backlog_path.exists():
        print(f"Error: Backlog file not found at {backlog_path}")
        return []

    with open(backlog_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    migrations = []
    
    for idx, line in enumerate(lines):
        # Match lines with generic Reels idea
        if "Идея из видео (Reels)" in line or "Идея из видео" in line:
            # Check for pattern - [status] [title](filename) - url
            m = re.search(r"^(-\s*\[([ xX~!\?])\]\s*)\[(.*?)\]\((idea_[^\)]+\.md)\)(?:\s*-\s*(https?://\S+))?", line)
            if m:
                prefix = m.group(1)
                status_char = m.group(2)
                old_title = m.group(3).strip()
                filename = m.group(4).strip()
                url = m.group(5) or ""

                idea_file = plans_dir / filename
                if not idea_file.exists():
                    print(f"Warning: {filename} does not exist in {plans_dir}, skipping.")
                    continue

                with open(idea_file, "r", encoding="utf-8", errors="ignore") as inf:
                    idea_content = inf.read()

                extracted_tasks = extract_tasks_from_analysis(idea_content, url=url)
                if not extracted_tasks:
                    print(f"Warning: Could not extract tasks from {filename}, skipping.")
                    continue

                primary_new_title = extracted_tasks[0]["title"]
                
                # Extract job_id from filename e.g. idea_<job_id>.md
                job_id_match = re.search(r"idea_([a-f0-9\-]+)\.md", filename)
                job_id = job_id_match.group(1) if job_id_match else None

                migrations.append({
                    "line_index": idx,
                    "filename": filename,
                    "idea_file": idea_file,
                    "job_id": job_id,
                    "old_title": old_title,
                    "new_title": primary_new_title,
                    "all_tasks": extracted_tasks,
                    "status_char": status_char,
                    "url": url,
                    "old_line": line,
                })

    return migrations

async def apply_migration(migrations, dry_run: bool = True):
    if not migrations:
        print("No generic Reels tasks found for migration.")
        return

    print(f"\nFound {len(migrations)} tasks to migrate:")
    print("=" * 80)
    for m in migrations:
        print(f"File: {m['filename']}")
        print(f"  Old Title: {m['old_title']}")
        print(f"  New Title: {m['new_title']}")
        if len(m['all_tasks']) > 1:
            print(f"  (Extracted {len(m['all_tasks'])} sub-tasks)")
        print(f"  URL:       {m['url']}")
        print("-" * 80)

    if dry_run:
        print("\n[DRY RUN] No files or database records were modified.")
        print("Run with --apply to execute the migration.")
        return

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_dir = PLANS_DIR / f"backups_migration_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Backup and update BACKLOG.md
    bak_backlog = backup_file(BACKLOG_PATH, backup_dir)
    print(f"\nBacked up BACKLOG.md -> {bak_backlog}")

    with open(BACKLOG_PATH, "r", encoding="utf-8") as f:
        backlog_lines = f.readlines()

    for m in migrations:
        idx = m["line_index"]
        status_char = m["status_char"]
        new_title = m["new_title"]
        filename = m["filename"]
        url_part = f" - {m['url']}" if m["url"] else ""
        
        # If single task:
        if len(m["all_tasks"]) <= 1:
            backlog_lines[idx] = f"- [{status_char}] [{new_title}]({filename}){url_part}\n"
        else:
            # If multiple independent tasks extracted from single reel
            new_lines = []
            for t in m["all_tasks"]:
                new_lines.append(f"- [{status_char}] [{t['title']}]({filename}){url_part}\n")
            backlog_lines[idx] = "".join(new_lines)

    with open(BACKLOG_PATH, "w", encoding="utf-8") as f:
        f.writelines(backlog_lines)
    print("Updated BACKLOG.md with new titles.")

    # 2. Update H1 in idea_*.md files
    for m in migrations:
        idea_file = m["idea_file"]
        backup_file(idea_file, backup_dir)

        with open(idea_file, "r", encoding="utf-8", errors="ignore") as inf:
            content = inf.read()

        # Replace first line H1
        lines = content.splitlines()
        if lines and lines[0].startswith("# "):
            lines[0] = f"# {m['new_title']}"
            new_content = "\n".join(lines)
            if content.endswith("\n"):
                new_content += "\n"
        else:
            new_content = f"# {m['new_title']}\n\n" + content

        with open(idea_file, "w", encoding="utf-8") as outf:
            outf.write(new_content)

    print(f"Updated H1 headers in {len(migrations)} idea files.")

    # 3. Synchronize DB Tasks in PostgreSQL
    db_url = settings.db_url
    if "postgres:5432" in db_url:
        # local run convenience
        db_url = db_url.replace("postgres:5432", "localhost:5432")

    print(f"Connecting to DB: {db_url}")
    try:
        engine = create_async_engine(db_url, echo=False)
        AsyncSession = async_sessionmaker(engine, expire_on_commit=False)
        
        async with AsyncSession() as session:
            for m in migrations:
                if not m["job_id"]:
                    continue
                job_id = m["job_id"]
                
                # Check if tasks exist for this job_id
                stmt = select(Task).where(Task.job_id == job_id)
                res = await session.execute(stmt)
                existing_tasks = res.scalars().all()

                if existing_tasks:
                    # Update title of existing task(s)
                    for i, t in enumerate(existing_tasks):
                        if i < len(m["all_tasks"]):
                            t.title = m["all_tasks"][i]["title"]
                            if not t.description:
                                t.description = m["all_tasks"][i].get("description")
                else:
                    # Insert task record for this job
                    for t_info in m["all_tasks"]:
                        new_t = Task(
                            id=str(uuid.uuid4()),
                            job_id=job_id,
                            user_id=int(os.getenv("ADMIN_USER_ID", "0")),  # from environment, not hard-coded
                            title=t_info["title"],
                            description=t_info.get("description"),
                            status="PENDING",
                        )
                        session.add(new_t)

            await session.commit()
        print("Synchronized PostgreSQL task records successfully.")
    except Exception as e:
        print(f"Database sync notice (non-fatal): {e}")

    print("\nMigration completed successfully!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate generic Reels tasks to descriptive titles.")
    parser.add_argument("--apply", action="store_true", help="Execute the migration (default is dry-run)")
    args = parser.parse_args()

    migrations = plan_migration()
    asyncio.run(apply_migration(migrations, dry_run=not args.apply))
