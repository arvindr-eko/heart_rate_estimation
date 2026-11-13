# Version history

Compiled from the version READMEs and the change notes in the source headers
(`docs/VERSION_HISTORY_from_source_comments.txt` has the original wording). Dates are those written in the source.
Revision numbers keep incrementing (0.10, 0.11 ...); they do not roll over to 1.0.

| Rev | Date | Change |
|---|---|---|
| 0.1 – 0.6 | 2026-08-08 (same day) | Streaming C port promoted step by step: agreement signal flux, full-wave flux rectification, pitch-tilt trace, early-release and RAM consolidation, further parameter sweeps. Snapshots of the intermediate files are in `reference_c/embedded_c_history/` (`pre0.1-*`). |
| 0.9 | 2026-08-09 | Verified against the deployed visualizer build (byte-identical on real recordings); `TEAL_BPM_DELAY_SLOPE` pushed to 0.03 (more confirmation time before showing a high rate). `reference_c/releases/v0.9/` |
| 0.10 | 2026-08-09 | `TEMPLATE_WEIGHT_ENABLE` promoted into the shipped build. |
| 0.11 | 2026-08-10 | `TEMPLATE_MATCH_GATED_V2_ENABLE`: candidate chosen by scoring the top 6 autocorrelation peaks against templates instead of taking the single tallest. |
| 0.12 | 2026-08-10 | RAM / clock optimisation pass, no behaviour change (314/314 corpus files unchanged). `releases/v0.12` and `releases/v0.12_lean` (cleaned source, validated bit-exact against it). |
| 0.13 | 2026-08-11 | `TICK_S` 0.25 s -> 0.5 s (fewer clock cycles). |
| 0.14 | 2026-08-11 | Decimation stride 10 -> 12 (400 Hz -> 333.3 Hz), reducing RAM and clock. Accuracy traded for resources; 0.12 is the pre-change baseline. |
| 0.15 | 2026-10-06 | Lower BPM bound 40 -> 30 (lag grid extended; validated on synthetic data only). |
| 0.16 | 2026-10-12 | Retuned templates: 34 buckets (one merged 30-40 BPM bucket, then every 5 BPM 40-200) built from the train split of a 3,049-file corpus; `SLOW_RATIO_MIN` 1.5 -> 1.35. |
| 0.16 + qsfix | 2026-10-15 | Firmware fixes (watchdog reboot, quickselect O(n^2)); see `firmware/docs/HR_0.16_REBOOT_AND_DROPOUT_REPORT.md`. Version string still "0.16". |
| 0.16 + parabolic | 2026-10-16 (device-tested 10-19) | Parabolic peak refinement of the winning lag (display only), to reduce integer-lag quantisation at high BPM. On the 400-clip device run: MAE 3.01 -> 2.74 bpm, +-3 accuracy 83.8% -> 86.3%, +-5 unchanged. Version string still "0.16". |

## Known open items (from the author's notes)

- Clips at 192-197 BPM often display the half rate (~98 BPM); 145 -> 49 and 149 -> ~92 also persist.
- Untested hypothesis: the peak scan starts at bin 1, so lag 20 (200 BPM) can never be a peak.
- `COMBO13_VERSION` was not bumped for the quickselect and parabolic changes.
- Revisions 0.7 and 0.8 have no source folder on the author's PC; 0.4-0.6 exist only as zips (not included).
