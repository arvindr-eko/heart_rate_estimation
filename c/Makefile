# C port of the device reference. Host build: debug library for the tests (HR_DEBUG), the
# hr_run tool and the self-test (make selftest); Cortex-M4 build: object sizes and stack use per
# function (make m4); release bundle for integration (make dist).
# Generated model files (sizes, coefficients, weights) and self-test cases: make model, with the
# project's Python environment active. make m4 needs clang and the LLVM size tools.

CC      ?= cc
M4_CC   ?= clang
SIZE    ?= $(shell command -v llvm-size || xcrun -f llvm-size 2>/dev/null)
NM      ?= $(shell command -v llvm-nm || xcrun -f llvm-nm 2>/dev/null)
MODEL   := generated
GEN     := build/generated
SRC     := src/frontend.c src/quality.c src/net.c src/features.c src/scorer.c src/tracker.c \
           src/rhythm.c src/readout.c src/estimator.c src/stream.c
M4_OBJ  := $(addprefix build/m4/,$(notdir $(SRC:.c=.o))) build/m4/hr_model.o
HDR     := include/hr.h include/hr_blocks.h $(MODEL)/hr_model.h
SHA256  ?= $(shell command -v sha256sum || echo shasum -a 256)
VERSION  = $(shell sed -n 's/^\#define HR_VERSION "\(.*\)"/\1/p' $(MODEL)/hr_model.h)
MODEL_ID = $(shell sed -n 's/^\#define HR_MODEL_ID "\(.*\)"/\1/p' $(MODEL)/hr_model.h)
REPO     = $(shell git -C .. remote get-url origin 2>/dev/null | sed 's/\.git$$//')
DIST     = build/hr_c_$(VERSION)
WARN    := -std=c99 -Wall -Wextra -Werror -pedantic -Wdouble-promotion
CFLAGS  := $(WARN) -O2 -ffp-contract=off -Iinclude -I$(MODEL) -I$(GEN)
M4FLAGS := --target=thumbv7em-none-eabihf -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=hard \
           $(WARN) -Os -nostdlibinc -fno-math-errno -ffp-contract=off -fstack-usage \
           -Iinclude -I$(MODEL) -I$(GEN) -isystem tools/freestanding
ifeq ($(shell uname -s),Darwin)
LIB     := build/libhr_debug.dylib
SHARED  := -dynamiclib
else
LIB     := build/libhr_debug.so
SHARED  := -shared
endif

.PHONY: all model selftest m4 dist clean

all: $(LIB) build/hr_run

model:
	cd .. && hr-estimation c-model

$(MODEL)/hr_model.h $(MODEL)/hr_model.c $(GEN)/hr_selftest_cases.h:
	@echo "$@ missing: run 'make model'" && false

$(LIB): $(SRC) $(MODEL)/hr_model.c $(HDR)
	$(CC) $(CFLAGS) -DHR_DEBUG -fPIC $(SHARED) -o $@ $(SRC) $(MODEL)/hr_model.c -lm

build/hr_run: tools/hr_run.c $(SRC) $(MODEL)/hr_model.c $(HDR)
	$(CC) $(CFLAGS) -o $@ tools/hr_run.c $(SRC) $(MODEL)/hr_model.c -lm

build/hr_selftest: tools/hr_selftest.c $(SRC) $(MODEL)/hr_model.c $(GEN)/hr_selftest_cases.h $(HDR)
	$(CC) $(CFLAGS) -DHR_SELFTEST_MAIN -o $@ tools/hr_selftest.c $(SRC) $(MODEL)/hr_model.c -lm

selftest: build/hr_selftest
	./build/hr_selftest

m4: $(SRC) $(MODEL)/hr_model.c $(HDR) tools/state_size.c
	@test -n "$(SIZE)" -a -n "$(NM)" || { echo "llvm-size and llvm-nm not found"; false; }
	mkdir -p build/m4
	for f in $(SRC) $(MODEL)/hr_model.c tools/state_size.c; do \
		$(M4_CC) $(M4FLAGS) -c $$f -o build/m4/$$(basename $$f .c).o || exit 1; done
	mv *.su build/m4/ 2>/dev/null || true
	$(SIZE) $(M4_OBJ)
	$(NM) --print-size --radix=d build/m4/state_size.o | awk '{print $$2 + 0 " B " $$4}'
	sort -t'	' -k2 -n -r build/m4/*.su | head -12

dist: model
	$(MAKE) selftest
	rm -rf $(DIST) $(DIST).tar.gz
	mkdir -p $(DIST)/include $(DIST)/src $(DIST)/generated $(DIST)/tools
	cp include/hr.h include/hr_blocks.h $(DIST)/include/
	cp $(SRC) $(DIST)/src/
	cp $(MODEL)/hr_model.h $(MODEL)/hr_model.c $(GEN)/hr_selftest_cases.h $(DIST)/generated/
	cp tools/hr_selftest.c tools/hr_run.c $(DIST)/tools/
	{ echo "# hr_c $(VERSION)"; echo; \
	  echo "C port of the heart-rate estimator, model $(MODEL_ID), built $$(date -u +%F) from"; \
	  echo "commit $$(git -C .. describe --always --dirty 2>/dev/null || echo unknown)."; \
	  echo "Integration guide: $(REPO)/blob/v$(VERSION)/docs/realtime_implementation.md §4."; \
	  echo; echo "Self-test on a computer:"; echo; \
	  echo '    cc -std=c99 -O2 -ffp-contract=off -Iinclude -Igenerated -DHR_SELFTEST_MAIN \'; \
	  echo '        tools/hr_selftest.c src/*.c generated/hr_model.c -lm -o hr_selftest'; \
	  echo '    ./hr_selftest'; \
	} > $(DIST)/README.md
	cd $(DIST) && $(SHA256) $$(find . -type f | sort) > SHA256SUMS
	tar -czf $(DIST).tar.gz -C build $(notdir $(DIST))
	@echo "$(DIST).tar.gz"

clean:
	rm -rf build/m4 build/hr_run build/hr_selftest build/libhr_debug.* build/hr_c_*
