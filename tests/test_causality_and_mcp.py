"""Pure-stdlib unittest suite for causality verification, CLI, zero-deps, and live MCP stdio server."""

from __future__ import annotations

import io
import json
import os
import random
import subprocess
import sys
import unittest

import streamsieve
from streamsieve import (
    CausalityViolationError,
    EStream,
    EvidenceLattice,
    TrustCalibrator,
    VOISifter,
    assert_causal,
    verify_estream_causality,
)
from streamsieve.cli import main as cli_main


def _repo_python_env() -> dict[str, str]:
    env = dict(os.environ)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(streamsieve.__file__)))
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pkg_parent}{os.pathsep}{existing}" if existing else pkg_parent
    return env


class TestCausality(unittest.TestCase):
    def test_assert_causal_passes_on_causal_fn_and_catches_lookahead(self) -> None:
        series_1d = [float(i % 7) * 0.25 for i in range(40)]

        def causal_running_mean(xs: list[float]) -> list[float]:
            out: list[float] = []
            acc = 0.0
            for idx, x in enumerate(xs):
                acc += x
                out.append(acc / (idx + 1))
            return out

        def leaky_global_z(xs: list[float]) -> list[float]:
            mean = sum(xs) / len(xs)
            return [x - mean for x in xs]

        def leaky_one_step_ahead(xs: list[float]) -> list[float]:
            return [xs[min(i + 1, len(xs) - 1)] for i in range(len(xs))]

        self.assertTrue(assert_causal(causal_running_mean, series_1d, probe_ticks=[0, 5, 19, 38]))

        with self.assertRaises(CausalityViolationError):
            assert_causal(leaky_global_z, series_1d, probe_ticks=[5])

        with self.assertRaises(CausalityViolationError):
            assert_causal(leaky_one_step_ahead, series_1d, probe_ticks=[10])

    def test_verify_estream_causality_bit_exact(self) -> None:
        rng = random.Random(123)
        mat = [[rng.gauss(0.0, 1.0) for _ in range(6)] for _ in range(100)]
        for t in range(45, 100):
            mat[t][0] += 1.8
            if t % 4 == 0:
                mat[t][1] += 3.5

        self.assertTrue(
            verify_estream_causality(
                mat, probe_ticks=[0, 10, 31, 32, 44, 45, 70, 98], warmup=32
            )
        )


class TestZeroDependenciesAndCLI(unittest.TestCase):
    def test_zero_third_party_runtime_dependencies(self) -> None:
        # Exercise every public API surface and verify no third-party scientific pkgs were imported
        with EStream(4, warmup=8) as est:
            est.update_batch([[0.1 * c for c in range(4)] for _ in range(20)])
            _ = est.brief().to_compact_text()
        with EvidenceLattice() as lat:
            lat.observe("h", "p", 1.0)
            _ = EvidenceLattice.from_base85(lat.to_base85())
        with VOISifter(8, n_sites=2) as sf:
            sf.observe_mean(0, 1.0)
            _ = sf.allocate(2, n_agents=2)
        with TrustCalibrator(2) as tc:
            tc.predict_prob(1, [0.7, 0.3])
            tc.resolve(1, 1.0)

        for forbidden in ("numpy", "scipy", "river", "pandas", "torch"):
            self.assertNotIn(forbidden, sys.modules, f"Forbidden runtime dependency imported: {forbidden}")

    def test_cli_subcommands(self) -> None:
        # version
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            rc = cli_main(["version"])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("streamsieve", buf.getvalue())

        # verify --json
        buf = io.StringIO()
        try:
            sys.stdout = buf
            rc = cli_main(["verify", "--json"])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())
        self.assertTrue(report["ok"])

        # bench --json
        buf = io.StringIO()
        try:
            sys.stdout = buf
            rc = cli_main(["bench", "--channels", "32", "--ticks", "120", "--json"])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        bench = json.loads(buf.getvalue())
        self.assertGreater(bench["mpts_per_sec"], 0.0)

        # sift via stdin CSV
        rng = random.Random(55)
        lines = ["cpu,mem,err"]
        for t in range(100):
            v0 = rng.gauss(0.0, 1.0) + (2.5 if t >= 40 else 0.0)
            v1 = rng.gauss(0.0, 1.0)
            v2 = rng.gauss(0.0, 1.0)
            lines.append(f"{v0:.4f},{v1:.4f},{v2:.4f}")
        csv_payload = "\n".join(lines) + "\n"

        proc = subprocess.run(
            [sys.executable, "-m", "streamsieve.cli", "sift", "--warmup", "24", "--json", "-"],
            input=csv_payload,
            text=True,
            capture_output=True,
            env=_repo_python_env(),
            check=True,
        )
        sift_out = json.loads(proc.stdout)
        self.assertEqual(sift_out["n_channels"], 3)
        self.assertEqual(sift_out["ticks"], 100)
        self.assertIn(0, sift_out["rejected_channels"])
        self.assertEqual(sift_out["top"][0]["name"], "cpu")


class TestMCPStdioServerEndToEnd(unittest.TestCase):
    """Spawns ``python3 -m streamsieve.mcp_server`` over real stdin/stdout pipes."""

    def _rpc(self, proc: subprocess.Popen[bytes], req: dict) -> dict:
        assert proc.stdin is not None and proc.stdout is not None
        line = (json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8")
        proc.stdin.write(line)
        proc.stdin.flush()
        resp_line = proc.stdout.readline()
        self.assertTrue(resp_line, "MCP server closed stdout unexpectedly")
        return json.loads(resp_line.decode("utf-8"))

    def test_full_mcp_session_all_7_tools_ndjson(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "streamsieve.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_repo_python_env(),
        )
        try:
            assert proc.stdin is not None and proc.stdout is not None

            # 1. initialize
            init_resp = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "clientInfo": {"name": "unittest", "version": "1.0"},
                        "capabilities": {},
                    },
                },
            )
            self.assertEqual(init_resp["id"], 1)
            self.assertEqual(init_resp["result"]["serverInfo"]["name"], "streamsieve")
            self.assertIn("tools", init_resp["result"]["capabilities"])

            # 2. notifications/initialized (must produce NO response)
            proc.stdin.write(
                (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode("utf-8")
            )
            proc.stdin.flush()

            # 3. ping (proves notification did not desync responses)
            ping_resp = self._rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
            self.assertEqual(ping_resp["id"], 2)
            self.assertEqual(ping_resp["result"], {})

            # 4. tools/list -> all 7 tools present with valid schemas
            list_resp = self._rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
            tools = list_resp["result"]["tools"]
            tool_names = [t["name"] for t in tools]
            self.assertEqual(
                tool_names,
                [
                    "sift_stream",
                    "fetch_raw_ticks",
                    "lattice_observe",
                    "lattice_merge",
                    "lattice_query",
                    "voi_allocate",
                    "trust_step",
                ],
            )
            for t in tools:
                self.assertIn("inputSchema", t)
                self.assertEqual(t["inputSchema"]["type"], "object")

            # Tool 1: sift_stream (ingests 160x8 stream with shift on ch 2)
            rng = random.Random(314)
            rows = []
            for tick in range(160):
                row = [rng.gauss(0.0, 1.0) for _ in range(8)]
                if tick >= 60:
                    row[2] += 2.4
                rows.append(row)

            r_sift = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "sift_stream",
                        "arguments": {
                            "session_id": "ops1",
                            "rows": rows,
                            "names": [f"host_{i}" for i in range(8)],
                            "alpha": 0.05,
                            "warmup": 32,
                            "max_channels": 5,
                            "ring_capacity": 200,
                        },
                    },
                },
            )
            self.assertFalse(r_sift["result"]["isError"])
            sift_data = json.loads(r_sift["result"]["content"][0]["text"])
            self.assertEqual(sift_data["n_channels"], 8)
            self.assertEqual(sift_data["ticks"], 160)
            self.assertIn(2, sift_data["rejected_channels"])
            self.assertGreater(sift_data["token_savings"]["compression_ratio"], 1.0)
            self.assertIn("STREAMSIEVE BRIEF", sift_data["compact_text"])
            handle_ch2 = next(item["handle"] for item in sift_data["top"] if item["channel"] == 2)

            # Also verify sift_stream accepts CSV text
            csv_lines = ["a,b", "0.1,0.2", "0.2,0.1", "0.0,0.3"]
            r_csv = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {
                        "name": "sift_stream",
                        "arguments": {"session_id": "csv_s", "csv_text": "\n".join(csv_lines), "warmup": 2},
                    },
                },
            )
            self.assertFalse(r_csv["result"]["isError"])
            csv_data = json.loads(r_csv["result"]["content"][0]["text"])
            self.assertEqual(csv_data["ticks"], 3)

            # Tool 2: fetch_raw_ticks using exact handle returned by sift_stream
            r_raw = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "tools/call",
                    "params": {
                        "name": "fetch_raw_ticks",
                        "arguments": {"session_id": "ops1", "handle": handle_ch2, "limit": 12},
                    },
                },
            )
            self.assertFalse(r_raw["result"]["isError"])
            raw_data = json.loads(r_raw["result"]["content"][0]["text"])
            self.assertEqual(raw_data["channel"], 2)
            self.assertEqual(raw_data["name"], "host_2")
            self.assertEqual(raw_data["count"], 12)
            self.assertEqual(raw_data["ticks"][-1][0], 160)
            self.assertAlmostEqual(raw_data["ticks"][-1][1], rows[-1][2], places=12)

            # Tool 3: lattice_observe (first observation + duplicate echo on latA; separate evidence on latB)
            r_obs1 = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {
                        "name": "lattice_observe",
                        "arguments": {
                            "lattice_id": "latA",
                            "hyp_id": "incident_42",
                            "provenance_id": "trace:001",
                            "llr": 1.6,
                            "agent_id": 0,
                            "timestamp_ns": 100,
                        },
                    },
                },
            )
            obs1 = json.loads(r_obs1["result"]["content"][0]["text"])
            self.assertFalse(obs1["was_echo"])
            self.assertEqual(obs1["summary"]["unique_evidence"], 1)

            r_obs_echo = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 21,
                    "method": "tools/call",
                    "params": {
                        "name": "lattice_observe",
                        "arguments": {
                            "lattice_id": "latA",
                            "hyp_id": "incident_42",
                            "provenance_id": "trace:001",
                            "llr": 1.6,
                            "agent_id": 1,
                            "timestamp_ns": 200,
                        },
                    },
                },
            )
            obs_echo = json.loads(r_obs_echo["result"]["content"][0]["text"])
            self.assertTrue(obs_echo["was_echo"])
            self.assertEqual(obs_echo["summary"]["echoes_suppressed"], 1)
            self.assertEqual(obs_echo["summary"]["unique_evidence"], 1)

            self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 22,
                    "method": "tools/call",
                    "params": {
                        "name": "lattice_observe",
                        "arguments": {
                            "lattice_id": "latB",
                            "hyp_id": "incident_42",
                            "provenance_id": "trace:002",
                            "llr": 1.1,
                            "agent_id": 2,
                            "timestamp_ns": 150,
                        },
                    },
                },
            )

            # Also build a Base85 payload externally to test wire merge
            with EvidenceLattice() as ext_lat:
                ext_lat.observe("incident_42", "trace:003", 0.7, agent_id=3, timestamp_ns=175)
                ext_b85 = ext_lat.to_base85()

            # Tool 4: lattice_merge (merges session latB + external base85 into latA)
            r_merge = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 23,
                    "method": "tools/call",
                    "params": {
                        "name": "lattice_merge",
                        "arguments": {
                            "target_id": "latA",
                            "source_id": "latB",
                            "source_base85": ext_b85,
                            "return_base85": True,
                        },
                    },
                },
            )
            self.assertFalse(r_merge["result"]["isError"])
            merge_data = json.loads(r_merge["result"]["content"][0]["text"])
            self.assertIn("base85", merge_data)
            self.assertGreater(merge_data["wire_bytes"], 20)

            # Tool 5: lattice_query
            r_q = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 24,
                    "method": "tools/call",
                    "params": {
                        "name": "lattice_query",
                        "arguments": {"lattice_id": "latA", "hyp_id": "incident_42"},
                    },
                },
            )
            q_data = json.loads(r_q["result"]["content"][0]["text"])
            self.assertEqual(q_data["summary"]["unique_evidence"], 3)
            self.assertEqual(q_data["summary"]["echoes_suppressed"], 1)
            self.assertEqual(q_data["summary"]["distinct_agents"], 3)
            self.assertAlmostEqual(q_data["summary"]["total_log_odds"], 3.4, places=10)

            # Tool 6: voi_allocate
            r_voi = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 30,
                    "method": "tools/call",
                    "params": {
                        "name": "voi_allocate",
                        "arguments": {
                            "sifter_id": "fleet1",
                            "n_channels": 32,
                            "n_sites": 4,
                            "observations": [
                                {"kind": "ticks", "target_id": 2, "value": [0.0, 3.8, -0.1, 3.6, 0.1, 0.0, -0.2, 0.1]}
                            ],
                            "budget_per_agent": 2,
                            "n_agents": 4,
                        },
                    },
                },
            )
            self.assertFalse(r_voi["result"]["isError"])
            voi_data = json.loads(r_voi["result"]["content"][0]["text"])
            self.assertEqual(voi_data["n_actions"], 8)
            pairs = [(a["tool_kind"], a["target_id"]) for a in voi_data["actions"]]
            self.assertEqual(len(pairs), len(set(pairs)))
            self.assertEqual(voi_data["top_posteriors"][0]["channel"], 2)

            # Tool 7: trust_step (predict_prob, resolve, predict_scalar, status)
            r_tp = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 40,
                    "method": "tools/call",
                    "params": {
                        "name": "trust_step",
                        "arguments": {
                            "calibrator_id": "arb1",
                            "action": "predict_prob",
                            "decision_id": 100,
                            "agent_probs": [0.9, 0.5, 0.1],
                            "underage_cost": 4.0,
                            "overage_cost": 1.0,
                        },
                    },
                },
            )
            self.assertFalse(r_tp["result"]["isError"])
            tp_data = json.loads(r_tp["result"]["content"][0]["text"])
            self.assertEqual(tp_data["decision"]["active_agents"], 3)
            self.assertEqual(tp_data["decision"]["optimal_action"], 1.0)

            r_res = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 41,
                    "method": "tools/call",
                    "params": {
                        "name": "trust_step",
                        "arguments": {
                            "calibrator_id": "arb1",
                            "action": "resolve",
                            "decision_id": 100,
                            "outcome": 1.0,
                        },
                    },
                },
            )
            res_data = json.loads(r_res["result"]["content"][0]["text"])
            self.assertGreater(res_data["agents"][0]["weight"], res_data["agents"][2]["weight"])

            r_sc = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 42,
                    "method": "tools/call",
                    "params": {
                        "name": "trust_step",
                        "arguments": {
                            "calibrator_id": "arb_scalar",
                            "action": "predict_scalar",
                            "decision_id": "ord-1",
                            "agent_means": [10.0, 11.0],
                            "agent_stds": [1.0, 1.5],
                            "underage_cost": 3.0,
                            "overage_cost": 1.0,
                        },
                    },
                },
            )
            self.assertFalse(r_sc["result"]["isError"])
            sc_data = json.loads(r_sc["result"]["content"][0]["text"])
            self.assertGreater(sc_data["decision"]["optimal_action"], sc_data["decision"]["fused_point"])

            # Error handling: unknown method -> JSON-RPC -32601
            r_err = self._rpc(proc, {"jsonrpc": "2.0", "id": 99, "method": "nonexistent/method"})
            self.assertEqual(r_err["error"]["code"], -32601)

            proc.stdin.close()
            rc = proc.wait(timeout=5)
            self.assertEqual(rc, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    def test_mcp_content_length_framing(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "streamsieve.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_repo_python_env(),
        )
        try:
            assert proc.stdin is not None and proc.stdout is not None
            body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode("utf-8")
            frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
            proc.stdin.write(frame)
            proc.stdin.flush()

            hdr = proc.stdout.readline()
            self.assertTrue(hdr.lower().startswith(b"content-length:"))
            resp_len = int(hdr.split(b":", 1)[1].strip())
            blank = proc.stdout.readline()
            self.assertIn(blank, (b"\r\n", b"\n"))
            resp_body = proc.stdout.read(resp_len)
            resp = json.loads(resp_body.decode("utf-8"))
            self.assertEqual(resp["id"], 7)
            self.assertEqual(resp["result"], {})

            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=5), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
