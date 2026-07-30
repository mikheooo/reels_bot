# Graph Report - C:\Users\Misha\reels_bot  (2026-07-28)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 75 nodes · 98 edges · 24 communities
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 3 edges (avg confidence: 0.6)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `48b5c3d9`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- tasks.py
- handlers.py
- settings.py
- send_long_text
- task_cycle

## God Nodes (most connected - your core abstractions)
1. `process_video()` - 12 edges
2. `handle_url()` - 6 edges
3. `init_db()` - 5 edges
4. `Job` - 5 edges
5. `Task` - 5 edges
6. `send_long_text()` - 5 edges
7. `_try_cobalt()` - 4 edges
8. `download_video()` - 4 edges
9. `get_video_dimensions()` - 4 edges
10. `downscale_video()` - 4 edges

## Surprising Connections (you probably didn't know these)
- `startup()` --calls--> `init_db()`  [EXTRACTED]
  app/worker/settings.py → app/db/database.py
- `process_video()` --calls--> `Task`  [EXTRACTED]
  app/worker/tasks.py → app/db/models.py
- `handle_url()` --calls--> `clean_url()`  [EXTRACTED]
  app/bot/handlers.py → app/core/normalizer.py
- `handle_url()` --calls--> `is_valid_url()`  [EXTRACTED]
  app/bot/handlers.py → app/core/normalizer.py
- `handle_url()` --calls--> `Job`  [EXTRACTED]
  app/bot/handlers.py → app/db/models.py

## Import Cycles
- None detected.

## Communities (24 total, 0 thin omitted)

### Community 0 - "tasks.py"
Cohesion: 0.19
Nodes (15): analyze_video(), download_video(), downscale_video(), _extract_summary(), _extract_task(), get_video_dimensions(), process_video(), Get video width/height via ffprobe. Returns (0, 0) on failure. (+7 more)

### Community 1 - "handlers.py"
Cohesion: 0.26
Nodes (12): cmd_start(), cmd_tasks(), cmd_tasks_done(), cmd_tasks_pending(), get_redis_pool(), handle_url(), clean_url(), is_valid_url() (+4 more)

### Community 2 - "settings.py"
Cohesion: 0.23
Nodes (8): main(), Config, Settings, init_db(), startup(), WorkerSettings, BaseSettings, Bot

### Community 3 - "send_long_text"
Cohesion: 0.50
Nodes (4): Split long text into chunks under Telegram's message limit., Send text that may exceed Telegram's 4096 char limit., send_long_text(), _split_text()

### Community 4 - "task_cycle"
Cohesion: 0.67
Nodes (3): task_cycle(), callback_query, CallbackQuery

## Knowledge Gaps
- **2 isolated node(s):** `Config`, `WorkerSettings`
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `process_video()` connect `tasks.py` to `handlers.py`, `settings.py`, `send_long_text`?**
  _High betweenness centrality (0.058) - this node is a cross-community bridge._
- **Why does `Job` connect `handlers.py` to `tasks.py`?**
  _High betweenness centrality (0.043) - this node is a cross-community bridge._
- **Why does `Task` connect `handlers.py` to `tasks.py`?**
  _High betweenness centrality (0.043) - this node is a cross-community bridge._
- **What connects `Config`, `WorkerSettings` to the rest of the system?**
  _2 weakly-connected nodes found - possible documentation gaps or missing edges._