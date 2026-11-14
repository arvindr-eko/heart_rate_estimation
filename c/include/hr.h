/* Streaming heart rate from heart sounds (PCG): C port of the Python device reference
 * (src/device). C99, float32, no dynamic memory, no recursion; all state in one caller-owned
 * hr_state_t of hr_state_size() bytes (hr_blocks.h for static allocation). Sizes and
 * coefficients come from generated/hr_model.h (hr-estimation c-model). The API is shared with
 * the combo13 algorithm, so firmware can switch between the two; the fields marked combo13 are
 * filled as described for this estimator.
 *
 * Firmware (four calls; input at HR_FS_HZ, any number of samples per push):
 *   audio context, under the caller's lock:  hr_push_data; true when a block of
 *                                            HR_BLOCK_SAMPLES (50 ms) is complete: wake the
 *                                            processing task
 *   processing task:                         hr_process_chunk (no lock: estimator work on
 *                                            the complete blocks), hr_update_chunk (lock:
 *                                            release them), hr_calculate (display value)
 * Up to HR_RING_BLOCKS blocks wait between hr_process_chunk calls; when the processing task
 * falls further behind, input is dropped and a new measurement starts after the gap.
 * hr_push_data(s, NULL, n) marks n missing samples the same way.
 *
 * Direct (one context): hr_process_block for every block of HR_BLOCK_SAMPLES samples; it
 * returns 1 when a reading is due. Do not mix both patterns on one state.
 *
 * Readings: the first one when the estimate is confident enough (or at latest_s), then one per
 * output period (1 s) while a heartbeat is heard. HR_LOST_FRAMES frames in a row without
 * heartbeat (stethoscope lifted, contact lost) end the measurement: readings stop and a new
 * measurement starts by itself. hr_calculate shows a reading in whole BPM, smoothed (display
 * rule in stream.c), and nothing outside the reading phase. hr_get_reading, hr_get_frame_info
 * and hr_get_settings may be called at any time; hr_set_settings changes the runtime settings
 * from the next frame on (no retraining); hr_reset starts a new measurement on request and
 * keeps the settings. */
#ifndef HR_H
#define HR_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum hr_result {
    HR_OK = 0,
    HR_ERR_ARG = -1                 /* NULL pointer or setting out of range */
} hr_result_t;

typedef enum hr_phase {
    HR_PHASE_WARMUP = 0,            /* less than the minimum features window: no features,
                                       the estimate is the tracker's prior; also the frame
                                       that ended a measurement (restart) */
    HR_PHASE_SEARCHING = 1,         /* estimate updated every frame, first reading not yet due */
    HR_PHASE_READING = 2            /* first reading given; one reading per output period
                                       while a heartbeat is heard */
} hr_phase_t;

typedef enum hr_path {
    HR_PATH_REGULAR = 0,            /* regular rhythm at a common rate */
    HR_PATH_SLOW_IRREGULAR = 1      /* estimate below HR_SLOW_BPM or irregular picked beats */
} hr_path_t;

typedef struct hr_settings {
    float confidence;               /* regular hearts: probability within ±3 BPM required for
                                       the first reading, (0, 1]; lower is sooner */
    float slow_irregular_after_s;   /* slow or irregular hearts: earliest first reading, s */
    float slow_irregular_confidence; /* slow or irregular hearts from HR_SLOW_IRREGULAR_
                                        RELAXED_BLOCKS (4 s): probability within ±10 BPM
                                        required for the first reading, (0, 1]; higher is
                                        later and more accurate */
    float latest_s;                 /* heart sound present: first reading at this time when
                                       the estimate is roughly certain, s */
} hr_settings_t;

typedef struct hr_reading {
    float t_s;                      /* time since the measurement started (hr_init,
                                       hr_reset or the automatic restart), s */
    float bpm;                      /* heart rate, beats per minute; hr_calculate: the shown
                                       value in whole BPM, -1 when none */

    /* ML specific readings */
    float confidence;               /* calibrated probability of the frame's path */
    uint8_t path;                   /* hr_path_t of the frame */
    uint8_t first;                  /* 1 for the first reading of the measurement */

    /* Combo13 specific readings; this estimator: */
    bool bpm_valid;                 /* a value is shown (hr_calculate); 1 (hr_process_block) */
    bool is_flatline;               /* 0 */
    float raw_candidate_bpm;        /* latest reading before the display rule */
    bool raw_candidate_valid;       /* a reading was given in this measurement */
    int harmonic_confirmed;         /* -1 */
} hr_reading_t;

typedef struct hr_frame_info {      /* state of the latest frame (every HR_FRAME_BLOCKS blocks) */
    float t_s;
    float estimate_bpm;             /* tracker estimate, also before the first reading */
    float frame_bpm;                /* best state of the frame alone; NaN without features */
    float periodicity;              /* activation ACF at the estimate's period (gate) */
    float activation_sd;            /* activation standard deviation over the window (gate) */
    float interval_cv;              /* picked-beat interval CV; NaN when undefined */
    float beat_bpm;                 /* beat-count heart rate; NaN when undefined */
    float p_fine;                   /* probability within ±3 BPM (regular path) */
    float p_rough;                  /* probability within ±10 BPM (slow/irregular path) */
    uint8_t path;                   /* hr_path_t */
    uint8_t phase;                  /* hr_phase_t */
    uint8_t heart_present;          /* 1: periodicity >= HR_MIN_PERIODICITY and activation SD
                                       >= HR_MIN_ACTIVATION_SD (the first-reading gates) */
} hr_frame_info_t;

typedef struct hr_state hr_state_t; /* block states (hr_blocks.h; fields are private) */

/* Defaults of configs/device.yml (readout). */
void hr_default_settings(hr_settings_t *settings);

/* Clear all state; settings NULL: defaults. HR_ERR_ARG on NULL state or invalid settings. */
hr_result_t hr_init(hr_state_t *s, const hr_settings_t *settings);

/* New measurement on request: clear all block states, keep the settings and the queued input.
 * Contact loss needs no call (automatic restart). */
void hr_reset(hr_state_t *s);

/* Validate and apply runtime settings; they take effect at the next frame. */
hr_result_t hr_set_settings(hr_state_t *s, const hr_settings_t *settings);
void hr_get_settings(const hr_state_t *s, hr_settings_t *settings);

/* Audio context: queue n samples (NULL: n samples missing, new measurement after them); true
 * when at least one block was completed. */
bool hr_push_data(hr_state_t *s, const int16_t *samples, int n);

/* Processing task: run the estimator on every block complete at the call; number of blocks. */
int hr_process_chunk(hr_state_t *s);

/* Processing task, under the lock of hr_push_data: release the processed blocks; number of
 * complete blocks still queued. */
int hr_update_chunk(hr_state_t *s);

/* Shown value: 1 and *out filled with bpm_valid set when a value is shown, else 0 (out may be
 * NULL; bpm -1 when none). */
int hr_calculate(hr_state_t *s, hr_reading_t *out);

/* Process one block of HR_BLOCK_SAMPLES samples; 1 and *out filled when a reading is due,
 * else 0 (out may be NULL). */
int hr_process_block(hr_state_t *s, const int16_t *pcm, hr_reading_t *out);

/* Latest reading; 0 before the first reading. */
int hr_get_reading(const hr_state_t *s, hr_reading_t *out);

/* Latest frame: estimate, gates, rhythm, probabilities, phase. */
void hr_get_frame_info(const hr_state_t *s, hr_frame_info_t *info);

/* Posterior over the HR_N_STATES heart-rate states of the latest frame. */
const float *hr_get_posterior(const hr_state_t *s);

/* Software version (HR_VERSION, semantic versioning) and model name with weight checksums
 * (HR_MODEL_ID). */
const char *hr_version(void);
const char *hr_model_id(void);

/* sizeof(hr_state_t), for callers that allocate the state without hr_blocks.h. */
uint32_t hr_state_size(void);

#ifdef HR_DEBUG
/* Intermediate outputs of the latest block or frame, for comparison with the reference. */
const float *hr_debug_band_energy(const hr_state_t *s);   /* [HR_FE_OUT][HR_ENV_PER_BLOCK] */
const float *hr_debug_activation(const hr_state_t *s);    /* [HR_ENV_PER_BLOCK] */
const float *hr_debug_features(const hr_state_t *s);      /* [HR_N_STATES][HR_N_IN] */
const float *hr_debug_log_emission(const hr_state_t *s);  /* [HR_N_STATES] */
int hr_debug_has_features(const hr_state_t *s);            /* features of the latest frame */
#endif

#ifdef __cplusplus
}
#endif

#endif /* HR_H */
