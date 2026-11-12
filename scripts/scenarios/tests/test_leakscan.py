"""leakscan.py: the capture gate finds injection tool command lines in process_meta (v4 C6b review 3, low 2).

The MySQL export launcher of C6b's second revision (mysqlsh arming SIGALRM, then os.execvp into mysql) and the export
Job's receive-buffer sysctl would be injection machinery if a process table sample ever caught them. The launcher is
gone; this keeps such a command line from coming back unnoticed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

LEAKSCAN = Path(__file__).resolve().parents[1] / "leakscan.py"

CLEAN = [
    "mysqld",
    "mysql --defaults-extra-file=/dev/stdin -ufooddelivery --quick -N -B fooddelivery -e SELECT id FROM dispatches",
    "psql -X -q -c COPY (SELECT id FROM inventory_schema.outbox_events) TO STDOUT",
    'awk -v r=171 NR % r == 0 { system("sleep 1") }',
    "sysctl -a",
    "/usr/bin/alarmd --config /etc/alarm.conf",
    "python3 -m http.server",
    "mysqlsh --sql -e SELECT 1",
]
LEAKS = {
    "launcher": "mysqlsh --no-defaults --log-file=/dev/null --py -e import os, signal; signal.alarm(780); "
                "a = [\"mysql\"]; os.execvp(a[0], a)",
    "alarm": "python3 -c import signal; signal.alarm(5)",
    "execvp": "python3 -c import os; os.execvp('mysql', ['mysql'])",
    "mysqlsh_py": "mysqlsh --py -e print(1)",
    "tcp_rmem": "sysctl -w net.ipv4.tcp_rmem=4096 8192 8192",
}


def _case(root: Path, cmdlines: list[str]) -> Path:
    (root / "input").mkdir(parents=True)
    table = pa.table({"proc_key": [f"k{i}" for i in range(len(cmdlines))], "cmdline": cmdlines})
    pq.write_table(table, root / "input" / "process_meta.parquet")
    return root


def _scan(case: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(LEAKSCAN), "--json", str(case)], capture_output=True, text=True,
                          timeout=60)


class LeakscanToolCommandLines(unittest.TestCase):
    def test_tool_command_lines_in_process_meta_fail_the_case(self) -> None:
        for name, cmdline in LEAKS.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as raw:
                done = _scan(_case(Path(raw), CLEAN + [cmdline]))
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertIn('"inject_path"', done.stdout)

    def test_the_clients_and_readers_of_a_normal_run_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            done = _scan(_case(Path(raw), CLEAN))
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn('"verdict": "clean"', done.stdout)


# Values an injection used to leave in what KCM, kubelet, the mock and the apps record (v4 LEAK, 2026-11-12).
# The old pattern was upper case only and passed every one of them except the label value.
ID_FORMS = {
    "job_name": ("inject_name", "scenario-f03-h-order-thread-pool-4jcn9"),
    "label_key": ("inject_tag", '{"app": "pricing", "rca-scenario": "F08-R"}'),
    "annotation_key": ("inject_tag", '{"rca.lucida.ai/f08-h-rollout": "20260825T062642Z"}'),
    "image_tag": ("inject_name", 'Container image "commerce-payment:scenario-invalid-f05-g" is not present'),
    "probe_path": ("scenario_id", 'Readiness probe failed: HTTP probe failed with statuscode: 404 /actuator/health/f16-h-fail'),
    "mock_reply": ("inject_name", "PG response: APPROVED, txId: scenario-delay"),
    "session_tag_lower": ("inject_tag", "rca-f02-r-index-ddl"),
    "process_name": ("inject_tag", "rca-F12-G-external-nping --tcp -p 39999"),
}
# What a normal recording is full of and must stay clean under the wider, case-blind patterns.
ORDINARY = [
    "rca-testbed-commerce/testbed-order-7f9c8d6b5-x2v4q",
    "running (0h12m30.0s), 30/30 VUs, 1 scenario, 30 max VUs",
    "kubectl.kubernetes.io/restartedAt: 2026-11-12T03:04:05Z",
    "order-report-export-4jcn9",
    "commerce-payment:1.4.2",
    "6f1e2a90-f12a-4c3b-9d5e-0a1b2c3d4e5f",
    "sha256:3f12e4b1c0de",
    "buf12-a overflow f12-ab f123-a xf12-a",
    "pgAdmin 4 - DB:commerce",
    "/scripts/load.js",
]


def _csv_case(root: Path, cells: list[str]) -> Path:
    import csv as csvmod

    (root / "input").mkdir(parents=True)
    with (root / "input" / "kcm_resources_history.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csvmod.writer(fh)
        writer.writerow(["resource_id", "spec"])
        for i, cell in enumerate(cells):
            writer.writerow([f"r{i}", cell])
    return root


class LeakscanScenarioIdForms(unittest.TestCase):
    def test_lower_case_ids_and_tool_names_fail_the_case(self) -> None:
        for name, (pattern, value) in ID_FORMS.items():
            for layout in ("csv", "parquet", "dictionary"):
                with self.subTest(name, layout=layout), tempfile.TemporaryDirectory() as raw:
                    if layout == "csv":
                        case = _csv_case(Path(raw), ORDINARY + [value])
                    elif layout == "parquet":
                        case = _case(Path(raw), ORDINARY + [value])
                    else:
                        (Path(raw) / "input").mkdir(parents=True)
                        column = pa.array(ORDINARY + [value]).dictionary_encode()
                        pq.write_table(pa.table({"cmdline": column}), Path(raw) / "input" / "process_meta.parquet")
                        case = Path(raw)
                    done = _scan(case)
                    self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                    self.assertIn(f'"{pattern}"', done.stdout)

    def test_values_inside_map_list_and_binary_columns_are_seen(self) -> None:
        # otel_traces, lucida_logs and lucida_events keep attributes in map and list columns.
        value = "scenario-f03-h-order-thread-pool-4jcn9"
        tables = {
            "map_value": pa.table({"span_attributes": pa.array([[("k8s.pod.name", "a")], [("k8s.pod.name", value)]],
                                                               type=pa.map_(pa.string(), pa.string()))}),
            "map_key": pa.table({"span_attributes": pa.array([[("rca.lucida.ai/f08-h-rollout", "x")]],
                                                             type=pa.map_(pa.string(), pa.string()))}),
            "list": pa.table({"events_name": pa.array([["ok"], ["fine", value]], type=pa.list_(pa.string()))}),
            "list_of_maps": pa.table({"events_attributes": pa.array([[[("message", value)]]],
                                                                    type=pa.list_(pa.map_(pa.string(), pa.string())))}),
            "fixed_binary": pa.table({"trace_id": pa.array([b"scenario-f03-h-x" + b"0" * 16], type=pa.binary(32))}),
        }
        for name, table in tables.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as raw:
                (Path(raw) / "input").mkdir()
                pq.write_table(table, Path(raw) / "input" / "otel_traces_local.parquet")
                done = _scan(Path(raw))
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "input").mkdir()
            pq.write_table(pa.table({
                "span_attributes": pa.array([[("k8s.namespace.name", "rca-testbed-commerce")]],
                                            type=pa.map_(pa.string(), pa.string())),
                "trace_id": pa.array([bytes(range(32))], type=pa.binary(32)),
            }), Path(raw) / "input" / "otel_traces_local.parquet")
            self.assertEqual(_scan(Path(raw)).returncode, 0)

    def test_nested_columns_yield_only_the_rows_of_their_slice(self) -> None:
        # MapArray.keys and .items ignore a slice; the candidates must still be the batch's own rows.
        import importlib.util

        spec = importlib.util.spec_from_file_location("leakscan_slices", LEAKSCAN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        maps = pa.array([[("a", "scenario-x1")], [("k", "v")], [("z", "scenario-z2")]],
                        type=pa.map_(pa.string(), pa.string()))
        self.assertEqual(module._loose_candidates(maps.slice(0, 1)), ["scenario-x1"])
        self.assertEqual(module._loose_candidates(maps.slice(1, 1)), [])
        self.assertEqual(module._loose_candidates(maps.slice(1)), ["scenario-z2"])
        nested = pa.array([[[("m", "scenario-q")]], [[("n", "ok")]]], type=pa.list_(pa.map_(pa.string(), pa.string())))
        self.assertEqual(module._loose_candidates(nested.slice(1)), [])
        self.assertEqual(module._loose_candidates(nested.slice(0, 1)), ["scenario-q"])

    def test_ordinary_values_stay_clean_under_the_case_blind_patterns(self) -> None:
        for layout, build in (("csv", _csv_case), ("parquet", _case)):
            with self.subTest(layout), tempfile.TemporaryDirectory() as raw:
                done = _scan(build(Path(raw), ORDINARY + CLEAN))
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_the_old_upper_case_findings_still_count(self) -> None:
        for value in ("program=rca-F01-R-inventory-lock", "COMMAND=/usr/bin/bash -s -- run F15-P pressure",
                      "/var/lib/lucida/scenario-profile-state/F09-R.pgid"):
            with self.subTest(value), tempfile.TemporaryDirectory() as raw:
                done = _scan(_case(Path(raw), [value]))
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)


class LeakscanCaptureMeta(unittest.TestCase):
    def test_topology_bundle_manifest_is_capture_meta_and_its_snapshots_are_still_scanned(self) -> None:
        """topology/manifest.json carries the case_id and run_id (`F01-H-run-...`) and is never loaded into
        the product by the evaluation restore; the topology snapshots next to it are product responses."""
        manifest = json.dumps({"case_id": "case-f01-h-v3-48ec8e62", "run_id": "F01-H-run-48ec8e62"})
        for layout in ("input", ""):
            with self.subTest(layout=layout or "old"), tempfile.TemporaryDirectory() as raw:
                base = Path(raw) / layout if layout else Path(raw)
                (base / "topology" / "graph").mkdir(parents=True)
                (base / "topology" / "manifest.json").write_text(manifest)
                (base / "topology" / "graph" / "2026-08-25T04-22-13Z.json").write_text('{"nodes": []}')
                self.assertEqual(_scan(Path(raw)).returncode, 0, _scan(Path(raw)).stdout)
                (base / "topology" / "graph" / "2026-08-25T04-22-13Z.json").write_text(manifest)
                self.assertEqual(_scan(Path(raw)).returncode, 1)
                (base / "topology" / "graph" / "2026-08-25T04-22-13Z.json").write_text('{"nodes": []}')
                (base / "data").mkdir()
                (base / "data" / "manifest.json").write_text(manifest)  # only the topology bundle's list is skipped
                self.assertEqual(_scan(Path(raw)).returncode, 1)


class LeakscanTextFiles(unittest.TestCase):
    def test_text_scan_counts_what_a_line_by_line_scan_counts_across_block_edges(self) -> None:
        import collections
        import importlib.util

        spec = importlib.util.spec_from_file_location("leakscan_under_test", LEAKSCAN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        lines = ORDINARY + [value for _, value in ID_FORMS.values()] + [
            "two in one line: scenario-f03-h and rca-scenario: F03-H",
            "",
            "x" * 300 + " tail F09-R " + "y" * 50,
            "\u0130stanbul office: scenario-f03-h",  # lower() makes this line longer: the slow path
        ]
        text = "\n".join(lines * 7)  # no newline at the end: the last line is a tail

        def reference():
            found = {k: collections.Counter() for k in module.PATTERNS}
            for line in text.split("\n"):
                module._hit(line, found, collections.defaultdict(set), {}, "f")
            return found

        expected = reference()
        self.assertTrue(any(expected.values()))
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "logs.jsonl"
            path.write_text(text, encoding="utf-8")
            for block in (7, 64, 1000, 1 << 24):
                with self.subTest(block=block):
                    module._TEXT_BLOCK = block
                    found = {k: collections.Counter() for k in module.PATTERNS}
                    module.scan_text(path, found, collections.defaultdict(set), {})
                    self.assertEqual(found, expected)


if __name__ == "__main__":
    unittest.main()
