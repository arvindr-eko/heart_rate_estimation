"""HTML report: old vs new autocorr templates per 5-bpm bucket (raw + detrended, 80-bin shipped
format; plus the full 126-bin new curve with the 80-bin cutoff marked)."""
import os, io, base64, csv, collections
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
OLD = r"C:\Users\daniel\Desktop\pcg_abab\embedded_c"
new = np.load(os.path.join(HERE, "new_templates.npz"), allow_pickle=True)
oraw = np.load(os.path.join(OLD, "autocorr_final_templates.npz"), allow_pickle=True)
odet = np.load(os.path.join(OLD, "autocorr_final_detrend_templates.npz"), allow_pickle=True)
LO = 20
FS_AC = 4000 / 60.0 * 60  # lag*bpm = 4000 (66.667Hz decimated autocorr rate)
old_b = {int(b) for b in oraw["buckets"]}
new_b = sorted(int(b) for b in new["buckets"] if f"template_raw80_{int(b)}" in new)
summ = {int(r[0]): r[1:] for r in new["summary"]}


def lab(b):
    return "30-40 (merged)" if b == 35 else (f"40-42.5" if b == 40 else str(b))


def pear(a, b):
    return float(np.corrcoef(a, b)[0, 1])


def lag_axis(n):
    return np.arange(LO, LO + n)


def b64(fig):
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=90, bbox_inches="tight"); plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def add_bpm_axis(ax):
    sec = ax.secondary_xaxis("top", functions=(lambda l: 4000.0 / np.maximum(l, 1), lambda b: 4000.0 / np.maximum(b, 1)))
    sec.set_xticks([200, 150, 100, 75, 60, 50, 40, 30]); sec.tick_params(labelsize=6)


rows, imgs = [], []
for b in new_b:
    nraw, ndet = new[f"template_raw80_{b}"], new[f"template_det80_{b}"]
    n126 = new.get(f"template_raw126_{b}")
    src = str(new[f"source_{b}"])
    fig, axs = plt.subplots(1, 3, figsize=(13, 2.6))
    x80 = lag_axis(80)
    r_raw = r_det = None
    for ax, nv, ov, ttl in ((axs[0], nraw, oraw[f"template_{b}"] if b in old_b else None, "raw (80-bin)"),
                            (axs[1], ndet, odet[f"template_{b}"] if b in old_b else None, "detrended (80-bin)")):
        if ov is not None:
            ax.plot(x80, ov, color="#888", lw=1.4, label="old")
        ax.plot(x80, nv, color="#d62728", lw=1.4, label="new")
        ax.set_title(f"{lab(b)} bpm  {ttl}", fontsize=8); ax.tick_params(labelsize=6); ax.grid(alpha=.25)
        add_bpm_axis(ax)
        if ov is not None:
            if "raw" in ttl: r_raw = pear(ov, nv)
            else: r_det = pear(ov, nv)
    if b not in old_b:
        axs[0].text(.5, .9, "no old template", transform=axs[0].transAxes, ha="center", fontsize=7, color="#555")
    axs[0].legend(fontsize=6, loc="upper right")
    x126 = lag_axis(126)
    if n126 is not None:
        axs[2].plot(x126, n126, color="#d62728", lw=1.2, label="new 126-bin")
        axs[2].axvline(LO + 80, color="k", ls="--", lw=.8)
        axs[2].text(LO + 81, 0.02, "80-bin cutoff (lag 100)", fontsize=6)
        if b == 35:
            axs[2].axvspan(4000.0 / 40, 4000.0 / 30, color="#1f77b4", alpha=.15)
            axs[2].set_title("30-40 bpm merged, full curve (blue band = fundamental lags 100-133)", fontsize=8)
        else:
            axs[2].axvline(4000.0 / b, color="#1f77b4", ls=":", lw=1)
            axs[2].set_title(f"{lab(b)} bpm full curve (blue = fundamental lag {4000/b:.0f})", fontsize=8)
        axs[2].tick_params(labelsize=6); axs[2].grid(alpha=.25)
    imgs.append((b, b64(fig)))
    nA, nB, fA, fB = (int(v) for v in summ[b][:4])
    rows.append((b, b in old_b, nA, nB, fA, fB, src, r_raw, r_det))

H = ["<html><head><meta charset='utf-8'><title>Autocorr templates old vs new</title><style>body{font-family:Segoe UI,Arial;margin:20px}"
     "table{border-collapse:collapse;font-size:12px}td,th{border:1px solid #bbb;padding:3px 7px;text-align:right}th{background:#eee}"
     ".s{background:#fff3cd}.n{background:#d1ecf1}img{max-width:100%}</style></head><body>"]
H.append("<h2>Autocorrelation templates: old (314-file corpus, 28 buckets) vs new (3,049-file combined corpus)</h2>")
H.append("<p>Curves are tick-averaged, each tick normalized by its own max (raw) or max |value| (detrended). x = lag (decimated samples; "
         "bpm = 4000/lag, shown on the top axis). Grey = old shipped template, red = new. Right panel = full 126-bin v0.15 curve "
         "(old templates only cover lags 20-99). <b>Primary source</b>: A = ticks where the algorithm's raw candidate was correct "
         "(old method); B = all ticks with a valid curve, used when A had &lt;50 ticks or bucket &lt;45 bpm (the algorithm "
         "mostly mis-locks harmonics there, so A is starved/biased). <b>Slow end (updated):</b> all labels &lt;40 bpm are pooled into ONE "
         "10-bpm-wide template (30-40, nominal center 35; 49 files). The 40 bucket now covers labels 40-42.5 only. Previous 5-bpm version "
         "saved as template_report_5bpm_only.html.</p>")
H.append("<table><tr><th>bucket</th><th>old template</th><th>ticks A</th><th>ticks B</th><th>files A</th><th>files B</th><th>primary</th>"
         "<th>r(old,new) raw</th><th>r(old,new) det</th></tr>")
for b, had, nA, nB, fA, fB, src, rr, rd in rows:
    cls = "n" if not had else ("s" if (src == "B" or (rr is not None and rr < 0.95)) else "")
    H.append(f"<tr class='{cls}'><td>{lab(b)}</td><td>{'yes' if had else 'NEW'}</td><td>{nA}</td><td>{nB}</td><td>{fA}</td><td>{fB}</td><td>{src}</td>"
             f"<td>{'' if rr is None else f'{rr:.3f}'}</td><td>{'' if rd is None else f'{rd:.3f}'}</td></tr>")
H.append("</table><p><span class='n'>blue</span> = bucket had no old template; <span class='s'>yellow</span> = B-source or r&lt;0.95 vs old.</p>")
for b, im in imgs:
    H.append(f"<img src='data:image/png;base64,{im}'>")
H.append("</body></html>")
out = os.path.join(HERE, "template_report.html")
open(out, "w", encoding="utf-8").write("\n".join(H))
print(out, len(rows), "buckets")
