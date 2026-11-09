/* Minimal <math.h> for the Cortex-M4 size and stack build, which has no C library: the
 * functions the estimator calls (from the target's libm when linked) and the C99 macros it
 * uses, mapped to compiler builtins. */
#ifndef HR_FREESTANDING_MATH_H
#define HR_FREESTANDING_MATH_H

float expf(float x);
float logf(float x);
float sqrtf(float x);
float fabsf(float x);
float floorf(float x);
float ceilf(float x);

#define NAN (__builtin_nanf(""))
#define INFINITY (__builtin_inff())
#define isnan(x) __builtin_isnan(x)
#define isfinite(x) __builtin_isfinite(x)

#endif
