import json
import subprocess
import sys
import unittest

from rankfence import mcp


def request(method, rid, params=None):
    return {"jsonrpc": "2.0", "method": method, "id": rid, "params": params or {}}


def call(name, args, rid=3, modern=False):
    params = {"name": name, "arguments": args}
    if modern:
        params["_meta"] = {mcp.NS + "protocolVersion": mcp.MODERN,
                           mcp.NS + "clientCapabilities": {}}
    return request("tools/call", rid, params)


def run_wire(messages):
    wire = b"".join((m if isinstance(m, bytes) else json.dumps(m).encode()) + b"\n"
                    for m in messages)
    child = subprocess.run([sys.executable, "-m", "rankfence.mcp"], input=wire,
                           capture_output=True, timeout=8, check=True)
    if child.stderr:
        raise AssertionError(child.stderr)
    return [json.loads(line) for line in child.stdout.splitlines()]


class McpTests(unittest.TestCase):
    def test_real_stdio_legacy_handshake_analysis_and_verification(self):
        init = request("initialize", 0, {"protocolVersion": "2025-11-25", "capabilities": {},
                                         "clientInfo": {"name": "unit", "version": "1"}})
        ready = {"method": "notifications/initialized", "jsonrpc": "2.0"}
        names = ["hot", "hot", "cold"]
        args = {"rankings": [["a", "b", "h1"], ["a", "b", "h2"], ["z", "y", "x"]],
                "k": 1, "source_groups": names, "page_size": 1}
        replies = run_wire([init, ready, request("ping", 1), request("tools/list", 2),
                            call("rankfence_analyze", args)])
        self.assertEqual([r["id"] for r in replies], [0, 1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], mcp.LEGACY)
        self.assertEqual(replies[1]["result"], {})
        self.assertEqual([t["name"] for t in replies[2]["result"]["tools"]],
                         ["rankfence_analyze", "rankfence_verify"])
        data = replies[3]["result"]["structuredContent"]
        self.assertEqual(data["ids"], ["a"])
        self.assertTrue(data["matches_exhaustive"] and data["receipt_valid"])
        self.assertEqual(data["ids_saved_in_replay"], 6)
        arg = {"receipt": data["receipt"], "trusted_groups": names}
        ok, changed, missing = run_wire([
            call("rankfence_verify", arg, modern=True),
            call("rankfence_verify", {**arg, "trusted_groups": ["same"] * 3}, modern=True),
            call("rankfence_verify", {"receipt": data["receipt"]}, modern=True)])
        self.assertEqual(ok["result"]["structuredContent"], {"valid": True})
        self.assertEqual(changed["result"]["structuredContent"], {"valid": False})
        self.assertTrue(missing["result"]["isError"])

    def test_stateless_modern_discovery_and_errors(self):
        meta = {mcp.NS + "protocolVersion": mcp.MODERN, mcp.NS + "clientCapabilities": {}}
        requests = [request("server/discover", 1, {"_meta": meta}),
                    request("tools/list", 2, {"_meta": {**meta, mcp.NS + "protocolVersion": "2099"}}),
                    request("other", 3, {"_meta": meta}), call("unknown", {}, modern=True),
                    call("rankfence_analyze", {"rankings": [["a", "a"]], "k": 1}, modern=True),
                    request("tools/list", 6, {"_meta": {mcp.NS + "protocolVersion": mcp.MODERN}})]
        discovery, version, unknown, bad_tool, bad_data, metadata = run_wire(requests)
        self.assertEqual(discovery["result"]["supportedVersions"], [mcp.MODERN, mcp.LEGACY])
        self.assertEqual(discovery["result"]["resultType"], "complete")
        self.assertEqual(version["error"]["code"], -32022)
        self.assertEqual(unknown["error"]["code"], -32601)
        self.assertEqual(bad_tool["error"]["code"], -32602)
        self.assertTrue(bad_data["result"]["isError"])
        self.assertEqual(metadata["error"]["code"], -32602)

    def test_bounds_and_parser_recovers(self):
        bad = [b'{', b'{"jsonrpc":"2.0","id":1,"id":2}', b'{"x":NaN}',
               b'{"x":1e999}', b'\xff', b'x' * (mcp.FRAME + 100)]
        replies = run_wire(bad + [request("ping", 1)])
        self.assertEqual([r["error"]["code"] for r in replies[:-1]], [-32700] * 5 + [-32600])
        self.assertEqual(replies[-1]["result"], {})
        for args in [
            {"rankings": [[]] * 33, "k": 1}, {"rankings": [["a"]], "k": True},
            {"rankings": [["a" * 257]], "k": 1}, {"rankings": [[str(i) for i in range(513)]], "k": 1},
            {"rankings": [["x"], ["x"]], "k": 1, "source_groups": ["one", "two"]},
            {"rankings": [["a"]], "k": 1, "prune": True},
        ]:
            self.assertTrue(mcp.Server().respond(call("rankfence_analyze", args, modern=True))
                            ["result"]["isError"])

    def test_replay_demonstrates_actual_pruning(self):
        # A and B are tied in two finished rankings. Third hot source
        # breaks their tie after a diversion, while cold cannot win.
        p = [["a", "b"], ["b", "a"], ["h1", "h2", "h3", "h4", "b"],
             [f"z{i}" for i in range(10)]]
        args = {"rankings": p, "k": 2, "source_groups": ["h", "h", "h", "z"], "page_size": 1}
        on = mcp.analyze(args)
        off = mcp.analyze({**args, "prune": False})
        self.assertEqual(on["retired_sources"], [3])
        self.assertGreater(on["ids_saved_in_replay"], off["ids_saved_in_replay"])
        self.assertTrue(on["matches_exhaustive"] and off["matches_exhaustive"])
