/* Streaming heart rate from heart sounds (PCG): C port of the Python device reference
 * (src/device). C99, float32, no dynamic memory, no recursion; all state in one caller-owned
 * hr_state_t. Sizes and coefficients come from build/generated/hr_model.h
 * (hr-estimation c-model).
 *
 * Call pattern: hr_init once, then hr_process_block for every block of HR_BLOCK_SAMPLES int16
 * samples at HR_FS_HZ (50 ms). It returns 1 when a reading is due: the first one when the
 * estimate is confident enough (or at latest_s), then one per output period (1 s) while a
 * heartbeat is heard; without heartbeat no new reading comes. HR_LOST_FRAMES frames in a row
 * without heartbeat (stethoscope lifted, contact lost) end the measurement: readings stop and
 * a new measurement starts by itself, with a new first reading once a heart is heard again.
 * hr_get_reading, hr_get_frame_info and hr_get_settings may be called at any time;
 * hr_set_settings changes the runtime settings from the next frame on (no retraining); hr_reset
 * starts a new measurement on request and keeps the settings. */
#ifndef HR_H
#define HR_H

#include <stdint.h>

#include "hr_model.h"

typedef enum {
    HR_OK = 0,
    HR_ERR_ARG = -1                 /* NULL pointer or setting out of range */
} hr_result_t;

typedef enum {
    HR_PHASE_WARMUP = 0,            /* less than the minimum features window: no features,
                                       the estimate is the tracker's prior; also the frame
                                       that ended a measurement (restart) */
    HR_PHASE_SEARCHING = 1,         /* estimate updated every frame, first reading not yet due */
    HR_PHASE_READING = 2            /* first reading given; one reading per output period
                                       while a heartbeat is heard */
} hr_phase_t;

typedef enum {
    HR_PATH_REGULAR = 0,            /* regular rhythm at a common rate */
    HR_PATH_SLOW_IRREGULAR = 1      /* estimate below HR_SLOW_BPM or irregular picked beats */
} hr_path_t;

typedef struct {
    float confidence;               /* regular hearts: probability within ±3 BPM required for
                                       the first reading, (0, 1]; lower is sooner */
    float slow_irregular_after_s;   /* slow or irregular hearts: earliest first reading, s */
    float latest_s;                 /* heart sound present: first reading at this time when
                                       the estimate is roughly certain, s */
} hr_settings_t;

typedef struct {
    float t_s;                      /* time since the measurement started (hr_init,
                                       hr_reset or the automatic restart), s */
    float bpm;                      /* heart rate, beats per minute */
    float confidence;               /* calibrated probability of the frame's path */
    uint8_t path;                   /* hr_path_t of the frame */
    uint8_t first;                  /* 1 for the first reading of the measurement */
} hr_reading_t;

typedef struct {                    /* state of the latest frame (every HR_FRAME_BLOCKS blocks) */
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

#include "hr_blocks.h"              /* hr_state_t: block states (fields are private) */

/* Defaults of configs/device.yml (readout). */
void hr_default_settings(hr_settings_t *settings);

/* Clear all state; settings NULL: defaults. HR_ERR_ARG on NULL state or invalid settings. */
hr_result_t hr_init(hr_state_t *s, const hr_settings_t *settings);

/* New measurement on request: clear all block states, keep the settings. Contact loss needs
 * no call (automatic restart). */
void hr_reset(hr_state_t *s);

/* Validate and apply runtime settings; they take effect at the next frame. */
hr_result_t hr_set_settings(hr_state_t *s, const hr_settings_t *settings);
void hr_get_settings(const hr_state_t *s, hr_settings_t *settings);

/* Process one block of HR_BLOCK_SAMPLES samples; 1 and *out filled when a reading is due,
 * else 0 (out may be NULL). */
int hr_process_block(hr_state_t *s, const int16_t pcm[HR_BLOCK_SAMPLES], hr_reading_t *out);

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

/* sizeof(hr_state_t), for callers that allocate the state without the header (tests). */
uint32_t hr_state_size(void);

#ifdef HR_DEBUG
/* Intermediate outputs of the latest block or frame, for comparison with the reference. */
const float *hr_debug_band_energy(const hr_state_t *s);   /* [HR_FE_OUT][HR_ENV_PER_BLOCK] */
const float *hr_debug_activation(const hr_state_t *s);    /* [HR_ENV_PER_BLOCK] */
const float *hr_debug_features(const hr_state_t *s);      /* [HR_N_STATES][HR_N_IN] */
const float *hr_debug_log_emission(const hr_state_t *s);  /* [HR_N_STATES] */
int hr_debug_has_features(const hr_state_t *s);            /* features of the latest frame */
#endif

#endif
