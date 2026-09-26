import random
from pacing import order_compilation, pick_livestream, _similar, LONG_SECONDS

rng = random.Random(1)
clips = [{"id": f"c{i}", "views": rng.randint(200_000, 30_000_000),
          "duration": rng.choice([35, 55, 70, 100, 130]),
          "title": rng.choice(["He Hurt My Dog", "The Cashier Slid Me A Note", "My Wife Called This Boy",
                               "Someone Stole My Cop Car", "What Was In My Coffee"]) + f" #{i}"}
         for i in range(300)]

live = pick_livestream(clips, min_views=2_000_000, count=250, exclude_ids=["c1", "c2"], seed=7)
assert all(c["views"] >= 2_000_000 for c in live) and len({c["id"] for c in live}) == len(live)
print("livestream:", len(live), "clips, all >= 2M views, no duplicates")

pinned = ["c10", "c20", "c30"]
order = order_compilation(live, pinned_ids=[p for p in pinned if p in {c['id'] for c in live}], seed=3)
assert len(order) == len(live) and len({c["id"] for c in order}) == len(order), "lost or duplicated clips"
top = sorted(live, key=lambda c: c["views"], reverse=True)
cut = top[round(len(live) * 0.2) - 1]["views"]
gaps, last = [], None
for i, c in enumerate(order):
    if c["views"] >= cut:
        if last is not None: gaps.append(i - last)
        last = i
long_pairs = sum(1 for a, b in zip(order, order[1:]) if a["duration"] > LONG_SECONDS and b["duration"] > LONG_SECONDS)
sim_pairs = sum(1 for a, b in zip(order, order[1:]) if _similar(a, b))
print(f"big hit every ~{sum(gaps) / len(gaps):.1f} clips (max gap {max(gaps)}), "
      f"long-long pairs: {long_pairs}, similar-title pairs: {sim_pairs}")
print("closer is a big hit:", order[-1]["views"] >= cut)

monthly = order_compilation(clips[:40], pinned_ids=["c5", "c9", "c2"], seed=1)
assert [c["id"] for c in monthly[:3]] == ["c5", "c9", "c2"]
print("monthly: pinned Top 3 stay first:", [c["id"] for c in monthly[:3]])
print("opener (no pins):", [f"{c['views'] / 1e6:.1f}M" for c in order_compilation(clips[:40], seed=1)[:2]],
      "| biggest in set:", [f"{c['views'] / 1e6:.1f}M" for c in sorted(clips[:40], key=lambda c: -c['views'])[:2]])
