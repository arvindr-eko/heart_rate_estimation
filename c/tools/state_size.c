/* Cortex-M4 size build: symbols the size of hr_state_t and of its input ring under the target
 * ABI, read with llvm-nm (make m4). */
#include "hr_blocks.h"

char hr_state_size_probe[sizeof(hr_state_t)];
char hr_input_ring_probe[sizeof(((hr_state_t *)0)->ring)];
