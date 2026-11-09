# C port of the device reference. Host build: debug library for the tests (HR_DEBUG) and the
# hr_run tool; Cortex-M4 build: object sizes and stack use per function.
# Generated model files (sizes, coefficients, weights): make model.

CC      ?= cc
M4_CC   ?= clang
SIZE    ?= $(shell command -v llvm-size || xcrun -f llvm-size 2>/dev/null)
GEN     := build/generated
SRC     := src/frontend.c src/net.c src/features.c src/scorer.c src/tracker.c src/rhythm.c \
           src/readout.c src/estimator.c
HDR     := include/hr.h include/hr_blocks.h $(GEN)/hr_model.h
WARN    := -std=c99 -Wall -Wextra -Werror -pedantic
CFLAGS  := $(WARN) -O2 -ffp-contract=off -Iinclude -I$(GEN)
M4FLAGS := --target=thumbv7em-none-eabihf -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard \
           $(WARN) -Os -nostdlibinc -fno-math-errno -ffp-contract=off -fstack-usage \
           -Iinclude -I$(GEN) -isystem tools/freestanding
ifeq ($(shell uname -s),Darwin)
LIB     := build/libhr_debug.dylib
SHARED  := -dynamiclib
else
LIB     := build/libhr_debug.so
SHARED  := -shared
endif

.PHONY: all model m4 clean

all: $(LIB) build/hr_run

model:
	cd .. && hr-estimation c-model

$(GEN)/hr_model.h $(GEN)/hr_model.c:
	@echo "$@ missing: run 'make model'" && false

$(LIB): $(SRC) $(GEN)/hr_model.c $(HDR)
	$(CC) $(CFLAGS) -DHR_DEBUG -fPIC $(SHARED) -o $@ $(SRC) $(GEN)/hr_model.c -lm

build/hr_run: tools/hr_run.c $(SRC) $(GEN)/hr_model.c $(HDR)
	$(CC) $(CFLAGS) -o $@ tools/hr_run.c $(SRC) $(GEN)/hr_model.c -lm

m4: $(SRC) $(GEN)/hr_model.c $(HDR)
	mkdir -p build/m4
	for f in $(SRC) $(GEN)/hr_model.c; do \
		$(M4_CC) $(M4FLAGS) -c $$f -o build/m4/$$(basename $$f .c).o || exit 1; done
	mv *.su build/m4/ 2>/dev/null || true
	$(SIZE) build/m4/*.o
	sort -t'	' -k2 -n -r build/m4/*.su | head -12

clean:
	rm -rf build/m4 build/hr_run build/libhr_debug.*
