/* Firmware calls (hr.h): input ring between the audio context and the processing task, and the
 * display value (src/device/readout.py, display_step). hr_push_data fills blocks of
 * HR_BLOCK_SAMPLES in a ring of HR_RING_BLOCKS; hr_process_chunk runs hr_process_block on the
 * blocks complete at the call, in order, and updates the display after each; hr_update_chunk
 * releases them. One producer (hr_push_data) and one consumer (hr_process_chunk): the producer
 * writes only free slots and publishes a block by incrementing blocks_written; the consumer
 * frees slots only in hr_update_chunk, called under the producer's lock. A full ring drops the
 * input; the next complete block then starts a new measurement (hr_reset). */
#include "hr_blocks.h"

/* Shown value after a new reading of bpm (whole BPM): the first as is; within
 * HR_DISPLAY_NEAR_BPM as is; further, halfway, by HR_DISPLAY_NEAR_BPM to HR_DISPLAY_JUMP_BPM;
 * beyond HR_DISPLAY_JUMP_BPM only once the next reading agrees with it. */
static int16_t smooth(hr_state_t *s, int16_t bpm)
{
    if (s->shown_bpm == HR_DISPLAY_NONE) {
        s->pending_bpm = HR_DISPLAY_NONE;
        return bpm;
    }
    const int gap = bpm - s->shown_bpm, size = gap < 0 ? -gap : gap;
    if (size > HR_DISPLAY_JUMP_BPM) {
        const int to_pending = bpm - s->pending_bpm;
        const int confirmed = s->pending_bpm != HR_DISPLAY_NONE
                              && (to_pending < 0 ? -to_pending : to_pending) <= HR_DISPLAY_JUMP_BPM;
        s->pending_bpm = bpm;
        if (!confirmed)
            return s->shown_bpm;
    } else {
        s->pending_bpm = HR_DISPLAY_NONE;
    }
    if (size <= HR_DISPLAY_NEAR_BPM)
        return bpm;
    int step = size / 2;
    step = step < HR_DISPLAY_NEAR_BPM ? HR_DISPLAY_NEAR_BPM : step;
    step = step > HR_DISPLAY_JUMP_BPM ? HR_DISPLAY_JUMP_BPM : step;
    return (int16_t)(s->shown_bpm + (gap > 0 ? step : -step));
}

/* Display after one block: the new reading (NULL: none) smoothed; nothing outside the reading
 * phase. */
static void display_update(hr_state_t *s, const hr_reading_t *r)
{
    if (r) {
        int whole = (int)(r->bpm + 0.5f);
        whole = whole > HR_DISPLAY_MAX_BPM ? HR_DISPLAY_MAX_BPM : whole;
        s->shown_bpm = smooth(s, (int16_t)whole);
    }
    if (s->info.phase != HR_PHASE_READING)
        s->shown_bpm = s->pending_bpm = HR_DISPLAY_NONE;
}

/* Audio context: copy samples into the ring; true when a block was completed. */
bool hr_push_data(hr_state_t *s, const int16_t *samples, int n)
{
    bool completed = false;
    if (s == 0 || n <= 0)
        return false;
    if (samples == 0) {
        s->ring_fill = 0;
        s->gap_pending = 1;
        return false;
    }
    while (n > 0) {
        if (s->blocks_written - s->blocks_read >= HR_RING_BLOCKS) {
            /* processing task behind: drop the input, new measurement after it */
            s->ring_fill = 0;
            s->gap_pending = 1;
            return completed;
        }
        const uint32_t slot = s->blocks_written % HR_RING_BLOCKS;
        int take = HR_BLOCK_SAMPLES - s->ring_fill;
        take = take < n ? take : n;
        for (int i = 0; i < take; i++)
            s->ring[slot][s->ring_fill + i] = samples[i];
        s->ring_fill = (uint16_t)(s->ring_fill + take);
        samples += take;
        n -= take;
        if (s->ring_fill == HR_BLOCK_SAMPLES) {
            s->ring_restart[slot] = s->gap_pending;
            s->gap_pending = 0;
            s->ring_fill = 0;
            s->blocks_written++;
            completed = true;
        }
    }
    return completed;
}

/* Processing task: estimator and display on every block complete at the call. */
int hr_process_chunk(hr_state_t *s)
{
    if (s == 0)
        return 0;
    const uint32_t end = s->blocks_written;
    int processed = 0;
    for (; s->blocks_done != end; s->blocks_done++, processed++) {
        const uint32_t slot = s->blocks_done % HR_RING_BLOCKS;
        hr_reading_t r;
        if (s->ring_restart[slot])
            hr_reset(s);
        display_update(s, hr_process_block(s, s->ring[slot], &r) ? &r : 0);
    }
    return processed;
}

/* Processing task, under the producer's lock: free the processed slots. */
int hr_update_chunk(hr_state_t *s)
{
    if (s == 0)
        return 0;
    s->blocks_read = s->blocks_done;
    return (int)(s->blocks_written - s->blocks_read);
}

/* Shown value with the latest reading's details. */
int hr_calculate(hr_state_t *s, hr_reading_t *out)
{
    if (s == 0)
        return 0;
    const int shown = s->shown_bpm != HR_DISPLAY_NONE;
    if (out) {
        *out = s->last;
        if (!s->has_reading) {
            out->confidence = out->raw_candidate_bpm = 0.0f;
            out->path = out->first = 0;
        }
        out->t_s = (float)(s->n_blocks * HR_BLOCK_SAMPLES) / (float)HR_FS_HZ;
        out->bpm = (float)s->shown_bpm;
        out->bpm_valid = shown;
        out->is_flatline = false;
        out->raw_candidate_valid = s->has_reading;
        out->harmonic_confirmed = -1;
    }
    return shown;
}
