CXX      ?= g++
CXXFLAGS ?= -std=c++20 -O3 -march=native -ffp-contract=off -Wall -Wextra -Wpedantic -fPIC
SANFLAGS := -std=c++20 -O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined -Wall -Wextra -Wpedantic -fPIC

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
    SHLIB_EXT := dylib
    SHLIB_FLAGS := -dynamiclib
else
    SHLIB_EXT := so
    SHLIB_FLAGS := -shared
endif

BUILD_DIR := build
LIB_OUT   := python/streamsieve/_libstreamsieve.$(SHLIB_EXT)
CLI_OUT   := $(BUILD_DIR)/streamsieve
CPP_TEST  := $(BUILD_DIR)/test_core_cpp
SAN_TEST  := $(BUILD_DIR)/test_core_cpp_san

.PHONY: all lib cli test test-cpp test-san bench clean

all: lib cli

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

lib: $(LIB_OUT)

$(LIB_OUT): csrc/streamsieve_capi.cpp csrc/streamsieve_core.hpp csrc/streamsieve_capi.h
	$(CXX) $(CXXFLAGS) $(SHLIB_FLAGS) csrc/streamsieve_capi.cpp -o $(LIB_OUT) -lm

cli: $(CLI_OUT)

$(CLI_OUT): csrc/streamsieve_cli.cpp csrc/streamsieve_core.hpp csrc/streamsieve_capi.h | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) csrc/streamsieve_cli.cpp csrc/streamsieve_capi.cpp -o $(CLI_OUT) -lm

test-cpp: $(CPP_TEST)
	./$(CPP_TEST)

$(CPP_TEST): tests/test_core_cpp.cpp csrc/streamsieve_core.hpp csrc/streamsieve_capi.h csrc/streamsieve_capi.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) tests/test_core_cpp.cpp csrc/streamsieve_capi.cpp -o $(CPP_TEST) -lm

test-san: $(SAN_TEST)
	./$(SAN_TEST)

$(SAN_TEST): tests/test_core_cpp.cpp csrc/streamsieve_core.hpp csrc/streamsieve_capi.h csrc/streamsieve_capi.cpp | $(BUILD_DIR)
	$(CXX) $(SANFLAGS) tests/test_core_cpp.cpp csrc/streamsieve_capi.cpp -o $(SAN_TEST) -lm

test: all test-cpp test-san
	PYTHONPATH=python python3 -m unittest discover -s tests -p "test_*.py" -v

bench: all
	PYTHONPATH=python .bench-venv/bin/python benchmarks/run_all.py

clean:
	rm -rf $(BUILD_DIR) $(LIB_OUT)
