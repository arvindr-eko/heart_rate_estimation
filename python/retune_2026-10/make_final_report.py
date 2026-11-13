"""Final report: v0.15 vs v0.15+new templates vs v0.16 on the held-out TEST split (and train for reference)."""
import os, io, base64, pickle, csv, collections, re, html
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import evalkit as E

R = os.path.dirname(os.path.abspath(__file__))
out = pickle.load(open(os.path.join(R, "final_results.pkl"), "rb"))
rows = E.load_split()
L15, LT, L16 = "v0.15 (as shipped)", "v0.15 + new templates", "v0.16 (templates + slow ratio 1.35)"
LABS = [L15, LT, L16]
COL = {L15: "#888888", LT: "#1f77b4", L16: "#d62728"}
BANDS = [("30-40", 30, 40), ("40-60", 40, 60), ("60-100", 60, 100), ("100-160", 100, 160), ("160-200", 160, 300)]


def img(fig):
    b = io.BytesIO(); fig.savefig(b, format="png", dpi=100, bbox_inches="tight"); plt.close(fig)
    return f"<img src='data:image/png;base64,{base64.b64encode(b.getvalue()).decode()}'>"


def sub(split, lo=0, hi=999, src=None):
    return [r for r in rows if r["split"] == split and lo <= float(r["bpm"]) < hi and (src is None or r["source"] == src)]


def stats(label, split, rs):
    res = out[(label, split)]
    o, S = E.summarize(res, rs)
    # macro over groups (each person/user counts once): mean of per-group means
    g = collections.defaultdict(list)
    for r, s in zip([r for r in rs if r["file"] in res], S):
        n = s["n"]; c = s["cls"]
        g[r["group"]].append((sum(x == "correct" for x in c) / n, sum(x not in ("correct", "no_display") for x in c) / n,
                              s["ttfr"], s["ttfd"], s["first_disp_correct"], s["settled"]))
    mac = dict(groups=len(g), correct=np.mean([np.mean([x[0] for x in v]) for v in g.values()]),
               wrong=np.mean([np.mean([x[1] for x in v]) for v in g.values()]))
    tt = np.array([s["ttfr"] for s in S if s["ttfr"] is not None])
    o["ttfr_p75"] = float(np.percentile(tt, 75)) if len(tt) else None
    o["ttfr_min"] = float(tt.min()) if len(tt) else None
    o["within10"] = None
    return o, S, mac


def wilson(k, n, z=1.96):
    if n == 0: return (0, 0)
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); a = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0, (c - a) / d), min(1, (c + a) / d))


def boot_diff(la, lb, split, rs, n=2000, seed=1):
    """paired bootstrap over GROUPS of (b - a) in per-file correct-tick fraction and wrong fraction."""
    A, B = out[(la, split)], out[(lb, split)]
    per = collections.defaultdict(lambda: [[], []])
    for r in rs:
        gt = float(r["bpm"])
        for k, res in ((0, A), (1, B)):
            c = [E.classify(None if np.isnan(x) else x, gt) for x in res[r["file"]][1]]
            per[r["group"]][k].append((np.mean([x == "correct" for x in c]), np.mean([x not in ("correct", "no_display") for x in c])))
    gl = list(per)
    d = {g: (np.mean([x[0] for x in per[g][1]]) - np.mean([x[0] for x in per[g][0]]),
             np.mean([x[1] for x in per[g][1]]) - np.mean([x[1] for x in per[g][0]])) for g in gl}
    rng = np.random.default_rng(seed); bs = []
    for _ in range(n):
        pick = rng.choice(len(gl), len(gl)); bs.append(np.mean([d[gl[i]] for i in pick], axis=0))
    bs = np.array(bs)
    return np.mean([d[g] for g in gl], axis=0), np.percentile(bs, [2.5, 97.5], axis=0)


def pct(x, d=1): return "" if x is None else f"{x*100:.{d}f}%"
def sec(x): return "&ndash;" if x is None else f"{x:.1f}s"


H = ["<html><head><meta charset='utf-8'><title>combo13 retune report</title><style>body{font-family:Segoe UI,Arial;margin:24px;max-width:1250px}"
     "table{border-collapse:collapse;font-size:12px;margin:8px 0}td,th{border:1px solid #bbb;padding:3px 7px;text-align:right}th{background:#eee}"
     "td:first-child,th:first-child{text-align:left}h2{margin-top:34px;border-bottom:2px solid #ddd}.b{background:#e8f4e8}.w{background:#fdecea}.n{color:#555;font-size:12px}"
     "img{max-width:100%}</style></head><body>"]
H.append("<h1>Combo13 re-tune: 30&ndash;200 bpm &mdash; what changed and held-out test results</h1>")
H.append("<p class='n'>Generated 2026-10-12. Versions compared on the same files and the same harness: <b>v0.15</b> as shipped; <b>v0.15 + new templates</b> "
         "(isolates the template change); <b>v0.16</b> = new templates + <code>SLOW_RATIO_MIN</code> 1.5&rarr;1.35. Displayed-bpm convention (what the user would see "
         "after the combine / confirmation / settling pipeline), ticks every 0.5 s. &ldquo;Correct&rdquo; = within &plusmn;5 bpm or &plusmn;5% of ground truth (project-wide convention).</p>")

# ---------------- 1. what changed
tmpl = np.load(os.path.join(R, "new_templates_train.npz"), allow_pickle=True)
H.append("<h2>1. What changed</h2><ol>"
         "<li><b>Data.</b> Combined corpus = 2,735 new 2026 sweep files + 314 old files (96 synthetic removed) = 3,049 files at 30&ndash;200 bpm "
         "(old corpus: 314 files, none below 44.6 bpm).</li>"
         "<li><b>Split.</b> Grouped by person (patient_id, else user_id), stratified by bpm bin, seed fixed. <b>Test = 836 files from the new sweep only</b> "
         "(267 groups, no person on both sides); train = 2,213 files (all old-corpus files + the rest of the new sweep). The old corpus is all train because it "
         "was already used to tune 0.10&ndash;0.15. See <code>split.csv</code>.</li>"
         f"<li><b>Templates.</b> Rebuilt from <b>train files only</b>: {len(tmpl['buckets'])} buckets (was 28), raw + detrended, 80-bin shipped format. "
         "Every 5-bpm bucket from 40 to 200 now exists (new: 115, 155, 195, 200 and the slow end). <b>One merged 30&ndash;40 bpm bucket</b> (nominal 35; 32 train files) replaces "
         "separate 30/35 buckets, per your direction; the 40 bucket covers labels 40&ndash;42.5. Buckets &lt;45 bpm and 200 are built from all valid ticks of files at that rate "
         "(the algorithm rarely reports those rates correctly, so the old 'correct-candidate ticks' rule starves them). Overlays: <code>template_report.html</code> "
         "(built from all data; the shipped headers use the train-only version).</li>"
         "<li><b>Parameter.</b> <code>SLOW_RATIO_MIN</code> 1.5 &rarr; 1.35 (the slow-zone peak must be &ge;1.35&times; the tallest fast-zone peak to override it).</li>"
         "<li>Version string 0.15 &rarr; 0.16. Source: <code>Desktop\\pcg_abab\\v0.16\\</code>. <b>Not yet ported</b> to <code>c13_core_kit</code> / <code>coral-fw</code>, WASM, or the visualizer.</li></ol>")

# sweep summary
H.append("<h3>What was tried (tuning subset of TRAIN only; objective J = mean over bands of correct% &minus; 3&times;wrong% of ticks)</h3>")
H.append("<table><tr><th>Experiment</th><th>Result</th><th>Kept?</th></tr>"
         "<tr><td>New templates (train-only), nothing else</td><td>40&ndash;60 bpm: wrong ticks 2.6%&rarr;1.2%, never-correct files 9.0%&rarr;2.4% (tuning subset); neutral elsewhere</td><td class='b'>yes</td></tr>"
         "<tr><td><code>SLOW_RATIO_MIN</code> 1.0 / 1.2 / 1.35 / 1.5 / 1.75 / 2.0 / 2.5</td><td>30&ndash;40 J: 43 / 40 / 40 / 36 / 33 / 21 / 3; 1.0 costs ~2.5 J at 60&ndash;100; 1.35 costs nothing. Confirmed on all 2,213 train files (30&ndash;40 J 36.1&rarr;40.3, others unchanged)</td><td class='b'>1.35</td></tr>"
         "<tr><td>Template score gate on the slow candidate (min score 0&ndash;0.5; margin vs fast pick 0&ndash;0.3)</td><td>Always worse in 30&ndash;40 (e.g. 31.6 at min 0, &minus;8 at margin 0.3): slow-rate template scores carry little information (leave-one-out template correlation ~0.45)</td><td class='w'>rejected</td></tr>"
         "<tr><td>Two-tier ratio: accept lower ratio when the fast pick's template score is weak</td><td>Best 44.0 vs 44.2 for the plain ratio change; no gain for the added code</td><td class='w'>rejected</td></tr>"
         "<tr><td><code>LAG_POW</code> 0.1&ndash;0.4</td><td>Trades 160+ (+1.9) against 100&ndash;160 and 30&ndash;40 (&minus;1 to &minus;10); not confirmed on full train (J 42.9&rarr;41.8)</td><td class='w'>rejected</td></tr>"
         "<tr><td>Template confidence floor 0.15&ndash;0.4; override margin 0.15&ndash;0.5</td><td>&plusmn;0.3 J, within noise (margin 0.4 cost 160+ on full train)</td><td class='w'>no change</td></tr>"
         "<tr><td><code>TENT_LO_H</code> 0.8&ndash;1.7 (prior at 40 bpm)</td><td>+0.1 J at 1.7 (noise)</td><td class='w'>no change</td></tr>"
         "<tr><td><code>CONS_WAIT_S</code> 4&ndash;8 s</td><td>Shorter wait gains +1.8pt correct ticks but +0.6pt wrong ticks and loses 10 J at 30&ndash;40; longer loses 2.5&ndash;5 J. 6.0 s stays the best trade</td><td class='w'>no change</td></tr>"
         "</table>")
H.append("<p class='n'>Honest summary: apart from the template rebuild and the slow ratio, no single parameter moved the objective by more than the noise of an ~800-file tuning subset. "
         "The existing parameters were already near a local optimum, consistent with the earlier 0.14 re-tune.</p>")

# ---------------- 2. Test results overall
H.append("<h2>2. Held-out TEST results (836 files, 267 groups)</h2>")
rs_all = sub("test")
hdr = ["Metric"] + LABS
M = {l: stats(l, "test", rs_all) for l in LABS}
def row(name, f, cls=""):
    return f"<tr class='{cls}'><td>{name}</td>" + "".join(f"<td>{f(M[l])}</td>" for l in LABS) + "</tr>"
H.append("<table><tr>" + "".join(f"<th>{h}</th>" for h in hdr) + "</tr>")
H.append(row("Files / ticks", lambda m: f"{m[0]['files']} / {m[0]['ticks']}"))
H.append(row("<b>Correct ticks</b> (of all ticks, incl. not-yet-displayed)", lambda m: pct(m[0]["correct"])))
H.append(row("Correct ticks, macro over people (each person equal weight)", lambda m: pct(m[2]["correct"])))
H.append(row("Not displayed ('--')", lambda m: pct(m[0]["no_display"])))
H.append(row("<b>Wrong ticks</b> (displayed but not correct)", lambda m: pct(m[0]["wrong_displayed"])))
H.append(row("Wrong ticks, macro over people", lambda m: pct(m[2]["wrong"])))
H.append(row("Accuracy when a value is displayed", lambda m: pct(m[0]["acc_when_displayed"])))
H.append(row("&nbsp;&nbsp;half-rate / double / third / triple / other (of all ticks)",
             lambda m: f"{m[0]['half']*100:.1f} / {m[0]['double']*100:.1f} / {m[0]['third']*100:.1f} / {m[0]['triple']*100:.1f} / {m[0]['other']*100:.1f} %"))
H.append(row("<b>Time to first display</b> (median, files that display)", lambda m: sec(m[0]["ttfd_median"])))
H.append(row("Files that never display", lambda m: pct(m[0]["never_displayed"])))
H.append(row("First displayed value is correct (files)", lambda m: pct(m[0]["first_display_correct"])))
H.append(row("<b>Time to first accurate HR</b> &mdash; median", lambda m: sec(m[0]["ttfr_median"])))
H.append(row("&nbsp;&nbsp;mean", lambda m: sec(m[0]["ttfr_mean"])))
H.append(row("&nbsp;&nbsp;75th percentile", lambda m: sec(m[0]["ttfr_p75"])))
H.append(row("&nbsp;&nbsp;90th percentile", lambda m: sec(m[0]["ttfr_p90"])))
H.append(row("&nbsp;&nbsp;fastest file", lambda m: sec(m[0]["ttfr_min"])))
H.append(row("Files never reaching an accurate reading", lambda m: pct(m[0]["never_correct"])))
H.append(row("Time to <i>settled</i> accurate HR (4 consecutive correct ticks), median", lambda m: sec(m[0]["settled_median"])))
H.append(row("Files never settling", lambda m: pct(m[0]["never_settled"])))
H.append("</table>")
d, ci = boot_diff(L15, L16, "test", rs_all)
H.append(f"<p>Paired difference v0.16 &minus; v0.15, bootstrap over people (95% CI): correct ticks <b>{d[0]*100:+.2f} pt</b> [{ci[0][0]*100:+.2f}, {ci[1][0]*100:+.2f}]; "
         f"wrong ticks <b>{d[1]*100:+.2f} pt</b> [{ci[0][1]*100:+.2f}, {ci[1][1]*100:+.2f}].</p>")
H.append("<p class='n'>The 60&ndash;100 bpm wrong rate on TEST (12.6%, vs 2.6% on train) is dominated by two users who contribute 37 of its 246 files and "
         "90% of its wrong ticks (user groups u725849: 21 files, u578386: 16). Their median readings are mostly right (ratio 0.9&ndash;1.1); the wrong ticks are near-misses "
         "(e.g. 69 vs 77 bpm) that fall outside &plusmn;5 bpm. v0.15 and v0.16 behave identically there, so it is a property of these recordings, not of the retune. "
         "That is why macro (per-person) rows are shown.</p>")

# ---------------- 3. by band
H.append("<h2>3. Test results by rate band</h2>")
H.append("<table><tr><th>Band (bpm)</th><th>Files</th><th>Version</th><th>Correct ticks</th><th>Wrong ticks</th><th>Not displayed</th><th>Acc. when displayed</th>"
         "<th>TTFD med</th><th>TTFR med</th><th>TTFR p90</th><th>Never accurate</th><th>Never settled</th></tr>")
for bn, lo, hi in BANDS:
    rs = sub("test", lo, hi)
    for i, l in enumerate(LABS):
        o, S, mac = stats(l, "test", rs)
        H.append(f"<tr class='{'b' if l==L16 else ''}'><td>{bn if i==0 else ''}</td><td>{len(rs) if i==0 else ''}</td><td>{l}</td><td>{pct(o['correct'])}</td><td>{pct(o['wrong_displayed'])}</td>"
                 f"<td>{pct(o['no_display'])}</td><td>{pct(o['acc_when_displayed'])}</td><td>{sec(o['ttfd_median'])}</td><td>{sec(o['ttfr_median'])}</td><td>{sec(o['ttfr_p90'])}</td>"
                 f"<td>{pct(o['never_correct'])}</td><td>{pct(o['never_settled'])}</td></tr>")
H.append("</table>")
n30 = len(sub("test", 30, 40)); k_never = {l: round(stats(l, "test", sub("test", 30, 40))[0]["never_correct"] * n30) for l in LABS}
H.append(f"<p>30&ndash;40 bpm test band = {n30} files from {len({r['group'] for r in sub('test',30,40)})} people. Files never reaching an accurate reading: "
         + ", ".join(f"{l}: {k_never[l]}/{n30} (95% CI {wilson(k_never[l], n30)[0]*100:.0f}&ndash;{wilson(k_never[l], n30)[1]*100:.0f}%)" for l in LABS)
         + ". With 17 files these intervals are wide; treat the slow-band numbers as indicative, not confirmed.</p>")

# ---------------- 4. per-5bpm bin
bins = sorted({r["bin"] for r in rows if r["split"] == "test"}, key=lambda s: float(s.split("-")[0].rstrip("+")))
H.append("<h2>4. Test results per 5-bpm bin (v0.15 vs v0.16)</h2><table><tr><th>Bin</th><th>Test files</th><th>Train files</th>"
         "<th>Correct v0.15</th><th>Correct v0.16</th><th>Wrong v0.15</th><th>Wrong v0.16</th><th>TTFR med v0.15</th><th>TTFR med v0.16</th><th>Never accurate v0.15</th><th>Never accurate v0.16</th></tr>")
bx, y15, y16 = [], [], []
for b in bins:
    rt = [r for r in rows if r["split"] == "test" and r["bin"] == b]
    ntr = sum(1 for r in rows if r["split"] == "train" and r["bin"] == b)
    a, _, _ = stats(L15, "test", rt); c, _, _ = stats(L16, "test", rt)
    bx.append(b); y15.append(a["correct"]); y16.append(c["correct"])
    H.append(f"<tr><td>{b}</td><td>{len(rt)}</td><td>{ntr}</td><td>{pct(a['correct'])}</td><td>{pct(c['correct'])}</td><td>{pct(a['wrong_displayed'])}</td><td>{pct(c['wrong_displayed'])}</td>"
             f"<td>{sec(a['ttfr_median'])}</td><td>{sec(c['ttfr_median'])}</td><td>{pct(a['never_correct'])}</td><td>{pct(c['never_correct'])}</td></tr>")
H.append("</table>")

# ---------------- 5. plots
fig, ax = plt.subplots(figsize=(13, 3.4)); x = np.arange(len(bx))
ax.bar(x - .2, [v * 100 for v in y15], .4, color=COL[L15], label="v0.15"); ax.bar(x + .2, [v * 100 for v in y16], .4, color=COL[L16], label="v0.16")
ax.set_xticks(x); ax.set_xticklabels(bx, rotation=70, fontsize=7); ax.set_ylabel("% ticks correct"); ax.legend(); ax.grid(alpha=.25, axis="y"); ax.set_title("Test: share of ticks showing a correct HR, per 5-bpm bin")
H.append("<h2>5. Plots</h2>" + img(fig))

fig, axs = plt.subplots(1, 5, figsize=(15, 2.8), sharey=True)
for ax, (bn, lo, hi) in zip(axs, BANDS):
    for l in (L15, L16):
        rs = sub("test", lo, hi); _, S, _ = stats(l, "test", rs)
        tt = np.sort([s["ttfr"] for s in S if s["ttfr"] is not None]); n = len(S)
        ax.step(np.r_[0, tt], np.r_[0, np.arange(1, len(tt) + 1)] / n * 100, where="post", color=COL[l], label=l[:5])
    ax.set_title(f"{bn} bpm", fontsize=9); ax.set_xlabel("seconds"); ax.grid(alpha=.25); ax.set_xlim(0, 20)
axs[0].set_ylabel("% files with an accurate HR by t"); axs[0].legend(fontsize=7)
H.append("<p>Time to first accurate HR (cumulative share of files):</p>" + img(fig))

fig, axs = plt.subplots(1, 2, figsize=(13, 5))
for ax, l in zip(axs, (L15, L16)):
    res = out[(l, "test")]
    xs, ys = [], []
    for r in rs_all:
        b = res[r["file"]][1]; b = b[~np.isnan(b)]
        if len(b): xs.append(float(r["bpm"])); ys.append(float(np.median(b)))
    ax.scatter(xs, ys, s=7, alpha=.5, color=COL[l])
    ax.plot([25, 205], [25, 205], "k-", lw=.7)
    for k, st in ((2, "--"), (.5, "--"), (3, ":"), (1 / 3, ":")): ax.plot([25, 205], [25 * k, 205 * k], "gray", ls=st, lw=.6)
    ax.set_xlim(25, 205); ax.set_ylim(15, 260); ax.set_xlabel("ground truth bpm"); ax.set_ylabel("median displayed bpm (per file)"); ax.set_title(l, fontsize=9); ax.grid(alpha=.2)
H.append("<p>Per-file median displayed HR vs ground truth (test). Solid = identity; dashed = 2&times; and &frac12;; dotted = 3&times; and &#8531;.</p>" + img(fig))

# ---------------- 6. train / old corpus / real recordings
H.append("<h2>6. Reference: train set, old-corpus regression, real recordings</h2><table><tr><th>Set</th><th>Version</th><th>Files</th><th>Correct ticks</th><th>Wrong ticks</th><th>Not displayed</th><th>TTFR med</th><th>Never accurate</th></tr>")
for nm, rs, split in (("TRAIN (all)", sub("train"), "train"), ("TRAIN old corpus (regular+doubled+self-rec; tuned on in earlier revisions)", sub("train", src="old_corpus"), "train"),
                      ("TRAIN new sweep", sub("train", src="new_sq5_sweep"), "train")):
    for i, l in enumerate(LABS):
        o, _, _ = stats(l, split, rs)
        H.append(f"<tr class='{'b' if l==L16 else ''}'><td>{nm if i==0 else ''}</td><td>{l}</td><td>{o['files']}</td><td>{pct(o['correct'])}</td><td>{pct(o['wrong_displayed'])}</td><td>{pct(o['no_display'])}</td><td>{sec(o['ttfr_median'])}</td><td>{pct(o['never_correct'])}</td></tr>")
H.append("</table><p>Train numbers are not held-out: templates were built from these files. They are shown for regression checking only.</p>")
H.append("<p><b>Real device recordings</b> (<code>daniel_recovery_test.f32</code>, <code>daniel_offchest_test.f32</code>, the project's two witnesses to real contact/disconnect behavior): "
         "v0.16 output is identical to v0.15 &mdash; first display at 23.5 s / 22.5 s, 54.8 bpm / 52.6 bpm, final 54.8 / 53.3 bpm, same number of displayed ticks. No regression.</p>")

# ---------------- 7. caveats
H.append("<h2>7. Caveats and open items</h2><ul>"
         "<li><b>Time to first accurate HR did not improve</b> (median 6.5 s in every row; unchanged by this work). It is set by the 6 s confirmation wait (<code>CONS_WAIT_S</code>), which the sweep showed is still the best trade between speed and wrong readings. Roughly a third of all ticks are '--' because of it; 'correct ticks' cannot exceed roughly 60% on 17-s recordings.</li>"
         "<li><b>Slow-rate gains are modest and statistically thin</b>: 17 test files at 30&ndash;40 bpm; 32 train files built the merged template. Slow-rate curves are inconsistent between people (leave-one-out template correlation ~0.45), so template scoring does not help there; the gain comes from letting the slow-zone peak win more often.</li>"
         "<li>Test labels are ECG-derived and the corpus was filtered to pcg_sq &ge; 0.7 and |ECG&minus;PCG| &le; 5 bpm, so real-world accuracy will be lower. A single label per recording also penalizes real beat-to-beat variation. All new-sweep files are 'DUO3' hardware per the manifest; confirm that matches the device you ship.</li>"
         "<li>Test results are clustered by person (two users supply 15% of the 60&ndash;100 bpm files); see the macro rows and bootstrap interval.</li>"
         "<li>Not done: porting 0.16 to <code>c13_core_kit</code>/<code>coral-fw</code>, rebuilding the WASM/visualizer, CPU measurement on the PSoC6, updating <code>Combo13_HR_Algorithm_Change_Summary.docx</code>. "
         "The 8-s analysis window holds only ~4 beats at 30 bpm; a longer window for slow rates is a structural option not tried.</li>"
         "<li>Next candidates if you want more accuracy: the 100&ndash;160 bpm near-miss errors (display within 5&ndash;20% of label, partly label variability) and 160+ half-rate errors (2.4% of test ticks).</li></ul>")
H.append("</body></html>")
open(os.path.join(R, "retune_report.html"), "w", encoding="utf-8").write("\n".join(H))
print("wrote retune_report.html")
