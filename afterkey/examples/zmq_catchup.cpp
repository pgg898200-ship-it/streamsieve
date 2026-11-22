/*
 * Per-key catch-up for slow ZeroMQ readers. A client supplies its last applied
 * revision and a fixed channel mask. The server replies with a high-water mark
 * and one absolute value or deletion for each matching channel touched since.
 *
 * Build from afterkey/ with libzmq development headers installed:
 *   c++ -std=c++17 -O2 -Iinclude examples/zmq_catchup.cpp $(pkg-config --cflags --libs libzmq) -o /tmp/zmq-catchup
 *   /tmp/zmq-catchup
 *
 * This is a single-owner, three-key, in-process request/reply example. One REP
 * socket serves both readers. A test driver interleaves requests and replies
 * deterministically. Each reply fits in one frame. Topic masks filter rows
 * during iteration, so CPU work scales with all changed keys. Cursors are valid
 * only for one server instance and one mask. Clear client state and start at
 * zero after either changes. A remote service needs an instance ID on the wire
 * to detect restarts and a bound on aggregate response size.
 */
#include "afterkey.hpp"
#include <zmq.h>

#include <array>
#include <cstdint>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>

namespace {
constexpr unsigned capacity = 3;
constexpr char endpoint[] = "inproc://afterkey";
using Handle = std::unique_ptr<void, decltype(&zmq_close)>;

void check(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}

Handle socket(void* context, int type) {
    Handle s(zmq_socket(context, type), zmq_close);
    check(bool(s), "socket creation failed");
    int linger = 0, timeout_ms = 2000;
    check(zmq_setsockopt(s.get(), ZMQ_LINGER, &linger, sizeof(linger)) == 0 &&
          zmq_setsockopt(s.get(), ZMQ_RCVTIMEO, &timeout_ms, sizeof(timeout_ms)) == 0 &&
          zmq_setsockopt(s.get(), ZMQ_SNDTIMEO, &timeout_ms, sizeof(timeout_ms)) == 0,
          "socket configuration failed");
    const int rc = type == ZMQ_REP ? zmq_bind(s.get(), endpoint) : zmq_connect(s.get(), endpoint);
    check(rc == 0, "could not bind or connect");
    return s;
}

void send(void* s, const std::string& frame) {
    check(zmq_send(s, frame.data(), frame.size(), 0) == static_cast<int>(frame.size()),
          "incomplete send");
}

std::string receive(void* s) {
    std::array<char, 512> buf;
    const int size = zmq_recv(s, buf.data(), buf.size(), 0);
    check(size >= 0 && static_cast<std::size_t>(size) <= buf.size(), "invalid frame length");
    return std::string(buf.data(), size);
}

void serve_once(void* reply_socket, const afterkey::Index<int>& index) {
    std::istringstream request(receive(reply_socket));
    std::uint64_t cursor;
    unsigned mask;
    if (!(request >> cursor >> mask) || !(request >> std::ws).eof() ||
        cursor > index.version() || mask >= (1u << capacity)) {
        send(reply_socket, "ERR");
        return;
    }
    std::ostringstream reply;
    reply << index.version() << '\n';
    index.since(cursor, [&](std::uint64_t rev, std::uint32_t key, const int* value) {
        if (mask & (1u << key)) {
            reply << rev << ' ' << key << ' ' << (value ? std::to_string(*value) : "deleted") << '\n';
        }
    });
    send(reply_socket, reply.str());
}

struct Reader {
    Handle connection;
    unsigned mask;
    std::uint64_t cursor = 0;

    // The test consumer checks every returned row (including order) before
    // committing the watermark. An application would apply these rows here.
    void poll(void* server, const afterkey::Index<int>& index, const std::string& expected) {
        send(connection.get(), std::to_string(cursor) + " " + std::to_string(mask));
        serve_once(server, index);
        const std::string reply = receive(connection.get());
        check(reply == expected, "unexpected response");
        cursor = std::stoull(reply.substr(0, reply.find('\n')));
    }
};
} // namespace

int main() {
    try {
        std::unique_ptr<void, decltype(&zmq_ctx_term)> context(zmq_ctx_new(), zmq_ctx_term);
        check(bool(context), "context creation failed");
        auto server = socket(context.get(), ZMQ_REP);
        Reader a{socket(context.get(), ZMQ_REQ), 0b011};
        Reader b{socket(context.get(), ZMQ_REQ), 0b110};
        afterkey::Index<int> index(capacity);
        index.put(0, 10);
        index.put(1, 23);
        index.put(0, 11);
        a.poll(server.get(), index, "3\n2 1 23\n3 0 11\n");
        a.connection.reset();

        index.erase(1);
        index.put(2, 43);
        index.put(0, 13);
        index.put(2, 44);
        a.connection = socket(context.get(), ZMQ_REQ);
        a.poll(server.get(), index, "7\n4 1 deleted\n6 0 13\n");
        b.poll(server.get(), index, "7\n4 1 deleted\n7 2 44\n");
        a.poll(server.get(), index, "7\n");
        index.put(2, 45);
        a.poll(server.get(), index, "8\n");
        b.poll(server.get(), index, "8\n8 2 45\n");
        std::cout << "Verified 2 readers, 8 writes, 6 polls, 7 rows, 2 empty replies and 1 reconnect.\n";
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
