import sys, time, sqlite3
sys.path.insert(0, "/opt/storystack/server")
import db, jobs, providers
db.init("/var/lib/storystack/storystack.db")
t0 = time.time()
DB = sqlite3.connect("/var/lib/storystack/storystack.db")
channels = [r[0] for r in DB.execute("SELECT id FROM channels")]
def wait_for_fill(cid, limit=1800):
    end = time.time() + limit
    while cid in getattr(jobs, "_hydrating", ()) and time.time() < end:
        time.sleep(2)
for cid in channels:
    before = len(jobs.needs_detail(cid))
    print(f"[{cid}] syncing channel", flush=True)
    jobs.sync_channel(cid); wait_for_fill(cid)
    print(f"[{cid}] filling details ({len(jobs.needs_detail(cid))} missing, was {before})", flush=True)
    jobs.hydrate_channel(cid); wait_for_fill(cid)
key = providers.api_key()
if key:
    ids = [r[0] for r in DB.execute("SELECT id FROM videos")]
    for i in range(0, len(ids), 50):
        jobs._apply_details(providers.fetch_details_api(ids[i:i + 50], key))
    print(f"refreshed views for {len(ids)} videos", flush=True)
else:
    print("no API key: skipped view refresh", flush=True)
print(f"done in {time.time() - t0:.0f}s", flush=True)
