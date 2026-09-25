# StoryStack roadmap

Every idea Steven has raised, so nothing gets lost. Status: DONE / NEXT / PLANNED / IDEA.

## Done
- DONE 1080x1920 downloads (sticky DataImpulse port 10000; the format fix)
- DONE Original-language audio, never YouTube auto-dubs
- DONE YouTube API key: date, length and views fill in within seconds
- DONE Fill progress shows rate, ETA and elapsed time
- DONE Daily auto-sync 05:52 UTC: new videos, missing details, view refresh for every clip
- DONE Download retries on new sticky IPs, plus a backup downloader
- DONE Skip reasons saved per clip (the dashboard display still needs checking)

## Next
- NEXT Put the StoryStack code on GitHub: push_code_to_github.sh is READY (scans for secrets first)
- READY Overnight pre-downloader (prefetch.py + install_prefetch.sh): parallel, a sticky IP per worker, highest views first,
  nightly GB budget, disk guard, status file. Dashboard page for it: PLANNED once the code is on GitHub

## Builds and reliability
- PLANNED Skipped clip: retry it again at the end of the download stage; if it still fails, swap in
  another clip that matches the same rules, so a 250-clip build still has 250 clips
- PLANNED Resume a crashed build from where it stopped (finished parts are already kept)
- PLANNED Live build view: Story 1, 2, 3... with thumbnails and a status per clip
  (queued / downloading / encoding / done / skipped / replaced)
- PLANNED Encode on all 8 cores (several clips at once) plus a faster ffmpeg preset after a quality check
- PLANNED Show skip reasons in the build dialog (verify the app.js patch; pass the reason from the API if it's missing)

## Selecting clips
- PLANNED From/to date-range picker, alongside the existing month filter
- PLANNED View filters: minimum and maximum views (e.g. over 1M, 1.5M, 2M)
- PLANNED Mass-select: "select everything that matches the current filters"
- PLANNED Favourites / pins: hand-picked clips placed first (Top 10 / Top 25), then the rest randomised
- READY Ordering logic in pacing.py (tested): pinned first, strong opener, hits spread evenly, no long/similar
  back to back, big closer; pick_livestream() with a no-recent-repeats rule. Needs wiring into the UI.
- PLANNED Randomiser with pacing: a strong opener, a big hit (e.g. 10M+) every N clips, never two long
  clips or similar titles back to back, a strong closer. recipes.spread_order(anchor_every=5) already exists, so build on it
- PLANNED Livestream recipe: 250 random clips above X views; don't repeat clips used in recent streams

## Multi-channel and storage
- PLANNED All His Story / Her Story language channels in one dashboard
- PLANNED Separate VPS for StoryStack, so encoding doesn't compete with livestreams for CPU
- PLANNED Storage: capped local cache (least-recently-used clips removed first) and/or cheap object storage
  (Cloudflare R2 / Backblaze B2); keep only clips that match recipes, or all of them if storage is cheap
- IDEA Per-channel download budget / DataImpulse cost tracker (MB used per build)

## Access and hosting
- READY setup_domain.sh (Caddy, HTTPS, noindex). Needs the DNS A record first.
- PLANNED Own subdomain with HTTPS (e.g. storystack.plotpointedashboard.com), login required,
  noindex plus robots.txt so it never shows up on Google
- IDEA Link it from the PlotPointe dashboard, or have the dev team build it in
- IDEA Age-restricted clips: YouTube cookies from a spare account, used only for those clips (1 of 1656 today)

## Questions to check
- Channel shows 1,659 videos but the library has fewer. Check the next sync and whether long videos,
  private/scheduled or members-only videos account for the difference.

## Ideas from Claude
- IDEA Saved presets: "Monthly Top 25", "Livestream 2M+ x250", one click each
- IDEA "Used in" history per clip, so livestreams and monthlies avoid recent repeats automatically
- IDEA Storage and cost panel: GB cached, GB free, DataImpulse spend this month
- IDEA Chapters already exist (the Chapters button); add clickable chapter text for the YouTube description
- IDEA Auto-pick "your favourites that underperformed": high watch-time but lower views (needs YouTube Analytics access)
