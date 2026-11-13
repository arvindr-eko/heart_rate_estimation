#!/usr/bin/env python3
"""
Shared corpus file-list loaders (2026-07-25), so every sweep/report script uses ONE consistent set of
(path, gt, category, ident) tuples instead of each re-implementing its own file-discovery logic.

Three sources:
  - real: the labeled 269-file Amazon corpus (eval_harness.ROOT), category 'regular' or 'doubled'
    (files whose name ends _2spedup, the corpus's existing 2x-sped-up synthetic-tachycardia
    augmentation -- ground truth taken as-is from eval_harness's existing label join, same convention
    every sweep script in this project has always used).
  - synthetic: the 96-file SynthConfidence set, category 'synthetic', gt from the hrNNN_ filename prefix.
  - selfrecorded: Daniel's own PulseOx-verified recordings (Desktop\\RecordedHeartSoundsWithPulseOx,
    added 2026-07-25), category 'selfrecorded', gt parsed from the poxNNbpm filename suffix baked in by
    pcg_visualizer's own recorder -- per Daniel's explicit choice, NOT recomputed from the paired CSV
    trace, so the 2 files that lack a companion CSV still count (all 45 files included).
"""
import os, glob, re
import numpy as np
import eval_harness as EH

SYNTH_ROOT = r"C:\Users\daniel\Desktop\SynthConfidence"
SELFRECORDED_ROOT = r"C:\Users\daniel\Desktop\RecordedHeartSoundsWithPulseOx"
SYNTH_HR_RE = re.compile(r"hr(\d+)_")
SELFRECORDED_HR_RE = re.compile(r"_pox(\d+)bpm$")


def load_real_files():
    lab = EH.load_labels()
    files = []
    for sub in sorted(os.listdir(EH.ROOT)):
        sd = os.path.join(EH.ROOT, sub)
        if not os.path.isdir(sd):
            continue
        for w in sorted(glob.glob(os.path.join(sd, "*.wav"))):
            k = EH.wkey(w)
            L = lab.get(k)
            if L is None or not np.isfinite(L["ecg"]):
                continue
            stem = os.path.splitext(os.path.basename(w))[0]
            category = "doubled" if stem.endswith("_2spedup") else "regular"
            files.append((w, float(L["ecg"]), category, stem))
    return files


def load_synth_files():
    files = []
    for w in sorted(glob.glob(os.path.join(SYNTH_ROOT, "*.wav"))):
        m = SYNTH_HR_RE.search(os.path.basename(w))
        if not m:
            continue
        gt = float(int(m.group(1)))
        stem = os.path.splitext(os.path.basename(w))[0]
        files.append((w, gt, "synthetic", stem))
    return files


def load_selfrecorded_files():
    files = []
    for w in sorted(glob.glob(os.path.join(SELFRECORDED_ROOT, "*.wav"))):
        stem = os.path.splitext(os.path.basename(w))[0]
        m = SELFRECORDED_HR_RE.search(stem)
        if not m:
            continue
        gt = float(int(m.group(1)))
        files.append((w, gt, "selfrecorded", stem))
    return files


def load_all_files(include_synth=True):
    files = load_real_files() + load_selfrecorded_files()
    if include_synth:
        files += load_synth_files()
    return files
