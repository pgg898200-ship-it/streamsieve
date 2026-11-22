CXX ?= c++
CXXFLAGS ?= -O3 -std=c++17 -DNDEBUG -Wall -Wextra -Wpedantic
OUT ?= build
HEADER = include/pulsefold.hpp

.PHONY: all check live-check benchmark scale clean
all: $(OUT)/pulsefold

$(OUT):
	mkdir -p $(OUT)

$(OUT)/pulsefold: src/replay.cpp src/arguments.hpp $(HEADER) | $(OUT)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -Iinclude $< -o $@

$(OUT)/pulsefold-live: src/live.cpp src/arguments.hpp $(HEADER) | $(OUT)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -Iinclude $< -o $@

live-check: $(OUT)/pulsefold-live
	python3 tests/live_smoke.py $(OUT)/pulsefold-live

$(OUT)/tests: tests/test_monitor.cpp $(HEADER) | $(OUT)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -UNDEBUG -Iinclude $< -o $@

$(OUT)/benchmark: bench/benchmark.cpp bench/workloads.hpp bench/baselines.hpp bench/wide_radix.hpp $(HEADER) | $(OUT)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -Iinclude $< -o $@

$(OUT)/scale: bench/scale.cpp $(HEADER) | $(OUT)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -Iinclude $< -o $@

$(OUT)/baseline-tests: tests/test_baselines.cpp bench/baselines.hpp bench/wide_radix.hpp | $(OUT)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -UNDEBUG $< -o $@

check: $(OUT)/pulsefold $(OUT)/tests $(OUT)/baseline-tests
	$(OUT)/tests
	$(OUT)/baseline-tests
	$(OUT)/pulsefold 1 5,12 < examples/heartbeat.txt > $(OUT)/example.txt
	diff -u examples/expected.txt $(OUT)/example.txt

benchmark: $(OUT)/benchmark
	$(OUT)/benchmark

scale: $(OUT)/scale
	$(OUT)/scale

clean:
	rm -rf $(OUT)

$(OUT)/grouped: bench/grouped.cpp bench/workloads.hpp bench/baselines.hpp bench/wide_radix.hpp $(HEADER) | $(OUT)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -Iinclude $< -o $@
