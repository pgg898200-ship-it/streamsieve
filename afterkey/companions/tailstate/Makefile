CC ?= cc
CXX ?= c++
CPPFLAGS += -Iinclude
CFLAGS ?= -O3
CXXFLAGS ?= -O3
CSTD = -std=c99 -Wall -Wextra -Wpedantic
BUILD = build

.PHONY: all test sanitize bench pages paged clean
all: $(BUILD)/libtailstate.so $(BUILD)/libtailstate.a

$(BUILD):
	mkdir -p $@

$(BUILD)/tailstate.o: src/tailstate.c include/tailstate.h | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(CSTD) -fPIC -c $< -o $@

$(BUILD)/libtailstate.so: $(BUILD)/tailstate.o
	$(CC) -shared $(LDFLAGS) $< -o $@

$(BUILD)/libtailstate.a: $(BUILD)/tailstate.o
	$(AR) rcs $@ $<

$(BUILD)/test: tests/test_tailstate.c $(BUILD)/tailstate.o
	$(CC) $(CPPFLAGS) $(CFLAGS) $(CSTD) $^ $(LDFLAGS) -o $@

test: $(BUILD)/test $(BUILD)/libtailstate.so
	$(BUILD)/test
	TS_LIBRARY=$(abspath $(BUILD)/libtailstate.so) python3 -m unittest discover -s python -v

sanitize: | $(BUILD)
	$(CC) $(CPPFLAGS) $(CSTD) -O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined tests/test_tailstate.c src/tailstate.c -o $(BUILD)/test-sanitize
	$(BUILD)/test-sanitize

$(BUILD)/bench: bench/bench.c $(BUILD)/tailstate.o
	$(CC) $(CPPFLAGS) $(CFLAGS) $(CSTD) $^ $(LDFLAGS) -o $@

bench: $(BUILD)/bench
	$(BUILD)/bench

$(BUILD)/pages: bench/pages.cpp $(BUILD)/tailstate.o
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -std=c++17 $^ $(LDFLAGS) -o $@

pages: $(BUILD)/pages
	$(BUILD)/pages

$(BUILD)/paged: examples/paged.c $(BUILD)/tailstate.o
	$(CC) $(CPPFLAGS) $(CFLAGS) $(CSTD) $^ $(LDFLAGS) -o $@

paged: $(BUILD)/paged
	$(BUILD)/paged

clean:
	$(RM) -r $(BUILD)

.PHONY: fleet
$(BUILD)/fleet: bench/fleet.cpp $(BUILD)/tailstate.o
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -std=c++17 $^ $(LDFLAGS) -o $@

fleet: $(BUILD)/fleet
	$(BUILD)/fleet
