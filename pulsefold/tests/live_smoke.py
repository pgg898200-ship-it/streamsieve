"""Exercise the live adapter through a real pipe, including a stalled producer."""
import os
import select
import subprocess
import sys
import time

binary = sys.argv[1]


def read_event(stream):
    deadline = time.monotonic() + 5
    line = bytearray()
    while not line.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        assert remaining > 0 and select.select([stream], [], [], remaining)[0], "event timed out"
        byte = os.read(stream.fileno(), 1)
        assert byte, "monitor closed stdout unexpectedly"
        line.extend(byte)
    timestamp, feed, subscriber, state = line.decode("ascii").split()
    return int(timestamp), int(feed), int(subscriber), state


proc = subprocess.Popen(
    [binary, "2", "90,170"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, bufsize=0
)
try:
    # The incomplete next ID must neither refresh feed 1 nor stall timer delivery.
    proc.stdin.write(b"1\n1")
    early, late = read_event(proc.stdout), read_event(proc.stdout)
    assert early[1:] == (1, 0, "stale") and late[1:] == (1, 1, "stale"), (early, late)
    assert early[0] >= 90 and late[0] - early[0] == 80, (early, late)
    proc.stdin.write(b"\r\n")
    first, second = read_event(proc.stdout), read_event(proc.stdout)
    assert first[1:] == (1, 0, "fresh") and second[1:] == (1, 1, "fresh"), (first, second)
    assert first[0] >= late[0] and second[0] == first[0], (first, second)
    proc.stdin.close()
    assert proc.wait(timeout=5) == 0, proc.stderr.read()
finally:
    proc.kill()
    proc.wait()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        stream.close()

for args, data, expected in [
    (["1", "1000"], b"0", 0),           # complete, unterminated final ID
    (["1", "1000"], b"0\n2\n", 1),     # out of range
    (["1", "1000"], b"0" * 65, 1),     # unbounded partial input
    (["1", "1000"], b"nope\n", 1),
    (["1", "0"], b"", 1),              # zero TTL
    (["1", "1000,"], b"", 1),          # missing TTL
    (["4294967296", "1000"], b"", 1),
    (["1", "18446744073709551615"], b"0\n", 0),
]:
    result = subprocess.run([binary, *args], input=data, capture_output=True, timeout=5)
    assert result.returncode == expected, (args, result.returncode, result.stderr)
    if expected:
        assert result.stderr
    else:
        assert not result.stdout

print("Live pipe smoke passed: silent expiry, partial lines, flushed recovery, EOF and invalid inputs.")
