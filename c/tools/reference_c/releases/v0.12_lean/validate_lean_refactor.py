"""Tick-by-tick validation of v0.12_lean/combo13_stream.c against the shipped embedded_c/
combo13_stream.c build (both revision 0.12, same compile flags). This is a pure-refactor check:
every Combo13StreamTick field must match EXACTLY, not just the displayed-bpm classification.

Usage:
    python validate_lean_refactor.py real          # the two named .f32 real recordings
    python validate_lean_refactor.py subset [N]     # every Nth corpus file (default N=8)
    python validate_lean_refactor.py full           # the full 314-file corpus
"""
import os
import subprocess
import sys

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, r"C:\Users\daniel\Desktop\pcg_abab")
import corpus_sources as CS  # noqa: E402

ROOT = r"C:\Users\daniel\Desktop\pcg_abab"
ORIG_DIR = os.path.join(ROOT, "embedded_c")
LEAN_DIR = os.path.join(ROOT, "v0.12_lean")
NODE = r"C:\Users\daniel\emsdk\node\22.16.0_64bit\bin\node.exe"
ORIG_DRIVER = os.path.join(ORIG_DIR, "test_driver_stream_csv.js")
LEAN_DRIVER = os.path.join(LEAN_DIR, "test_driver_stream_csv.js")


def run_driver(driver_js, f32_path, fs):
    out = subprocess.run([NODE, driver_js, f32_path, str(fs)], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"driver {driver_js} failed on {f32_path}: {out.stderr}")
    rows = []
    for line in out.stdout.splitlines()[1:]:
        if not line.strip():
            continue
        t, bpm, bpm_valid, is_flat, raw_bpm, raw_valid, harm = line.split(",")
        rows.append((float(t), float(bpm), int(bpm_valid), int(is_flat), float(raw_bpm),
                     int(raw_valid), int(harm)))
    return rows


def compare(a_rows, b_rows):
    n = min(len(a_rows), len(b_rows))
    mismatches = []
    if len(a_rows) != len(b_rows):
        mismatches.append(f"TICK COUNT MISMATCH: orig={len(a_rows)} lean={len(b_rows)}")
    for i in range(n):
        at, abpm, abv, afl, araw, arv, aharm = a_rows[i]
        bt, bbpm, bbv, bfl, braw, brv, bharm = b_rows[i]
        bad = []
        if at != bt:
            bad.append(f"t: orig={at} lean={bt}")
        if abv != bbv:
            bad.append(f"bpm_valid: orig={abv} lean={bbv}")
        if afl != bfl:
            bad.append(f"is_flatline: orig={afl} lean={bfl}")
        if arv != brv:
            bad.append(f"raw_valid: orig={arv} lean={brv}")
        if aharm != bharm:
            bad.append(f"harmonic_confirmed: orig={aharm} lean={bharm}")
        if abpm != bbpm:
            bad.append(f"bpm: orig={abpm} lean={bbpm} (diff={abpm-bbpm:.6f})")
        if araw != braw:
            bad.append(f"raw_bpm: orig={araw} lean={braw} (diff={araw-braw:.6f})")
        if bad:
            mismatches.append(f"  tick {i} (t={at:.2f}): " + "; ".join(bad))
    return n, len(mismatches), mismatches[:20]


def validate_real_files():
    files = [("daniel_recovery_test.f32", 5000.0), ("daniel_offchest_test.f32", 5000.0)]
    total_ticks = 0
    total_mismatch = 0
    for fname, fs in files:
        orig_path = os.path.join(ORIG_DIR, fname)
        lean_path = os.path.join(LEAN_DIR, fname)
        a_rows = run_driver(ORIG_DRIVER, orig_path, fs)
        b_rows = run_driver(LEAN_DRIVER, lean_path, fs)
        n, nmis, details = compare(a_rows, b_rows)
        total_ticks += n
        total_mismatch += nmis
        print(f"{fname}: {n} ticks compared, {nmis} mismatches.")
        for d in details:
            print(d)
    print(f"\nreal recordings TOTAL: {total_ticks} ticks, {total_mismatch} mismatches")
    return total_ticks, total_mismatch


def validate_corpus(files, tag):
    total_ticks = 0
    total_mismatch = 0
    n_files_with_mismatch = 0
    worst = []
    for idx, (wav_path, gt, cat, ident) in enumerate(files):
        fs_r, wdata = wavfile.read(wav_path)
        if wdata.ndim > 1:
            wdata = wdata.mean(axis=1)
        wdata = wdata.astype(np.float32)
        tmp_f32 = os.path.join(LEAN_DIR, f"_lean_validate_tmp_{os.getpid()}.f32")
        wdata.tofile(tmp_f32)
        try:
            a_rows = run_driver(ORIG_DRIVER, tmp_f32, fs_r)
            b_rows = run_driver(LEAN_DRIVER, tmp_f32, fs_r)
        finally:
            os.remove(tmp_f32)
        n, nmis, details = compare(a_rows, b_rows)
        total_ticks += n
        total_mismatch += nmis
        if nmis:
            n_files_with_mismatch += 1
            worst.append((ident, gt, cat, nmis, details))
        if (idx + 1) % 25 == 0:
            print(f"  [{tag}] {idx+1}/{len(files)} files done, running mismatch total={total_mismatch}",
                  file=sys.stderr)
    print(f"\n=== {tag}: {len(files)} files, {total_ticks} ticks compared, {total_mismatch} mismatches "
          f"({n_files_with_mismatch} files with >=1 mismatch) ===")
    for ident, gt, cat, nmis, details in worst[:10]:
        print(f"\n--- {ident} (gt={gt}, {cat}): {nmis} mismatches ---")
        for d in details[:5]:
            print(d)
    return total_ticks, total_mismatch


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "real"
    if mode == "real":
        validate_real_files()
    elif mode == "subset":
        n_step = int(sys.argv[2]) if len(sys.argv) > 2 else 8
        files = CS.load_all_files(include_synth=False)
        subset = files[::n_step]
        print(f"validating {len(subset)}/{len(files)} corpus files (every {n_step}th)...", file=sys.stderr)
        validate_corpus(subset, f"subset(every {n_step})")
    elif mode == "full":
        files = CS.load_all_files(include_synth=False)
        print(f"validating full {len(files)}-file corpus...", file=sys.stderr)
        validate_corpus(files, "FULL CORPUS")
    else:
        print(f"unknown mode {mode}", file=sys.stderr)
        sys.exit(1)
