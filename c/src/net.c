/* Block B, causal TCN (src/device/net.py): beat activation from the band energies, one 10 ms
 * frame at a time. Input: the log energies minus a causal running mean per band. Layers:
 * dilated causal convolutions (kernel HR_NET_KERNEL = 3), ReLU, residual from layer 1 on, then
 * a sigmoid output. Each layer keeps its last 2 x dilation inputs in a ring buffer
 * ([slot][channel]); the taps are the inputs 2 x dilation and dilation frames back and the
 * current one, so no data is shifted per frame. Weights: hr_conv_w [out][in][kernel] per layer,
 * concatenated. */
#include <math.h>

#include "hr.h"
#include "hr_blocks.h"

static const uint8_t dilation[HR_NET_LAYERS] = HR_NET_DILATIONS;
static const uint16_t line_offset[HR_NET_LAYERS] = HR_NET_LINE_OFFSETS;
static const uint8_t in_channels[HR_NET_LAYERS] = HR_NET_IN_CH;

/* Empty delay lines; running mean unset. */
void hr_net_init(hr_net_t *nn)
{
    for (int i = 0; i < HR_NET_LINE_TOTAL; i++)
        nn->line[i] = 0.0f;
    for (int i = 0; i < HR_NET_LAYERS; i++)
        nn->oldest[i] = 0;
    for (int b = 0; b < HR_NET_IN; b++)
        nn->mean[b] = 0.0f;
    nn->mean_set = 0;
}

/* Subtract the causal running mean of each band (started at the first frame). */
void hr_net_normalise(hr_net_t *nn, const float in[HR_NET_IN], float out[HR_NET_IN])
{
    /* the mean starts at the first frame */
    if (!nn->mean_set) {
        for (int b = 0; b < HR_NET_IN; b++)
            nn->mean[b] = in[b];
        nn->mean_set = 1;
    }
    for (int b = 0; b < HR_NET_IN; b++) {
        nn->mean[b] += HR_NORM_ALPHA * (in[b] - nn->mean[b]);
        out[b] = in[b] - nn->mean[b];
    }
}

/* One frame through the dilated causal convolutions and the sigmoid output. */
float hr_net_step(hr_net_t *nn, const float x_in[HR_NET_IN])
{
    float x[HR_NET_CH], y[HR_NET_CH];
    const float *w = hr_conv_w;
    const float *bias = hr_conv_b;
    for (int c = 0; c < HR_NET_IN; c++)
        x[c] = x_in[c];
    for (int layer = 0; layer < HR_NET_LAYERS; layer++) {
        const int n_in = in_channels[layer];
        const int d = dilation[layer];
        const int slots = 2 * d;
        float *line = &nn->line[line_offset[layer]];
        const int oldest = nn->oldest[layer];
        const float *tap0 = &line[oldest * n_in];                   /* 2 x d frames back */
        const float *tap1 = &line[((oldest + d) % slots) * n_in];   /* d frames back */
        /* convolution, bias, ReLU */
        for (int o = 0; o < HR_NET_CH; o++) {
            float acc = 0.0f;
            for (int c = 0; c < n_in; c++) {
                const float *k = &w[(o * n_in + c) * HR_NET_KERNEL];
                acc += k[0] * tap0[c] + k[1] * tap1[c] + k[2] * x[c];
            }
            acc += bias[o];
            y[o] = acc > 0.0f ? acc : 0.0f;
        }
        /* the current input replaces the oldest */
        for (int c = 0; c < n_in; c++)
            line[oldest * n_in + c] = x[c];
        nn->oldest[layer] = (uint16_t)((oldest + 1) % slots);
        for (int o = 0; o < HR_NET_CH; o++)
            x[o] = layer > 0 ? y[o] + x[o] : y[o];
        w += HR_NET_CH * n_in * HR_NET_KERNEL;
        bias += HR_NET_CH;
    }
    float out = 0.0f;
    for (int c = 0; c < HR_NET_CH; c++)
        out += hr_out_w[c] * x[c];
    out += hr_out_b[0];
    return 1.0f / (1.0f + expf(-out));
}
