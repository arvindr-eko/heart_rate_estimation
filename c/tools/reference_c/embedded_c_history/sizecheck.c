#include <stdio.h>
#include "combo13.h"
#include "combo13_opt.h"
int main(){
    printf("Phase A  (combo13.h)      state size: %lu bytes\n", c13_state_size());
    printf("Optimized(combo13_opt.h)  state size: %lu bytes\n", c13opt_state_size());
    return 0;
}
