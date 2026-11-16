CXX ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -Wno-missing-field-initializers -fPIC -pthread
all: libgpu-memory-owner.so gather.spv
libgpu-memory-owner.so: owner.cpp
	$(CXX) $(CXXFLAGS) -shared $< -o $@ -lvulkan -lcrypto
gather.spv: gather.comp
	glslangValidator -V $< -o $@
clean:
	rm -f libgpu-memory-owner.so gather.spv
.PHONY: all clean

# Host-specific optimized baseline; rebuild when comparing on another CPU.
cpu-gather.so: cpu_gather.cpp
	$(CXX) -O3 -march=native -std=c++17 -Wall -Wextra -fPIC -shared $< -o $@

# Retained divide/modulo shader for controlled optimization A/B tests.
gather-baseline.spv: gather_baseline.comp
	glslangValidator -V $< -o $@

gather-shift.spv: gather_experiment_shift.comp
	glslangValidator -V $< -o $@

resident_setup.spv: resident_setup.comp
	glslangValidator -V $< -o $@
vram-benchmark: vram_benchmark.cpp owner.cpp
	$(CXX) $(CXXFLAGS) $< -o $@ -lvulkan -lcrypto

cache_group.spv: cache_group.comp
	glslangValidator -V $< -o $@
cache-benchmark: cache_benchmark.cpp vram_benchmark.cpp owner.cpp
	$(CXX) $(CXXFLAGS) $< -o $@ -lvulkan -lcrypto
