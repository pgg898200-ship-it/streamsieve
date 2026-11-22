#!/usr/bin/env python3
"""Stream SUMO Floating Car Data XML to EdgeChime position records."""

import argparse
import gzip
import json
import math
import sys
from contextlib import nullcontext
from xml.parsers import expat


def convert(source, destination):
    parser = expat.ParserCreate()
    stack = []
    time = None

    def fail(message):
        raise ValueError(f"line {parser.CurrentLineNumber}: {message}")

    def number(attrs, name, context):
        try:
            value = float(attrs[name])
        except (KeyError, ValueError):
            fail(f"{context}: expected numeric {name}")
        if not math.isfinite(value):
            fail(f"{context}: expected finite {name}")
        return value

    def start(tag, attrs):
        nonlocal time
        parent = stack[-1] if stack else None
        if parent is None and tag != "fcd-export":
            fail("expected <fcd-export> as the root")
        if tag == "timestep":
            if parent != "fcd-export":
                fail("expected <timestep> directly within <fcd-export>")
            time = number(attrs, "time", "timestep")
        elif tag == "vehicle":
            if parent != "timestep":
                fail("expected <vehicle> directly within <timestep>")
            agent = attrs.get("id")
            if not agent:
                fail(f"timestep {time}: expected a vehicle id")
            context = f"vehicle {agent!r} at timestep {time}"
            record = {"agent": agent, "x": number(attrs, "x", context),
                      "y": number(attrs, "y", context), "time": time}
            destination.write(json.dumps(record, separators=(",", ":")) + "\n")
        stack.append(tag)

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda tag: stack.pop()
    # One underlying read per chunk lets live pipes advance as soon as bytes
    # arrive. No XML tree or set of previously observed vehicles accumulates.
    while chunk := source.read1(65536):
        parser.Parse(chunk, False)
        destination.flush()
    parser.Parse(b"", True)


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("path", nargs="?", default="-",
                      help="FCD XML file, .gz file, or - for an XML stream on stdin")
    path = args.parse_args().path
    try:
        if path == "-":
            context = nullcontext(sys.stdin.buffer)
        else:
            opener = gzip.open if path.endswith(".gz") else open
            context = opener(path, "rb")
        with context as source:
            convert(source, sys.stdout)
    except BrokenPipeError:
        # The downstream consumer closed the pipe.
        return 0
    except (OSError, ValueError, expat.ExpatError, EOFError) as error:
        print(f"fcd: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
