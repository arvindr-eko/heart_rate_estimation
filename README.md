# CORE 300 heart-rate algorithm ("combo13") — source and history

Heart rate from a phonocardiogram (PCG) audio stream: band-pass, envelope, autocorrelation against rate-specific
templates, consensus and a confidence gate. Embedded C for the PSoC6 / Cortex-M4F (Coral firmware) plus the Python
reference used to develop, tune and validate it. Start with `docs/ALGORITHM.md` for how it works.

**Current version: 0.16** (`COMBO13_VERSION`), plus two later firmware changes that did not bump the version string
(quickselect/reboot fix and parabolic peak interpolation) — see `VERSION_HISTORY.md`.

> DRAFT assembled from the author's working folders. Nothing here has been re-validated as a package; see "Verification
> status" at the bottom. Audio recordings and test corpora are NOT included.

## Layout

| Folder | What it is |
|---|---|
| `firmware/` | **What goes into the Coral firmware repo.** `heartrate/` = the 5 algorithm files (`combo13_stream.c/.h`, `combo13_stream_internal.h`, two template headers) at 0.16 with the quickselect fix and parabolic interpolation; `fw/audio_io_task.c` = the one caller change; `host_test/` = PC test driver; `patches_vs_0.14/` and `original_0.14/` = diff base; `docs/` = integration notes and the reboot/dropout report. Read `firmware/docs/README_FOR_FW_DEV.md` first. |
| `reference_c/` | The research-lineage C (PC and WASM builds, many experiment switches). `v0.16_current/` is the latest; `releases/v0.9 ... v0.15` are the earlier shipped revisions (source and README only); `embedded_c_history/` holds the original C port and the development snapshots (`pre0.1-*`, rejected attempts). |
| `python/reference/` | Python reference implementation (`combo13_heart_rate.py` is the main one) and the evaluation harness. |
| `python/retune_2026-10/` | Scripts that built the 0.16 templates from the 3,049-file corpus (train/test split, sweeps, final evaluation). |
| `python/template_tools/` | Earlier template-builder scripts. |
| `docs/` | Algorithm, pipeline, testing and feature reports; `experiment_reports/` = dated sweep and check reports from July–Oct 2026; `VERSION_HISTORY_from_source_comments.txt` = the author's change log copied verbatim from the source header. |

Two code lineages exist. `firmware/` is the lean, shipped-to-device version. `reference_c/` is the research version of
the same algorithm (it carries variants and diagnostics, and builds to WASM for the visualizer). Where they differ,
`firmware/` is the one that runs on the unit.

## Building the host test (firmware lineage)

```
cc -Wall -Wextra -O1 -Ifirmware/heartrate -Ifirmware/host_test -o drv \
   firmware/host_test/drv_main.c firmware/heartrate/combo13_stream.c -lm
./drv <raw_float32_file> 4000        # prints CSV: t,bpm,bpm_valid,is_flatline,raw_bpm,raw_valid,harm
```
It prints `COMBO13_VERSION=0.16` and `STATE_SIZE_BYTES=16600` on stderr. Input is raw little-endian float32 at 4 kHz.

## Python

Needs `numpy`, `scipy`, `pandas`. Many scripts contain absolute Windows paths from the author's PC
(`C:\Users\daniel\...` / `Desktop\...`) pointing at recordings that are not in this repo; edit those before running.
`python/reference/eval_harness.py` and `corpus_sources.py` are where the corpus locations are defined.

## Not included (on purpose)

Audio (WAV) recordings, `.f32` test vectors, pickled caches, WASM/JS builds, logs and zips. The recordings may carry
patient-privacy or licensing restrictions and must be cleared before any upload. `.gitignore` excludes them.

## Verification status (what was and was not checked when this folder was assembled)

- `firmware/heartrate/` compiles with `-Wall -Wextra` under zig cc and the host driver runs; the version and state-size
  strings match the integration notes. A real 4 kHz recording produced only 4 valid ticks, which was not compared against
  expected output — so this is a build-and-run check, not a result validation.
- `firmware/heartrate/combo13_stream.h` was copied from the author's firmware build tree (`cfw2`) because the 0.16 handoff
  did not change it; it differs from the research-lineage header in `reference_c/`. Confirm against the firmware repo's own copy.
- The Python scripts were not re-run. Some depend on corpora that are not here.
- Hardware results (accuracy runs, soak tests) are summarised in the reports under `firmware/docs/` and `docs/`; the
  underlying run data is not included.
- No license has been chosen. Treat as proprietary until the owner decides.
