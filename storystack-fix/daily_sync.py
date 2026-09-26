#!/usr/bin/env python3
"""Daily StoryStack refresh, run by storystack-sync.timer.

1. Sync every channel (pulls in newly posted videos).
2. Fill in date/length/views for anything still missing.
3. Refresh view counts for the whole library (YouTube API, 50 per call).
"""
import sys, time
sys.path.insert(0, "/opt/storystack/server")
import db, jobs, providers

db.init("/var/lib/storystack/storystack.db")
t0 = time.time()
import sqlite3
DB = sqlite3.connect("/var/lib/storystack/storystack.db")
channels = [r[0] for r in DB.execute("SELECT id FROM channels")]

def wait_for_fill(cid, limit=1800):
    # sync may start the fill in a background thread; don't exit before it ends
    end = time.time() + limit
    while cid in getattr(jobs, "_hydrating", ()) and time.time() < end:
        time.sleep(2)

for cid in channels:
    before = len(jobs.needs_detail(cid))
    print(f"[{cid}] syncing channel", flush=True)
    jobs.sync_channel(cid)
    wait_for_fill(cid)
    print(f"[{cid}] filling details ({len(jobs.needs_detail(cid))} missing, was {before})", flush=True)
    jobs.hydrate_channel(cid)
    wait_for_fill(cid)

key = providers.api_key()
if key:
    ids = [r[0] for r in DB.execute("SELECT id FROM videos")]
    for i in range(0, len(ids), 50):
        jobs._apply_details(providers.fetch_details_api(ids[i:i + 50], key))
    print(f"refreshed views for {len(ids)} videos", flush=True)
else:
    print("no API key: skipped view refresh", flush=True)
print(f"done in {time.time() - t0:.0f}s", flush=True)
