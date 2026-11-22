"""Local stdio MCP server for bounded, deterministic RRF replay and verification."""
import json
import math
import sys
from fractions import Fraction

from . import Fence, PartitionFence, retirable_sources, verify

MODERN, LEGACY = "2026-07-28", "2025-11-25"
NS = "io.modelcontextprotocol/"
INFO = {"name": "rankfence", "version": "0.1.0"}
CAPABILITIES = {"tools": {}}
FRAME = 256 * 1024
MAX_SOURCES, MAX_IDS, MAX_DEPTH = 32, 2048, 512
ID_SCHEMA = {"type": "string", "maxLength": 256}
GROUPS = {"type": "array", "minItems": 1, "maxItems": MAX_SOURCES, "items": ID_SCHEMA}
RANKINGS = {**GROUPS, "items": {"type": "array", "maxItems": MAX_DEPTH, "items": ID_SCHEMA}}


def tool(name, description, properties, required):
    return {"name": name, "description": description, "annotations": {
        "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {"type": "object", "properties": properties, "required": required,
                        "additionalProperties": False}}


TOOLS = [
    tool("rankfence_analyze", "Replay one complete RRF query in synchronous page rounds. "
         "Report simulated unread IDs, ordered winners, checked receipt, and exhaustive agreement. "
         "Supports up to 2048 ID occurrences. Supply source_groups only for disjoint shards.",
         {"rankings": RANKINGS, "source_groups": GROUPS,
          "k": {"type": "integer", "minimum": 1, "maximum": 64},
          "c": {"type": "integer", "minimum": 0, "maximum": 10000},
          "page_size": {"type": "integer", "minimum": 1, "maximum": MAX_DEPTH},
          "prune": {"type": "boolean"}}, ["rankings", "k"]),
    tool("rankfence_verify", "Verify a prefix receipt using exact rational arithmetic. "
         "For partition receipts supply trusted_groups independently from your index layout. "
         "Source order and EOF assertions must be truthful.",
         {"receipt": {"type": "object"}, "trusted_groups": GROUPS}, ["receipt"]),
]


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def validate_rows(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SOURCES:
        raise ValueError("expected 1 to 32 source rankings")
    total = 0
    for row in rows:
        if not isinstance(row, list) or len(row) > MAX_DEPTH:
            raise ValueError("each ranking must be an array of at most 512 IDs")
        if any(type(x) is not str or len(x) > 256 for x in row):
            raise ValueError("IDs must be strings of at most 256 characters")
        total += len(row)
    if total > MAX_IDS:
        raise ValueError("query exceeds 2048 ID occurrences")
    return total


def validate_groups(groups, n):
    if (not isinstance(groups, list) or len(groups) != n or
            any(type(x) is not str or len(x) > 256 for x in groups)):
        raise ValueError("supply one group string of at most 256 characters per source")


def analyze(args):
    rows = args.get("rankings")
    total = validate_rows(rows)
    k = integer(args.get("k"), 1, 64, "k")
    c = integer(args.get("c", 60), 0, 10000, "c")
    page = integer(args.get("page_size", 8), 1, MAX_DEPTH, "page_size")
    groups = args.get("source_groups")
    if "source_groups" in args:
        validate_groups(groups, len(rows))
    prune = args.get("prune", groups is not None)
    if type(prune) is not bool or (prune and groups is None):
        raise ValueError("prune must be boolean and requires source_groups when enabled")

    scores, owners = {}, {}
    for i, row in enumerate(rows):
        if len(set(row)) != len(row):
            raise ValueError("duplicate ID within one source")
        for rank, item in enumerate(row, 1):
            if groups is not None and owners.setdefault(item, groups[i]) != groups[i]:
                raise ValueError("supplied complete rankings violate group disjointness")
            scores[item] = scores.get(item, 0) + Fraction(1, c + rank)
    exact = sorted(scores, key=lambda item: (-scores[item], item))[:k]
    fence = Fence(len(rows), k, c) if groups is None else PartitionFence(groups, k, c)
    retired = set()
    while True:
        for i, (start, done) in enumerate(zip(fence.depths, fence.done)):
            if done or i in retired:
                continue
            stop = min(start + page, len(rows[i]))
            fence.push(i, rows[i][start:stop], done=stop == len(rows[i]))
        winners = fence.ordered()
        if winners is not None:
            break
        if prune:
            retired.update(retirable_sources(fence))
        if all(done or i in retired for i, done in enumerate(fence.done)):
            raise RuntimeError("no active source can complete the replay")

    proof = {"format": "rankfence/1", "c": c, "k": k, "mode": "ordered",
             "done": list(fence.done), "ids": list(winners),
             "prefixes": [row[:n] for row, n in zip(rows, fence.depths)]}
    if groups is not None:
        proof.update(format="rankfence-partition/1", source_groups=groups)
    read = sum(fence.depths)
    return {"ids": list(winners), "ids_in_full_query": total, "ids_read_in_replay": read,
            "ids_saved_in_replay": total - read, "retired_sources": sorted(retired),
            "matches_exhaustive": list(winners) == exact, "receipt": proof,
            "receipt_valid": verify(proof, source_groups=groups)}


def verify_tool(args):
    proof = args.get("receipt")
    if not isinstance(proof, dict):
        raise ValueError("receipt must be an object")
    rows = proof.get("prefixes")
    validate_rows(rows)
    integer(proof.get("c"), 0, 10000, "c")
    integer(proof.get("k"), 1, 64, "k")
    groups = args.get("trusted_groups")
    if "trusted_groups" in args or proof.get("format") == "rankfence-partition/1":
        validate_groups(groups, len(rows))
    return {"valid": verify(proof, source_groups=groups)}


def error(request_id, code, message, **extra):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message, **extra}}


class Server:
    def __init__(self):
        self.legacy_state = "new"

    def respond(self, request):
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or
                type(request.get("method")) is not str or
                ("id" in request and type(request["id"]) not in (int, str))):
            return error(None, -32600, "Invalid request")
        method = request["method"]
        if "id" not in request:
            if method == "notifications/initialized" and self.legacy_state == "initializing":
                self.legacy_state = "ready"
            return None
        rid = request["id"]
        params = request.get("params", {})
        if not isinstance(params, dict) or not isinstance(params.get("_meta", {}), dict):
            return error(rid, -32602, "params and _meta must be objects")
        meta = params.get("_meta", {})
        modern = NS + "protocolVersion" in meta
        if modern:
            version = meta[NS + "protocolVersion"]
            if type(version) is not str:
                return error(rid, -32602, "protocolVersion must be a string")
            if version != MODERN:
                return error(rid, -32022, "Unsupported protocol version",
                             data={"supported": [MODERN, LEGACY], "requested": version})
            if not isinstance(meta.get(NS + "clientCapabilities"), dict):
                return error(rid, -32602, "Missing client capabilities")
        if method == "server/discover" and modern:
            result = {"supportedVersions": [MODERN, LEGACY], "capabilities": CAPABILITIES}
        elif method == "initialize" and not modern:
            info = params.get("clientInfo")
            if self.legacy_state != "new":
                return error(rid, -32600, "Already initialized")
            if (type(params.get("protocolVersion")) is not str or
                    not isinstance(params.get("capabilities"), dict) or not isinstance(info, dict)
                    or any(type(info.get(key)) is not str for key in ("name", "version"))):
                return error(rid, -32602, "Invalid initialization parameters")
            self.legacy_state = "initializing"
            result = {"protocolVersion": LEGACY, "capabilities": CAPABILITIES, "serverInfo": INFO}
        elif method == "ping":
            result = {}
        elif not modern and self.legacy_state != "ready":
            return error(rid, -32602, "Use per-request MCP metadata or complete initialization")
        elif method == "tools/list":
            if "cursor" in params:
                return error(rid, -32602, "Unknown cursor")
            result = {"tools": TOOLS}
        elif method == "tools/call":
            entry = next((t for t in TOOLS if t["name"] == params.get("name")), None)
            args = params.get("arguments", {})
            if entry is None or not isinstance(args, dict):
                return error(rid, -32602, "Unknown tool or malformed arguments")
            try:
                if set(args) - entry["inputSchema"]["properties"].keys():
                    raise ValueError("unexpected tool argument")
                out = analyze(args) if entry is TOOLS[0] else verify_tool(args)
                result = {"structuredContent": out,
                          "content": [{"type": "text", "text": json.dumps(out, separators=(",", ":"))}]}
            except (TypeError, ValueError) as exc:
                result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        else:
            return error(rid, -32601, "Method not found")
        if modern:
            result.update(resultType="complete", _meta={NS + "serverInfo": INFO})
        return {"jsonrpc": "2.0", "id": rid, "result": result}


def unique_object(pairs):
    result = {}
    for key, val in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = val
    return result


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid number")
    return number


def check_depth(value, depth=0):
    if depth > 32:
        raise ValueError("JSON exceeds depth limit")
    if isinstance(value, (dict, list)):
        for child in (value.values() if isinstance(value, dict) else value):
            check_depth(child, depth + 1)


def main():
    server = Server()
    stream = sys.stdin.buffer
    while line := stream.readline(FRAME + 1):
        if len(line) > FRAME:
            while line and not line.endswith(b"\n"):
                line = stream.readline(FRAME + 1)
            reply = error(None, -32600, "Frame exceeds 256 KiB")
        else:
            try:
                message = json.loads(line.decode("utf-8"), object_pairs_hook=unique_object,
                                     parse_float=finite_float, parse_constant=finite_float)
                check_depth(message)
                reply = server.respond(message)
            except (ValueError, RecursionError):
                reply = error(None, -32700, "Invalid JSON")
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
