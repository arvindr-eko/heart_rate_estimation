/* Cortex-M4 size build: one symbol the size of hr_state_t under the target ABI, read with
 * llvm-nm (make m4). */
#include "hr.h"

char hr_state_size_probe[sizeof(hr_state_t)];
