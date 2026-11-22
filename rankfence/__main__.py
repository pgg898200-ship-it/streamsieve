"""rankfence fuse consumes NDJSON pages; rankfence verify checks a JSON receipt."""
import argparse
import json
import sys

from . import Fence, PartitionFence, verify


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rankfence", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fuse", help="read pages from standard input until certified")
    topology = f.add_mutually_exclusive_group(required=True)
    topology.add_argument("--sources", type=int)
    topology.add_argument("--groups", nargs="+", metavar="NAME", help="one disjoint-partition name per source")
    f.add_argument("-k", type=int, required=True)
    f.add_argument("--c", type=int, default=60)
    f.add_argument("--mode", choices=["ordered", "members"], default="ordered")
    f.add_argument("--receipt", action="store_true")
    check = sub.add_parser("verify", help="check a JSON receipt from standard input")
    check.add_argument("--groups", nargs="+", metavar="NAME", help="require this trusted partition layout")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            valid = verify(json.load(sys.stdin), source_groups=args.groups)
            print(json.dumps({"valid": valid}))
            return 0 if valid else 1

        if args.groups is None:
            fence = Fence(args.sources, args.k, args.c)
        else:
            fence = PartitionFence(args.groups, args.k, args.c)
        prefixes = [[] for _ in fence.depths] if args.receipt else None
        for line in sys.stdin:
            if not line.strip():
                continue
            page = json.loads(line)
            if not isinstance(page, dict) or not isinstance(page.get("ids"), list):
                raise ValueError("each line must be a page object with an ids array")
            source, ids = page["source"], page["ids"]
            fence.push(source, ids, done=page.get("done", False))
            if prefixes is not None:
                prefixes[source].extend(ids)
            result = fence.ordered() if args.mode == "ordered" else fence.members()
            if result is None:
                continue
            ids = list(result if args.mode == "ordered" else sorted(result))
            if prefixes is None:
                out = {"mode": args.mode, "ids": ids, "depths": fence.depths, "done": fence.done}
            else:
                out = {"format": "rankfence/1", "c": args.c, "k": args.k, "mode": args.mode,
                       "ids": ids, "prefixes": prefixes, "done": list(fence.done)}
            if prefixes is not None and args.groups is not None:
                out.update(format="rankfence-partition/1", source_groups=args.groups)
            print(json.dumps(out, ensure_ascii=True))
            return 0
        print("incomplete input: supply more pages or explicit done flags", file=sys.stderr)
        return 2
    except (ValueError, TypeError, IndexError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
