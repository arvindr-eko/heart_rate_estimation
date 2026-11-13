**Heart-rate firmware: changes since 0.14**

What was wrong, what we changed, and where to find it in the code

Prepared 2026-10-19 for Eko firmware review. Base: coral-fw branch feature/dbeitel/fw-985-integrate_new_heart_rate_algorithm (zip received 2026-10-12), heart-rate algorithm revision 0.14. All builds are dev-key signed, bench-only images. Nothing here has been through formal verification and none of it is a release approval.

# **1. Summary**

Four changes were made to the heart-rate (HR) code since 0.14. Two fix problems found on hardware (reboots, HR going blank), one extends the algorithm to lower heart rates, and one makes the reported heart rate more precise. Section 2 explains how the algorithm works, with just enough detail to follow the rest. Sections 3 to 6 each cover one change in the same order: the problem, why it happens, the fix, and where the fix is in the code.

| **\#** | **Problem** | **Fix** | **Code** | **Status** |
|----|----|----|----|----|
| 1 | The device reboots at random while connected over Bluetooth | Make the HR task sleep 20 ms after each run | audio_io_task.c lines 83, 921 | Confirmed on hardware: no reboots over multi-hour runs |
| 2 | Some HR calculations take 2-5 s instead of well under 0.5 s, and HR sometimes goes blank | Replace a slow search routine with a fast one that gives identical results | combo13_stream.c line 233 (used at 272, 604) | Identical output on 836 test files; dropouts gone on bench |
| 3 | Heart rates below 40 BPM cannot be reported | Algorithm 0.14 to 0.16: 30 BPM floor, retrained templates | combo13_stream_internal.h, both template headers, combo13_stream.c | Tested on host; runs on hardware |
| 4 | Reported BPM jumps in coarse steps at high heart rates (up to 9.5 BPM near 200) | Estimate the heart rate between steps (interpolation) | combo13_stream.c lines 1158-1176 | Device sweep: closer to truth (within 3 BPM: 83.8% to 86.3%); within 5 BPM unchanged |

**How sure are we?** Reboot cause and fix: high, because it was observed directly on the device and 0.14 rebooted too. Slow search routine as a defect: high, because it was reproduced on a PC and the fix gives identical output. That this defect caused the HR dropouts: medium, because it fits all the evidence but the exact moment of an hours-long failure was never captured. Benefit of interpolation: moderate, because there is one device run per build (section 6).

# **2. Background: how the heart rate is calculated**

The device listens to heart sounds and reports beats per minute (BPM). The algorithm, called combo13, runs once every 0.5 s on the most recent audio, up to 8 s of it. Each run is called a **tick**. A tick does five things:

> **Step 1. Reduce the audio.** The 4,000 Hz audio is filtered and reduced to a slow signal that rises with each heart sound. This signal has 66.67 values per second (called FFS). One value is a **frame**.
>
> **Step 2. Compare the signal with delayed copies of itself (autocorrelation).** A steady heartbeat repeats, so the signal matches itself best when the delay equals the time between beats. The delay is the **lag**, counted in frames. The code scores each lag from 20 to about 146, giving a curve of score against lag.
>
> **Step 3. Convert lag to BPM.** BPM = 60 x FFS / lag = 4000 / lag. Lag 20 is 200 BPM and lag 40 is 100 BPM. Because lag is a whole number of frames, only certain BPM values are possible (this matters in section 6).
>
> **Step 4. Pick the right peak.** The score curve has several peaks, including peaks at half and double the true rate. The algorithm compares the curve with stored **templates** (typical curve shapes for each BPM range, 34 ranges in 0.16) to decide which peak is the heart rate.
>
> **Step 5. Check and smooth.** Guards reject sudden jumps and implausible values, a weighted median combines recent results, and the first value is shown only after about 6 s of settling.

In the firmware, a FreeRTOS task (AudioHRTask) runs one tick whenever 0.5 s of audio has arrived. Audio is pushed in by the audio task. If the HR task falls behind, the 0.6 s input buffer fills and new audio is dropped ("chunk overflow").

| **Term** | **Meaning** |
|----|----|
| Tick | One run of the algorithm, every 0.5 s of audio. |
| Frame, FFS | One value of the reduced signal; FFS = 66.67 frames per second. |
| Lag | A delay, in frames. BPM = 4000 / lag. |
| Template | A stored curve shape for a BPM range, used to choose between candidate peaks. |
| Quickselect | A routine that finds the n-th smallest value in a list without fully sorting it. Used to ignore outliers when averaging. |
| Watchdog, idle hook | The watchdog resets the device if not "kicked" within 6 s. The kick happens only in the FreeRTOS idle hook, which runs when no other task is ready. |
| Chunk overflow | Audio dropped because the HR task has not consumed the input buffer in time. |

# **3. Random reboots (change 1)**

## **Problem**

The device reset at random while paired over Bluetooth, usually within about a minute of pairing. Unpaired it did not reboot. 0.14 did the same. The console showed Reset reason: 0x1, which means the hardware watchdog reset the device.

## **Why it happens**

- The watchdog is kicked only from the idle hook. If the idle task never gets to run for 6 s, the device resets.

- As the analysis window grows toward 8 s, one tick takes 0.9-1.2 s, which is longer than the 0.5 s between ticks. The next tick is already waiting when the previous one finishes, so the HR task never goes to sleep.

- The HR task has the same priority as the App and System tasks, and the high-priority audio task already uses about 65% of the CPU. The idle task gets no time, and 6 s later the watchdog fires.

- Evidence: a debug sampler printed Task running while un-kicked: audio_hr (silent 6073 ms), and the CPU profile showed idle at 0-1% during HR bursts. That Bluetooth load makes this worse (reboots only when paired) is inferred, not measured.

## **Fix**

At the end of every AudioHRTask run, sleep for 20 ms so the idle task always gets a turn. In audio_io_task.c:

> \#define HR_TASK_YIELD_MS 20 /\* line 83 \*/
>
> ...
>
> vTaskDelay(pdMS_TO_TICKS(HR_TASK_YIELD_MS)); /\* line 921 \*/

This does not make HR faster. If HR falls behind, the existing overflow path drops audio instead of resetting the device. **Limit:** the sleep only helps between ticks. A single tick longer than about 6 s would still reset the device, and logs showed a 5.3 s tick. Section 4 is about that.

# **4. Slow ticks and HR going blank (change 2)**

## **Problem**

After the reboots stopped, HR would work for minutes to hours and then show nothing while the device stayed connected. A build with extra debug output showed:

- The algorithm state was healthy (no invalid numbers), and HR values kept coming back, so it was not stuck.

- Some ticks took **2-5.3 s**, mostly during or just after quiet input. During these, idle ran 0% of the time and about 140 s of audio was dropped within ten minutes.

- The worst tick, 5.3 s, was within 0.7 s of the watchdog limit.

## **Why it happens**

In step 2 the algorithm uses a "trimmed" average that ignores outlier values. For each of about 126 lags it uses quickselect twice (about 250 times per tick) to find the cut-off values. The quickselect code used a simple partition method that is fast on varied data but very slow when many values are equal: the work grows with the square of the list length instead of in proportion to it. Many equal values are normal here. Silence gives all zeros, and clipped or 16-bit values repeat even in ordinary signals.

## **Fix**

Replace the partition with a three-way version that groups equal values together, which stays fast when values repeat. combo13_stream.c, function quickselectf_range() at line 233, called at lines 272 and 604. The old partitionf() is removed. Old and new code were run on all 836 held-out test files and **no output value differed (maximum difference 0.0)**, so this changes speed only.

| **Time per tick on a PC, median / worst (ms)** | **Before** | **After**   |
|------------------------------------------------|------------|-------------|
| Noise input                                    | 3.0 / 9.2  | 0.87 / 3.95 |
| Exact zeros (silence)                          | 6.7 / 63.1 | 0.79 / 3.41 |
| Values of only -1, 0, +1                       | 5.0 / 39.7 | 0.58 / 3.43 |

PC milliseconds are not device milliseconds. The ratios matter: typical ticks are 3.5-9x faster and worst cases 2.3-18x faster.

## **Why we think this explains the blank HR**

- During slow ticks the HR task could not keep up, so audio was dropped. A PC simulation shows the display only degrades when 60% or more of the audio is lost (87% of the time displayed at 60% loss, 69% at 80%), which matches these episodes. Short gaps do not stop HR: cutting 4.4 s of audio every 12 s left 98.6% of ticks displayed.

- After the fix the bench shows no reboots and no dropouts. No post-fix log has been reviewed, so this is qualitative.

## **What is still unexplained**

- One long-run capture in the "no HR" state looked healthy at that moment (idle 15-24%, ticks about 100 ms, no dropped audio). The cause of that specific moment is unknown.

- A separate, unchanged behaviour can also hold the display off: a loud-spike detector restarts a 6-9 s settling wait on every hit, so a very peaky signal can keep the display blank. It exists in 0.14 too and is uncommon on our test set (98.8% of clips display in the PC simulation).

# **5. Lower heart rates: algorithm 0.14 to 0.16 (change 3)**

## **Problem**

0.14 only scores lags up to 100 (40 BPM), so heart rates from 30 to 40 BPM cannot be found. Its templates were also built from an older set of recordings.

## **Fix and where it is**

- **Longer lag range.** Scoring now covers lags up to about 146 (30 BPM). In combo13_stream_internal.h: HI_LAG (line 55), S_LEN (line 58, 80 to 126), EXT_HI_MAX (line 76, 227 to 303).

- **Old range untouched.** Lags 20-99 (FAST_HI_LAG, FAST_LEN, lines 62-63) are scored exactly as in 0.14. Results at 40 BPM and above are identical to 0.14 unless the slow override below fires.

- **Slow override.** estimate_detrend_from_mw() (combo13_stream.c line 727) also looks for a 30-40 BPM peak. In c13stream_tick() (line 1902) that peak replaces the normal pick only if it is at least SLOW_RATIO_MIN times taller than the tallest normal peak. SLOW_RATIO_MIN is at line 68 and changed from 1.5 to 1.35.

- **New templates.** autocorr_templates.h and autocorr_templates_detrended.h hold 34 BPM ranges (one for 30-40 BPM, then every 5 BPM from 40 to 200), built from 3,049 recordings. They are generated tables; replace them whole.

| **PC test, 836 held-out files**          | **0.14** | **0.16** |
|------------------------------------------|----------|----------|
| Ticks with the correct BPM               | 59.1%    | 60.2%    |
| Ticks with a wrong BPM                   | 8.0%     | 7.8%     |
| Files that never reach an accurate value | 3.7%     | 1.4%     |
| 30-40 BPM files (only 17), correct ticks | 0%       | 40%      |

Resource impact: algorithm state 16,112 to 16,600 bytes of RAM, code size 17,595 to 19,371 bytes, c13stream_tick stack frame 192 to 200 bytes. **CPU use on the PSoC6 was not measured.** The slow-range result rests on 17 files and is indicative only.

# **6. More precise heart rate: interpolation (change 4)**

## **Problem**

Because lag is a whole number of frames, BPM = 4000 / lag can only take certain values, and the gaps get wider at high rates. Lag 21 is 190.5 BPM and lag 20 is 200 BPM, so nothing in between can be reported. The gap between neighbouring values is about BPM x BPM / 4000: 2.5 BPM near 100, 5.7 near 150, 8 near 180. At 180 BPM and above, rounding alone uses about 4 of the 5 BPM of allowed error. When error is plotted against true BPM this shows up as a saw-tooth.

## **Fix**

The true peak usually lies between two lags. We fit a parabola through the winning score and its two neighbours, find where the parabola peaks, and compute BPM from that fractional lag. For example, a peak at lag 40.3 gives 99.3 BPM instead of 100.

**This changes only the number shown.** Scoring and the choice of peak still use whole lags, so which peak wins does not change. If the parabola is flat or inverted, or the shift is 0.5 lag or more, the whole lag is used.

combo13_stream.c, function template_match_gated_pick_v2() (starts line 1128), lines 1158-1176. bins\[best_idx\] is the winning lag position and win_teal-\>Sw is the score curve:

> int b = bins\[best_idx\];
>
> float lag = (float)(LO_LAG + b); /\* whole-lag estimate \*/
>
> if (b \>= 1 && b + 1 \< S_LEN) {
>
> float ym = win_teal-\>Sw\[b - 1\], y0 = win_teal-\>Sw\[b\], yp = win_teal-\>Sw\[b + 1\];
>
> float den = ym - 2.0f \* y0 + yp; /\* curvature \*/
>
> if (den \< -1e-12f) { /\* a real peak \*/
>
> float d = 0.5f \* (ym - yp) / den; /\* shift of peak, in lags \*/
>
> if (d \> -0.5f && d \< 0.5f) lag += d;
>
> }
>
> }
>
> \*out_bpm = 60.0f \* FFS / lag;

Cost: about 116 bytes of code and one floating-point division per tick. The file compiles with 0 warnings under -Wall -Wextra -Wshadow -Werror=double-promotion -Werror=vla (arm-gcc 14.2, cortex-m4 hard-float).

## **Results**

PC test (836 files): final value within 5 BPM of truth 93.5% to 94.0%; 160-180 BPM files 88.5% to 93.6%; 140-160 BPM files 92.2% to 90.5%. Device test: the same 400 recordings played to the same device, once before (2026-10-15) and once after (2026-10-19):

| **400-clip device test**              | **Before**  | **After**   | **Change**   |
|---------------------------------------|-------------|-------------|--------------|
| Final value within 5 BPM (of all 400) | 89.5% (358) | 89.5% (358) | none         |
| Final value within 3 BPM (of all 400) | 83.8% (335) | 86.3% (345) | +2.5 points  |
| Within 1 BPM (of clips with a value)  | 58.2%       | 69.3%       | +11.1 points |
| Within 2 BPM (of clips with a value)  | 79.0%       | 88.2%       | +9.3 points  |
| Average error (BPM)                   | 3.01        | 2.74        | -0.27        |
| Average error, 140-180 BPM files      | 3.41        | 2.86        | -0.55        |
| Clips that never showed a value       | 25          | 24          | -1           |
| Time to first correct reading, median | 6.92 s      | 7.22 s      | +0.30 s      |

- **Summary:** the values are closer to the truth, but the pass rate at 5 BPM did not change. 8 clips moved from fail to pass and 8 from pass to fail, all within 1-2 BPM of the 5 BPM limit. At 3 BPM, 10 more clips pass.

- **Limits:** there is one run per build, so run-to-run noise is unknown. Both builds report software revision 3bc27c1-dirty; tell them apart by date. The new build is confirmed by values such as 193 and 194 BPM that were stuck at 190 before.

- **Timing:** the new run shows the first reading about 0.3 s later. Interpolation only changes the displayed number, so this probably comes from the test setup (the two runs used different host computers), not the algorithm.

- **Not fixed:** clips at 193-197 BPM still read about half the true rate, and files above 180 BPM pass only 72% of the time. One untested idea: the peak search starts one lag too late, so lag 20 (200 BPM) can never be chosen and the highest reportable rate is about 190.5 BPM.

# **7. Files to apply**

Five source files, two .c and three .h, replace the same-named files in the received tree. The handoff folder HR_0.16_handoff\\ holds them under heartrate/ and fw/.

| **File** | **Path in repo** | **Contains** |
|----|----|----|
| combo13_stream.c | coral/modus/coral/source/heartrate/ | Changes 2, 3 and 4 |
| combo13_stream_internal.h | coral/modus/coral/source/heartrate/ | Change 3 (lag range, constants) |
| autocorr_templates.h | coral/modus/coral/source/heartrate/ | Change 3 (generated table) |
| autocorr_templates_detrended.h | coral/modus/coral/source/heartrate/ | Change 3 (generated table) |
| audio_io_task.c | coral/modus/coral/source/ | Change 1 (20 ms sleep) |

No other repo file needs to change. combo13_stream.h, heart_rate.c/.h, the Makefile and FreeRTOSConfig.h are untouched in the clean build. The debug builds edited some of them for diagnostics, and those edits are not part of this handoff.

Review aids in the same folder: combo13_stream.c.patch, combo13_stream_internal.h.patch and audio_io_task.c.patch (differences against the received 0.14 tree), original_0.14/ (the originals), host_test/ (a PC test driver) and README_FOR_FW_DEV.md. At start-up the debug line Heart rate algorithm version: should read 0.16. COMBO13_VERSION (combo13_stream_internal.h line 49) was not changed for the interpolation; change it if you want that build identifiable.

# **8. Open items and recommendations**

- **Measure CPU on the PSoC6.** The audio task already uses about 65%. Worst-case HR tick time for 0.14 and 0.16 has not been measured on the device.

- **Add a regression test** that feeds all-zero, constant, clipped and -1/0/+1 input to the PC build and checks time per tick. The existing tests never used silent input, which is why the slow search routine went unnoticed.

- **Check other code trees for the same partition routine.** In the folders searched it appeared only in the firmware copies of combo13_stream.c.

- **Review the watchdog design.** Kicking only from the idle hook means the whole device resets when any task runs long. Having each critical task check in, with a timer doing the kick, would reset only when a critical task misses its own deadline. This is a suggestion and has not been tested.

- **Before any release candidate:** remove the debug code, confirm the HR task stack is large enough (it is 1 KB; 70-78 of 256 words were free), and put the changes through normal change control as a medical-device change.

Caveats: apart from the device test and the on-device CPU readings, timing and accuracy numbers come from PC runs. Test-set labels come from ECG on DUO3 hardware only. The device test plays the 400-file test set with 30 s gaps.
