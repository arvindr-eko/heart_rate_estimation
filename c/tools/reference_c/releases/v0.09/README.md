# Combo13 streaming heart-rate estimator — revision 0.9

Embedded-C port of the combo13 PCG (phonocardiogram) heart-rate algorithm, streaming
front end, RAM-reduced for a PSoC6 / Cortex-M4F target. This is **revision 0.9** —
verified against the deployed comparison build in the diagnostic visualizer,
byte-identical output on real test recordings.

**0.9 SAME-DAY ADDENDUM (2026-08-09), per Daniel's "we have some logic that tells us to take
longer when we are about to present a high number... correct?" then "push slope to 0.03 and put it
into V0.9."** Confirmed `required_teal_delay_ticks()`'s `TEAL_BPM_DELAY_SLOPE` is exactly that
mechanism (extra confirmation ticks scale with `max(bpm-60, 0)`) -- but the shipped value
(0.00239) wasn't strong enough to actually delay `daniel_recovery_test.f32`'s own documented
160bpm bad first reading. Added `TEAL_BPM_DELAY_SLOPE_FIRST` (same `!ever_displayed`-only scoping
as the rest of this round) and found the threshold empirically: the fix engages between 0.020
(still wrong) and 0.021 (correct) -- **PROMOTED to 0.03** for comfortable margin. Fixes that exact
case (first reading 160bpm->53.9bpm, correct, delayed only 2.25s) for a further first-reading
correct 293/314->294/314 on the full corpus, at correct(all ticks) 15495->15396 (-99) and
no_display 9663->9776 (+113) on top of the tradeoff already shipped earlier that same day. Both
real recordings reverified, final/tail values unchanged on both.

**REVISION 0.9 (2026-08-09), per Daniel's "optimize for the accuracy of the first displayed
reading" then "adapting some of these constants at the beginning of a file and after a flatline or
spike... best of both worlds" then "push 293/314 (moderate) to the visualizer."** A new
first-reading-accuracy sweep (tracks each file's FIRST non-"--" bpm value specifically, not the
mean/median-per-tick tally every other sweep in this project optimizes) found a real, scaling
tradeoff: stricter confirmation before the first release reduces wrong "other" answers but costs
overall correct-tick count and no-display rate. Daniel's own follow-up idea -- apply the stricter
constants only at a session's true first-ever reading (not every later flatline/spike reconnect)
-- was confirmed by the data to Pareto-dominate the equivalent global change: same first-reading
gain, measurably less cost, using `st->ever_displayed` (already existed for exactly this
true-cold-start-vs-later-reconnect distinction).
- **PROMOTED: `RAMP_DELAY_EXTRA_TICKS_FIRST` 4.0->6.0** (applies only while `!ever_displayed`).
  Full 314-file real+selfrecorded corpus: first-reading correct 287/314->293/314 (91.4%->93.3%),
  octave_errors 114->88 (better), correct(all ticks) 16082->15495, no_display 8954->9663. The
  equivalent GLOBAL change (same value, applied to every event not just the first) reached the
  SAME 293/314 but cost more (correct 15425, no_display 9767) -- the adaptive scoping recovered
  +70 correct ticks and -104 no-displays for free. A deliberate value tradeoff, not a clean win --
  chosen from a full frontier (a cheaper 289/314 option and a steeper 296/314 option were also
  found and left unshipped).
- Both real recordings diffed: identical first-display and final tail value to the pre-promotion
  build, including `daniel_recovery_test.f32`'s own documented 160bpm->54.5bpm self-correction.

**REVISION 0.8 (2026-08-09), per Daniel's follow-up "any other ideas... to detect locking on to
harmonics or subharmonics?" then "I want you to try all three."** One PROMOTED, three REJECTED,
all documented with full corpus evidence in `combo13_stream.c`'s own comments.
- **ROBUST_TRIM_FRAC 0.012->0.015, PROMOTED (variant 4 only)**: re-swept after `SPIKE_DETECT_MULT`
  (previous round, 20.0->15.0) changed the artifact-removal chain feeding the autocorrelation this
  constant trims. Full 314-file corpus: correct 16,014->16,082 (+68), octave_errors 119->114 (-5),
  other ~flat, no_display -66 -- a genuine multi-metric win. Tick-by-tick diff against both real
  recordings confined to diagnostic-only fields plus one already-correct value shifting 0.6bpm,
  well within tolerance.
- **LAG_POW, re-swept, REJECTED (variant 4 only)**: 45-file sample showed a clean octave-error win
  at 0.25-0.30, but the full 314-file corpus told a different story -- correct 16,014->15,879
  (-135, a real loss) despite octave_errors genuinely improving (119->102). The exact "small-sample
  misleads" pattern this project has hit before. Left at 0.2.
- **Ratio-aware jump confirmation, prototyped and REJECTED**: extra confirmation ticks for BPM
  jumps whose ratio sits near an exact harmonic (2x/0.5x/3x/1/3x). Fatal flaw found mechanistically,
  not just from bad numbers: a jump ratio alone can't distinguish "escaping a wrong harmonic" from
  "locking onto one" -- both look identical from the ratio's own perspective. Directly confirmed on
  `daniel_recovery_test.f32`'s own documented 160bpm->54.5bpm self-correction: delayed 1s at 8 extra
  ticks, never happens at all within the recording at 16 extra ticks. Kept in source, inert by
  default (`TEAL_JUMP_HARMONIC_CONFIRM_TICKS` defaults to equal the base confirm-tick count).
- **Cross-signal harmonic-ratio check (candidate vs. flux), investigated and REJECTED**: a real
  anecdote (candidate wrongly at 160bpm while flux correctly read ~54.5bpm, a ~3x mismatch)
  motivated checking whether autocorrelation is classically biased toward subharmonics while FFT
  (flux) is biased toward harmonics. Full 314-file corpus (18,366 ticks): among the 469 ticks where
  candidate/flux disagreed by a harmonic ratio, candidate was actually correct 73% of the time vs.
  flux only 21% -- the opposite of what the anecdote suggested, and no exploitable
  higher-rate/lower-rate shortcut either (55%/45%, close to a coin flip). No mechanism built.

`STATE_SIZE_BYTES` unchanged at 11,076. Full sweep numbers and mechanistic explanations in
`combo13_stream.c`'s own comments and project memory.

**REVISION 0.7 (2026-08-09), per Daniel's "any other ideas to increase accuracy?" then "let's try
all three."** Three ideas pursued: one PROMOTED, two REJECTED, plus a real bug found and fixed.
- **SPIKE_DETECT_MULT 20.0->15.0, PROMOTED**: inherited unchanged from combo13_opt, never
  independently re-validated for this streaming/decimated/int16-quantized pipeline. Full 314-file
  corpus: correct 15,971->16,014 (+43), octave_errors 164->119 (-27.4%), other 975->930 (-4.6%),
  no_display +47 (small, acceptable). BYTE-IDENTICAL on both real recordings (win lands on other
  corpus files). `TRANSIENT_LIMIT_MULT`/`ARTIFACT_DETECT_MULT`/`ARTIFACT_EXTEND_MULT`/
  `ARTIFACT_BASELINE_PCTL` also swept -- all looked promising on a 45-file sample but did NOT
  survive full-corpus re-check (the "small-sample misleads" pattern this project has hit before
  with TENT) -- NOT PROMOTED. `SIGNAL_ONSET_THRESH` excluded entirely -- confirmed dead for
  production output since 2026-08-07.
- **Symmetric harmonic-veto check (0.5x lag), REJECTED**: the existing veto only guards against the
  candidate being too FAST; tried a symmetric check for too SLOW. Instrumented with counters before
  trusting a zero-effect result: reached 18,361/18,361 times, found a competing peak 3,025 times,
  but that peak was taller than the candidate's own value only 3 times ever. A real, informative
  negative result (detrending rarely reshuffles which lag has the tallest local peak). Kept opt-in
  only (`HARMONIC_VETO_SYMMETRIC_ENABLE`), zero cost by default.
- **Curve-correlation as a soft combine-weight multiplier, REJECTED**: tried curve_corr as a smooth
  multiplier on the combine weight (like `AGREE_BONUS`) instead of the already-rejected hard veto.
  Swept 6 LO/HI ranges on the full corpus -- every configuration came back flat-to-slightly-worse.
  Kept opt-in only (`CURVE_WEIGHT_ENABLE`), zero cost by default.
- **REAL BUG found and fixed**: `win_teal.Sw` was only populated inside `COMBO13_EXPOSE_DEBUG` --
  meaning the already-rejected curve-correlation hard-veto prototype's entire corpus sweep (see the
  2026-08-09 entry below) was built without that flag and read UNINITIALIZED STACK MEMORY, not the
  real curve. Fixed by making that memcpy unconditional. Re-verified the hard-veto rejection with
  real data: the verdict still stands, but the originally-reported regression magnitude was wrong
  -- with real data the effect is much milder (near-neutral at conservative settings, a real-but-
  modest tradeoff at aggressive ones), not the dramatic collapse originally reported.

`STATE_SIZE_BYTES` unchanged at 11,076 in the default build (both rejected ideas' extra state
compiles out via their `_ENABLE` opt-in gates). Full sweep numbers in project memory.

**2026-08-09, same day, curve-correlation PRODUCTION GATE prototyped and REJECTED (source-tree sync
only, no revision bump -- `CURVE_GATE_ENABLE`-gated, undefined by default, zero cost in the real
shipped build, confirmed via a fresh standalone build reproducing `STATE_SIZE_BYTES=11076`):**
tried using curve_corr as a real algorithm-level hard veto (not just a confidence-badge input),
per Daniel's "could it be used to improve our algorithm further?" A cheap single-snapshot
mechanism (`curve_gate_prev[S_LEN_V]`, refreshed once every `CURVE_CORR_GAP_TICKS` ticks -- ~400B,
not panel 7's ~6KB debug ring buffer) feeds a veto on `pushed_valid`, mirroring how
`jump_unconfirmed`/`bpm_delay_unconfirmed` already gate that same variable. **Corpus-swept
THRESH=0.0/0.2/0.35/0.5 x STREAK_LIMIT=1/2/4 on a 45-file sample, all rejected**: even the
strictest low-correlation band tried is still 63.3% correct on the full 314-file/17,076-tick
corpus (direct check, not inferred) -- a hard veto at ANY threshold in this family discards
roughly 2 correct answers for every 1 truly-wrong one it catches. A real implementation bug was
found and fixed along the way (the low-correlation streak counter was incrementing once per TICK
instead of once per 4s snapshot EVENT, since the correlation value is held constant between
snapshots -- letting one bad window blow past any streak limit within itself); fixing it improved
the picture but did not change the conclusion. Left in the source as an opt-in (`CURVE_GATE_ENABLE`)
experimentation hook, not because it's ready to ship. Byte-identical production output and debug
WASM behavior re-confirmed on both real recordings after this change.

**2026-08-09, same day, GAP re-tune (source-tree sync only, no revision bump --
`COMBO13_EXPOSE_DEBUG`-gated, zero production impact, confirmed via a fresh standalone build):**
the curve-correlation prototype below shipped its first cut comparing consecutive ticks (GAP=1)
and Daniel caught it immediately in live use -- "that doesn't seem strict enough. It is almost
always at 1." Root-caused, not just re-tuned blind: the main candidate's analysis window grows to
`SINGLE_WIN_MAX`=8.0s at `TICK_S`=0.25s/tick, so two consecutive ticks' windows share
(8.0-0.25)/8.0 = 96.9% of their samples once at steady state -- confirmed empirically (median
correlation 0.97, 60-67% of ticks >=0.95 across both real recordings at GAP=1). Redesigned
`prev_teal_curve`/`prev_teal_curve_valid` into a `curve_ring[CURVE_CORR_GAP_TICKS][S_LEN_V]` ring
buffer (`curve_ring_head`/`curve_ring_count`) so the comparison can reach back further than 1 tick,
added a `CURVE_CORR_GAP_TICKS_OVERRIDE` sweep hook (default 16, same convention as this project's
other `_OVERRIDE` macros), verified the GAP=1 ring-buffer path reproduces the old single-variable
implementation's output exactly (pure refactor, no behavior change), then swept GAP=1/4/8/16/32:
median correlation dropped cleanly and monotonically (0.97->0.84->0.65->0.34->0.12 on
`daniel_recovery_test.f32`). Chose **GAP=16** (4.0s apart, ~50% fresh window content) as the new
default -- the first value showing genuine two-directional dynamic range: real negative dips (down
to ~-0.35) during actual instability (flatline/reconnect transients), climbing to a settled-but-
not-saturated 0.6-0.9 during genuinely stable tracking, neither pinned at ceiling nor uniformly low.
Re-confirmed the known octave-error self-correction on `daniel_recovery_test.f32` (160bpm-\>54.5bpm)
still does NOT produce a sharp dip even at GAP=16 -- 160 and ~54.5bpm sit near a 3:1 harmonic ratio,
so the true fundamental's own curve already contains a strong peak near the "wrong" harmonic's lag,
meaning whole-curve shape barely changes even as the peak-pick shifts between two peaks already
present in the same curve. This is a structural limit of whole-curve correlation, not a tuning gap,
and it means this metric alone cannot flag octave errors regardless of gap size. See project memory
(`project_pcg_embedded_c`) for the full sweep numbers.

**2026-08-09, source-tree sync only (no revision bump -- `COMBO13_EXPOSE_DEBUG`-gated, zero
production impact, confirmed via a fresh standalone build):** added `dbg_curve_corr`/
`dbg_curve_corr_valid` -- a frame-to-frame Pearson correlation of `win_teal`'s own detrended
autocorrelation curve against the same curve `CURVE_CORR_GAP_TICKS` ticks ago (see the GAP re-tune
entry above for why "ago" is a tunable gap and not a fixed 1 tick), prototyped per Daniel's "does
anything measure the consistency of the autocorrelations frame to frame?" Shown as its own
standalone panel (panel 7, reordered above the corpus scatter panel per Daniel's request) in
`pcg_visualizer/index.html`, not yet folded into the confidence-metric prototype or any algorithm
decision. Not yet ported into the production algorithm or public API; kept in sync here purely for
source-tree consistency.

**2026-08-08, same day, source-tree sync only (no revision bump -- `COMBO13_EXPOSE_DEBUG`-gated,
zero production impact, confirmed via a fresh standalone build):** added 6 new debug struct fields
(`dbg_conf`/`dbg_conf_valid`, `dbg_agree_margin`/`dbg_agree_margin_valid`, `dbg_trailing_spread`,
`dbg_trailing_count`) as raw inputs to a confidence-metric prototype, computed and displayed in
JS in `pcg_visualizer/index.html` next to the live HR readout -- see that project's memory
(`project_pcg_visualizer`) for the validated formula and corpus numbers. Not yet ported into the
production algorithm or public `Combo13StreamTick` API; this folder's copy is kept in sync purely
for source-tree consistency, matching this project's own established practice for prior
debug-only additions (C0-C5/Part A/B panels, etc.).

**2026-08-08 update (revision 0.6, same day as 0.1-0.5): part 2 of the same optimization run**,
continuing down the list of constants flagged as reasoned-but-not-corpus-tuned.
- **`TENT_PEAK_BPM`/`PEAK_H`/`LO_H`/`HI_H`** (`bpm_favor_center()`'s combine-weight shape, no
  tuning history at all): 1-D sweeps on a 60-file sample showed small trends, but NONE of it
  survived a full 314-file corpus check — `TENT_PEAK_BPM=60.0` actually scored flat-to-slightly-
  worse (15,052/165) vs the shipped 55.9's 15,053/164. Classic 60-file-sample-misleads result.
  **Not promoted.**
- **`PEAK_MEAN_TOP_PCT`/`DIFF_THRESH`/`SUSTAIN_S`** (the flatline detector — **safety-sensitive**,
  gates whether the device shows a number or "--"): `PEAK_MEAN_DIFF_THRESH` and `PEAK_MEAN_TOP_PCT`
  turned out to be genuinely miscalibrated for this streaming design (a straight port/rescale of
  the legacy JS pipeline's own value, never independently corpus-tuned here) — lowering either
  alone improved the full 314-file corpus substantially (correct +477 to +1,054, no_display -480
  to -1,103, octave_errors completely flat) with **zero change** to either real recording's actual
  disconnect-detection timing. Found and rejected a more aggressive pairing (`THRESH=0.001` +
  `TOP_PCT=0.05`) after it introduced a narrow **interaction effect** — neither change alone caused
  it, but together they delayed `daniel_recovery_test.f32`'s self-correction out of its
  pre-existing documented spurious 160bpm lock by 5 extra ticks (1.5s). **Promoted the safer
  `THRESH=0.002` + `TOP_PCT=0.05` pairing instead**, confirmed to produce byte-identical output to
  the pre-sweep build on both real recordings while still capturing most of the win (correct
  15,053→15,971, no_display 9,923→8,973, octave_errors flat at 164).
  `PEAK_MEAN_SUSTAIN_S` showed a real, uncapped tradeoff — higher values kept improving corpus
  numbers with no plateau, but each 0.5s increase cost exactly 0.5s of real device-off detection
  latency on both real recordings. Presented directly to Daniel with the actual numbers rather
  than auto-promoted; his call was to **leave it unchanged at 1.0s**, prioritizing device-off
  responsiveness over the extra corpus accuracy.

All 3 constants' `-D..._OVERRIDE` hooks added to `combo13_stream.c` for future re-sweeps. No
struct/state-size change — `STATE_SIZE_BYTES` stays 11,076.

**2026-08-08 update (revision 0.5, same day as 0.1-0.4): another optimization run, targeting
accuracy, harmonic (octave) error count, no-display rate, and time-to-first-display together.**
Surveyed the source for constants flagged as reasoned-but-not-corpus-tuned or last validated
against a signal that no longer exists, and found 2 candidates:
- **`EARLY_RELEASE_MIN_TICKS`/`MAX_SAVE_S`** (2 ticks/5.0s) was tuned 2026-08-07 entirely around
  TILT's own noise characteristics, before `AGREEMENT_SIGNAL_FLUX` existed at all. Full 314-file
  corpus grid over `MIN_TICKS` 1/2/3 × `MAX_SAVE_S` 4.0-10.0 found `MIN_TICKS=1` wins on every
  metric simultaneously (correct 14,810→15,032, octave_errors 168→170 [+2, within noise],
  no_display 10,176→9,937, median time-to-first-display 5.75s→5.50s); `MAX_SAVE_S` plateaus at
  6.0. **Promoted to 1/6.0.**
- **`AGREE_BONUS`** (2.70) was inherited unchanged through every agreement-signal swap this
  project has made and every retune of the signal itself, never independently validated for flux.
  A 1-D sweep found a real, monotonic trend — octave_errors dropped 168→163→162→161 as bonus went
  2.70→4.5→6.5→8.0, correct flat-to-improved, zero cost elsewhere, plateauing past 6.5.
  `AGREEMENT_BPM_TOL` (11.10) was also swept and found already near-optimal — left unchanged.
  **Promoted `AGREE_BONUS` to 6.5.**

Both changes validated **together** on the full 314-file corpus: correct 14,810→15,053,
octave_errors 168→164, other 929→943, no_display 10,176→9,923, median time-to-first-display
5.75s→5.50s — all 4 metrics moved the right direction simultaneously, "other" (+14, +1.5%) the
only real cost. Both real recordings (`daniel_recovery_test.f32`/`daniel_offchest_test.f32`)
diffed tick-by-tick against the pre-sweep build: every differing line was a diagnostic-only field
(`harmonic_confirmed`/`raw_cand`), the actual displayed bpm and first-display timing were
unchanged on the recovery file (including the pre-existing documented spurious 160bpm lock, not
made worse) and shifted by a benign 0.5bpm on 2 already-correct offchest ticks (52.2→52.7) with
the same first-display timing. `AGREEMENT_BPM_TOL_OVERRIDE`/`AGREE_BONUS_OVERRIDE` `-D` hooks
added for future re-sweeps, matching the existing `EARLY_RELEASE_*_OVERRIDE` convention. No
struct/state-size change — `STATE_SIZE_BYTES` stays 11,076.

**2026-08-08 update (revision 0.4, same day as 0.1/0.2/0.3): tilt removed entirely.**
Daniel asked directly why tilt was still being computed at all, since flux replaced it
as the agreement signal back in 0.2. Direct grep confirmed the suspicion: tilt's result
(`tilt_valid`/`tilt_bpm`) was consumed nowhere except the dead `#else` branch of the
`AGREEMENT_SIGNAL_FLUX` toggle and debug capture the visualizer no longer reads — true
dead code, not merely unused-but-safe. **Removed entirely**: the native-rate/FFT
pitch-tilt computation (`streaming_tilt_push_sample()`, `pitch_tilt_feature()`, the whole
`fft128()`/`cplx`/`fft_tw` FFT subsystem — confirmed by grep to have no other consumer),
every tilt-exclusive struct field (`pitch_bp_z`, `kumar_recent*`, `tilt_ring`,
`running_max_rms`, `last_t_carry`, plus the tilt debug fields), and every tilt-exclusive
constant (`TILT_SPLIT_HZ`, `PITCH_BP_B`/`A`, `PITCH_FFT_LO_HZ`/`HI_HZ`, `PITCH_GATE_FRAC`,
`TILT_FRAME_LEN`/`HOP`/`MAX_FRAMES`, `TILT_AC_LO`/`HI`/`LEN`). `AGREEMENT_SIGNAL_FLUX`
itself was also removed — flux is now unconditional. `SCRATCH_SHARED_LEN` (shared with
flux's own gather buffer) dropped from `max(FEAT_MAX_V, TILT_MAX_FRAMES)` to plain
`FEAT_MAX_V`, since `TILT_MAX_FRAMES` had dominated that max for every variant — this is
where most of the RAM savings actually came from, not tilt's own dedicated buffers.
**Validated as pure dead-code elimination, not a behavior change**: full 314-file corpus
score identical to 0.3's own numbers (correct=14,810, octave_errors=168, other=929,
no_display=10,176), and both real recordings (`daniel_recovery_test.f32`/
`daniel_offchest_test.f32`) produce byte-identical tick-by-tick output against a baseline
built from the frozen pre-removal 0.3 source. **`STATE_SIZE_BYTES` 17,052→11,076
(-5,976B, -35.0%)** — substantially more than tilt's own buffers alone would explain,
confirming the `SCRATCH_SHARED_LEN` shrink was the dominant factor. **Clock cycles also
improved** (tilt's computation had kept running unconditionally even after flux took
over the real decision in 0.2) — measured via this project's own WASM timing harness on
both real recordings: 1214.6→1114.9 µs/tick (recovery, -8.2%), 1503.2→1385.0 µs/tick
(offchest, -7.9%).

**2026-08-08 update (revision 0.3, same day as 0.1/0.2):** Daniel asked to optimize flux
further now that it was the shipped default. Full 314-file own-accuracy sweep across
flux's own tuning knobs, each validated against the REAL final output before trusting it:
- **ROBUST/LAGPOW/DETREND structure**: plain autocorrelation scored best on own-accuracy
  (77.9% vs shipped 73.3%) and even improved final correct+octave counts, but traded away
  coverage/caution — Daniel's explicit call was to KEEP lag-power+detrend (matches tilt's
  own structure) rather than take that tradeoff. Unchanged from 0.2.
- **FLUX_CLIP**: monotonic own-accuracy gain up to ~6.0-8.0, but did NOT survive the real
  final output (correct flat, "other" got worse) — NOT promoted, a classic case of
  own-accuracy not being the real metric. Unchanged from 0.2 (4.5).
- **FLUX_RECTIFY_MODE**: PROMOTED to full-wave (`fabsf(diff)` instead of `max(0,diff)`) —
  own-accuracy 73.3%→80.1%. Full-corpus final output: correct 14,667→14,810, octave_errors
  159→168, other 888→929, no_display 10,369→10,176 — the same coverage/caution tradeoff
  shape as the structure question above, but promoted anyway: spot-checked against both
  real disconnect/reconnect recordings and found a genuine, concrete fix rather than just a
  statistical tradeoff — full-wave **eliminates revision 0.2's own documented spurious
  160bpm lock** on `daniel_recovery_test.f32` entirely, and reaches the correct ~52bpm
  reading ~9.5s earlier on `daniel_offchest_test.f32` with zero wrong intermediate values.
  Promoted per Daniel's explicit "yes, promote it."

**2026-08-08 update (revision 0.2, same day as the rebase below):** `AGREEMENT_SIGNAL_FLUX`
promoted to default. Combo13's weighted-median-combine agreement bonus and early-release
streak previously trusted a "tilt" signal (a native-rate spectral-tilt feature via FFT)
exclusively since `combo13_opt`. Daniel recalled two older findings — the legacy JS
pipeline's own 2026-07-28 agreement-signal search, and a 2026-07-24 standalone-accuracy
comparison — both of which favored an envelope-flux signal (rectified first-difference of
the broadband envelope, z-scored+clipped) over tilt, but neither had ever been ported into
this C/Python lineage. Built as an A/B toggle (tilt's own code path untouched; flux is
fully separate new code reusing the already-streamed per-band envelope, autocorrelated
over the same lag range the main candidate uses, with the same lag-power+detrend treatment
tilt's own autocorrelation has). **Full 314-file corpus comparison: flux beat tilt on
every metric simultaneously** — correct 14,607→14,667, octave_errors 164→159, other
895→888, no_display 10,417→10,369. One honest caveat: on a real disconnect/reconnect test
recording, both tilt and flux hit an identical pre-existing spurious 160bpm lock; flux just
takes 2 extra ticks (0.5s) to self-correct — not a new failure mode. `STATE_SIZE_BYTES`
unchanged at 17,052 (flux adds no persistent state, only reuses existing per-tick scratch).
Promoted per Daniel's explicit "let's put flux in."

**2026-08-08 update (versioning rebase, revision 0.1):** "plan of record" is retired as this
project's informal label for "the current state" — from now on a plain revision
number (`COMBO13_VERSION` in `combo13_stream_internal.h`, queryable at runtime via
`c13stream_version()`) tracks it instead, bumped whenever the algorithm or its
memory footprint changes. This folder's own contents were also found to be quietly
stale at rebase time — missing 3 real, already-corpus-validated fixes that landed
later on 2026-08-07 (onset retired from the event-timing calculation entirely, the
tilt-autocorrelation lag-power+detrend promotion, and the early-release re-tune to
`EARLY_RELEASE_MIN_TICKS=2`/`EARLY_RELEASE_MAX_SAVE_S=5.0`) — none of which had
been synced into this handoff copy despite the changelog below reading as if they
had. Re-synced to the live source as part of this rebase; see project memory
(`project_pcg_embedded_c`) for full detail on those 3 fixes specifically.

**2026-08-05 update:** this folder's contents were updated in place to the current
plan of record, which now includes a time-to-first-display optimization on top of
the original "Pass 4" RAM/clock-reduction work (folder name is historical — the
content is current). If you need the exact prior version instead, it's preserved
at `embedded_c/combo13_stream_pass4_plan_of_record.c` (+ matching `.h`) in this
project's own source tree, not in this handoff folder.

**What changed:** the flat wait before the first reading can now shorten by up to
3.0s (retuned more aggressive 2026-08-06, from an original 1.5s) once the algorithm's
own growing-window estimate agrees with an independent cross-check (a separately-
derived pitch trace) for 3 consecutive ticks (~0.75s, retuned from 5/~1.25s) — this
targets ONLY the common case where the signal is already clean and unambiguous;
anything less certain gets exactly the same wait as before. Validated on a 314-file
real-recording corpus: mean time-to-first-display 7.66s→6.90s, median 7.25s→6.00s,
worst-case (hardest files) unchanged, and — the specific risk this was checked
against — zero new octave (half-rate/double-rate/triple-rate) errors introduced. An
even more aggressive tier (2 ticks/5.0s) was tried and rejected: it looked better on
every per-file metric but introduced 2 new triple-rate errors in the full corpus
tick-level check, invisible in any single-file spot check.

**2026-08-06 update:** fixed a real off-body-detection lag. The flatline detector
was sharing the same up-to-8-second window as the main estimate, so right after an
actual device removal it took several seconds (worst case observed: ~7s) for the
detector to reflect it, since the window still contained mostly pre-removal loud
signal — during that gap the main estimate could lock onto a spurious high reading
with nothing gating it. Fixed by computing the flatline statistic over the same
short (~2s) window the onset detector already used, which reacts to a removal
almost instantly. Validated on a real recording of the failure: wrong-value
duration cut from ~6.25s to ~1.25s, detection ~5s faster; 60-file corpus check
unchanged (bit-for-bit, since ordinary continuous recordings never exercise this
path); also incidentally fixed an unrelated known startup-struggle glitch as a
side effect of the same change. No public API or memory footprint change.

**2026-08-06 update (RAM consolidation):** `STATE_SIZE_BYTES` 26,788→20,356 (-6.4KB,
-24%). Traced 4 scratch buffers (used at different points within one tick) for
non-overlapping liveness — 3 of them (never alive at the same time as each other)
now share a single buffer sized to the largest individual need; the 4th
deliberately stays separate since it would alias with the shared buffer's own
internal use during one specific call. Validated bit-for-bit identical output
against every real/synthetic test file on disk, plus zero change on the 60-file
corpus check — a pure buffer-lifetime refactor, no algorithm change. **Real
tradeoff found on the clock-cycle side**: a higher-sample benchmark (150 repeats)
showed a consistent ~5% slower per-tick time on both real recordings after this
change, most likely from the sum-of-3-bands step now running as two smaller
accumulation loops instead of one combined one. Not the free win it looked like
on paper — a real, small RAM-for-clock-cycles trade.

**2026-08-07 update (promoted to `STREAM_VARIANT=4`, 400Hz decimation):** this
folder's contents were updated in place again — **the build flag changes** from
`-DSTREAM_VARIANT=2` to `-DSTREAM_VARIANT=4` (see Build section below), everything
else about the API is unchanged. Decimates to 400Hz/80Hz-frames instead of
500Hz/100Hz, with 4 constants retuned specifically for variant 4
(`ROBUST_TRIM_FRAC`, `LAG_POW`, `TEAL_JUMP_RATIO`+`TEAL_JUMP_CONFIRM_TICKS`,
`HARMONIC_VETO_LO_FRAC`/`HI_FRAC`) after a dedicated sweep. An initial
60-file-sample tuning result looked like a large win but was a false positive —
re-checked on the full 314-file corpus it actually cost 178 fewer correct
answers; a full-corpus leave-one-out pass found the real, corrected combination.
That corrected version genuinely beats the `STREAM_VARIANT=2` plan of record on
the full 314-file corpus: correct +5, octave errors -29, "other" -68, while using
~30% less RAM (`STATE_SIZE_BYTES` 18,780 vs 20,356) and running ~32% faster per
tick. Validated the same way as every prior update in this file: bit-identical
native-driver output on 2 real disconnect/reconnect recordings + 3 synthetic
recovery tests, plus the corpus numbers above. **If you need the exact prior
`STREAM_VARIANT=2` version instead**, it's preserved at
`embedded_c/combo13_stream_ram_consolidation_plan_of_record.c` (+ matching `.h`)
in this project's own source tree, not in this handoff folder.

**2026-08-07 update (RAM/clock pass 7, same day):** 3 further optimizations,
same build flag (`-DSTREAM_VARIANT=4`), no public API change. Each validated in
isolation against the full 314-file corpus before combining, so the cost of
each is known individually, not just as a bundle:
1. `TILT_MAX_FRAMES` trimmed 1200→1024 — this constant is the tilt trace's own
   effective analysis-window length (it isn't re-clamped to a fixed 8s
   elsewhere), so 1200 (10.2s) → 1024 (8.7s) is a real, if small, resolution
   cut, and also shrinks `scratch_shared` (sized to whichever of this or
   `FEAT_MAX_V` is larger — this one dominates). **The only one of the 3 with
   a measurable cost**: full-corpus correct 14,358→14,354, "other" 877→882,
   octave errors 166→165 (net wash on octave, tiny cost elsewhere) — one
   single-tick 0.6bpm blip observed on a real recording, still correctly
   classified.
2. `EXT_HI_MAX_V` (sizes `scratch_Sext`, the harmonic-veto's extended search
   buffer) made variant-4-specific: 350→270. The veto's own search-range
   formula (`ceil(HARMONIC_VETO_HI_EXT_MULT × cand_lag) + 2`) never exceeds
   264 for this variant's `LO_LAG`/`HI_LAG` — a hard-ceiling-proven
   tightening, confirmed bit-identical on the full corpus in isolation.
   Variants 1–3 keep the original shared 350 untouched.
3. `baseline_update()`'s `alpha=expf(...)` was recomputed twice per chunk
   (once for the artifact baseline, once for the median baseline, identical
   value both times) — hoisted to compute once per chunk instead. Bit-exact
   by construction; confirmed bit-identical on the full corpus in isolation.

Combined: `STATE_SIZE_BYTES` 18,780→17,052 (-9.2%). Validated the same way as
every prior update in this file: bit-identical or near-identical native-driver
output on the same 2 real recordings + 3 synthetic tests, plus the full-corpus
numbers above (run individually per change, then combined).

**2026-08-07 update (optional debug/visualization instrumentation, no algorithm
change):** `combo13_stream.c`/`combo13_stream_internal.h` gained a handful of
`#ifdef COMBO13_EXPOSE_DEBUG` blocks that snapshot a few intermediate signals
(the main autocorrelation curve, the native-rate tilt trace + its own
autocorrelation, the flatline detector's peak-minus-mean metric) into extra
struct fields — purely so an external tool (pcg_visualizer's browser
diagnostics) can render what the algorithm is actually doing internally. This
macro is **not defined** by the build command below, so none of this compiles
in for normal firmware use — confirmed `STATE_SIZE_BYTES` and all validation
numbers above are unchanged with the macro off. Safe to ignore entirely unless
you specifically want the same introspection in your own tooling.

## Files (all 3 required, no other dependencies beyond the standard library)

Filenames carry the revision number directly (2026-08-08, per Daniel's request) so a
copy pulled out of this folder stays traceable even without its containing directory
name — this is `v0_9_stream.*`, corresponding to `combo13_stream.*` in the live
project source tree (`pcg_abab/embedded_c/`), which keeps that name across revisions.

- `v0_9_stream.h` — public API, include this from your firmware code.
- `v0_9_stream_internal.h` — state struct layout, included by the .c file only.
- `v0_9_stream.c` — implementation.

## Build

Compile `v0_9_stream.c` with:

```
-DSTREAM_VARIANT=4
```

`STREAM_VARIANT` is a compile-time selector between 4 front-end designs that were
compared during development; **4** is the current plan of record (decimate to
400Hz/80Hz frames, then detect — see the 2026-08-07 update above for the tuning
and RAM/clock-pass detail). **2** (decimate to 500Hz/100Hz frames first) was the
prior plan of record, still fully supported by this same source file if you need
it instead — see the 2026-08-05 update above for where its own frozen snapshot
lives. No other build flags are required. Standard C99, no platform-specific
intrinsics.

## Memory

Static/stack state size: **11,076 bytes** (one `struct Combo13StreamState`,
returned by `c13stream_state_size()` at runtime for confirmation — no heap
allocation anywhere in this file).

## Public API (`v0_9_stream.h`)

```c
unsigned long c13stream_state_size(void);
const char *c13stream_version(void);  // e.g. "0.9"
void c13stream_init(Combo13StreamState *st);
void c13stream_push(Combo13StreamState *st, const float *samples, int n);
Combo13StreamTick c13stream_tick(Combo13StreamState *st);
```

- **`c13stream_init`** — zero-initializes state. Call once at startup (or on a full reset).
- **`c13stream_push`** — feed new audio samples as they arrive, in order, any chunk size.
  Samples are **raw, un-normalized PCM-scale floats** (i.e. the same scale as your ADC's
  native amplitude, e.g. int16-range values cast to float — do **not** pre-normalize to
  ±1.0, every tuned threshold in this port assumes raw scale).
- **`c13stream_tick`** — call once every **250ms** of audio pushed (the algorithm's
  internal timing assumes this cadence — `TICK_S=0.25f` in `combo13_stream.c`). Returns
  one `Combo13StreamTick` result per call.

```c
typedef struct {
    float t;                    // elapsed seconds since c13stream_init
    float bpm;                  // heart rate estimate, valid only if bpm_valid
    bool  bpm_valid;             // false while '--' should be displayed (no reading yet)
    bool  is_flatline;           // true = no signal / device off-body
    float raw_candidate_bpm;     // pre-confirmation candidate (diagnostic use)
    bool  raw_candidate_valid;
    int   harmonic_confirmed;    // 1=confirmed, 0=unconfirmed, -1=no opinion yet
} Combo13StreamTick;
```

Only `bpm` + `bpm_valid` + `is_flatline` are needed for a basic integration — treat
`bpm_valid==false` (or `is_flatline==true`) as "show no reading," not as an error.

## Minimal integration loop

```c
static Combo13StreamState st;   // ~10.8KB — put this in a section you control, not the stack
c13stream_init(&st);

// each time you have ~250ms worth of new audio samples:
c13stream_push(&st, new_samples, n);
Combo13StreamTick r = c13stream_tick(&st);
if (r.bpm_valid && !r.is_flatline) {
    // display r.bpm
} else {
    // display "--"
}
```

## Status

Corpus-validated (410-file real+self-recorded+synthetic test set) and validated
against 2 real disconnect/reconnect recordings during development. **Not yet
verified on real PSoC6 hardware** — validated on x86 (native C) and via WebAssembly
in a browser-based comparison tool only. Recommend a bring-up test against a known
audio capture before trusting live readings on-device.
