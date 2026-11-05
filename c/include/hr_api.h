/*
 * hr_api.h: device API of the PCG heart rate estimator (draft for the PSoC 6 CM4 port).
 *
 * Mirrors pcg_hr.estimator: one call per 50 ms block of 200 PCG samples at 4000 Hz. All state
 * is in a caller-provided hr_state_t (static allocation, no malloc). Constants are generated
 * from config.yml by the port's code generator; the values below are those of config.yml
 * (final candidate) and the state size of the cost model (float32).
 */
#ifndef HR_API_H
#define HR_API_H

#include <stdint.h>

#define HR_FS_HZ             4000    /* PCG sample rate */
#define HR_BLOCK_SAMPLES     200     /* samples per hr_process_block call (50 ms) */
#define HR_FRAME_BLOCKS      5       /* tracker update every 5 blocks (0.25 s) */
#define HR_STATE_BYTES       13312   /* >= cost-model RAM (12848 B float32); set by codegen */

typedef enum {
    HR_STATUS_MEASURING = 0,         /* no reading yet */
    HR_STATUS_READING = 1            /* out->bpm holds the current reading */
} hr_status_t;

/* Runtime parameters (config.yml: release). Defaults from hr_default_params(). */
typedef struct {
    float default_s;                 /* answer cutoff for HR >= low_hr_bpm (3.0; fast 2.5) */
    float low_hr_s;                  /* answer cutoff when the heart may be slower (6.0) */
    float max_s;                     /* always answer by this time (8.0) */
    float low_hr_bpm;                /* slow-heart boundary (50) */
    float low_mass_min;              /* posterior mass below low_hr_bpm: "may be slow" (0.3) */
    float c_early;                   /* confidence needed before the cutoff (0.97; fast 0.95) */
    float c_cutoff;                  /* confidence at the cutoff, falling to 0 at max_s (0.9) */
    float output_period_s;           /* reading interval after the first reading (1.0) */
} hr_params_t;

typedef struct {
    hr_status_t status;
    uint8_t new_reading;             /* 1 when this call produced a reading */
    float bpm;                       /* heart rate, valid when status == HR_STATUS_READING */
    float confidence;                /* tracker posterior mass within ±5 % of the estimate */
    float t_s;                       /* time since hr_init / hr_reset */
} hr_output_t;

typedef struct {
    _Alignas(8) uint8_t opaque[HR_STATE_BYTES];
} hr_state_t;

/* Fill params with the config.yml defaults. */
void hr_default_params(hr_params_t *params);

/* Initialise state for a new measurement (good contact detected). */
void hr_init(hr_state_t *state, const hr_params_t *params);

/* Restart the measurement after contact loss; keeps params. */
void hr_reset(hr_state_t *state);

/*
 * Consume one block of int16 PCG (scaled as pcm / 32768). Returns 1 and fills out when a
 * reading is due (first reading per the release rule, then every output_period_s), else 0.
 * Every HR_FRAME_BLOCKS-th call carries the frame work (features, scorer, tracker): about
 * 25 ms at 150 MHz in float32 per the cost model; the other calls about 0.4 ms.
 */
int hr_process_block(hr_state_t *state, const int16_t pcm[HR_BLOCK_SAMPLES], hr_output_t *out);

#endif /* HR_API_H */
