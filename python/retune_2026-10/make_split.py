"""Grouped, stratified train/test split of the combined corpus (seed fixed).

* TEST comes ONLY from the new 2026 sweep (the old 314-file corpus was already used to tune
  revisions 0.10-0.15, so it is not a clean held-out set; all of it goes to TRAIN).
* Grouping key = patient_id, else user_id (1,965/2,735 new files have no patient_id), so no person
  appears on both sides.
* Stratified by bpm bin: <40 pooled as one bin (matches the merged template), then 5-bpm bins,
  >=195 pooled. ~25% of each bin's FILES go to test (greedy, rarest-bin-first).
"""
import csv, random, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
C = r"C:\Users\daniel\Desktop\ExpandedCorpus"
SEED = 20261012
TEST_FRAC = 0.25

man = list(csv.DictReader(open(C + r"\combined_20261012\manifest.csv", encoding="utf-8")))
sw = {r["output_filename"]: r for r in csv.DictReader(open(C + r"\sq5_sweep_core500_20261008\manifest.csv", encoding="utf-8"))}


def sbin(b):
    b = float(b)
    return "30-40" if b < 40 else ("195+" if b >= 195 else f"{int(b // 5 * 5)}-{int(b // 5 * 5) + 5}")


def group(r):
    s = sw.get(r["file"])
    if s is None:
        return "old:" + r["file"]
    pid = s["patient_id"]
    return "p" + pid if pid not in ("", "nan") else "u" + s["user_id"]


rows = [dict(file=r["file"], source=r["source"], category=r["category"], bpm=float(r["bpm_label"]),
             bin=sbin(r["bpm_label"]), group=group(r)) for r in man]
new = [r for r in rows if r["source"] == "new_sq5_sweep"]
binN = collections.Counter(r["bin"] for r in new)
target = {b: max(1, round(TEST_FRAC * n)) for b, n in binN.items()}
groups = collections.defaultdict(list)
for r in new:
    groups[r["group"]].append(r)
rng = random.Random(SEED)
order = list(groups)
rng.shuffle(order)
# rarest-bin-first so thin bins get their share before common bins fill up
order.sort(key=lambda g: min(binN[r["bin"]] for r in groups[g]))
test_cnt = collections.Counter()
test_groups = set()
for g in order:
    bins = collections.Counter(r["bin"] for r in groups[g])
    need = [b for b in bins if test_cnt[b] + bins[b] <= target[b] * 1.15 and test_cnt[b] < target[b]]
    if need and all(test_cnt[b] + bins[b] <= target[b] * 1.5 for b in bins):
        test_groups.add(g)
        for b, n in bins.items():
            test_cnt[b] += n
for r in rows:
    r["split"] = "test" if r["group"] in test_groups else "train"
with open(os.path.join(HERE, "split.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["file", "source", "category", "bpm", "bin", "group", "split"])
    w.writeheader(); w.writerows(rows)
tr = collections.Counter(r["bin"] for r in rows if r["split"] == "train")
te = collections.Counter(r["bin"] for r in rows if r["split"] == "test")
trg = {r["group"] for r in rows if r["split"] == "train" and r["source"] != "old_corpus"}
assert not (trg & test_groups), "group leakage"
print(f"groups test={len(test_groups)}  files train={sum(tr.values())} test={sum(te.values())}")
print("bin        train  test")
keys = sorted(binN, key=lambda s: float(s.split('-')[0].rstrip('+')))
for b in keys:
    print(f"{b:>8} {tr[b]:>6} {te[b]:>5}   (new-sweep total {binN[b]})")
