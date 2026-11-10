"""leakscan.py: the capture gate finds injection tool command lines in process_meta (v4 C6b review 3, low 2).

The MySQL export launcher of C6b's second revision (mysqlsh arming SIGALRM, then os.execvp into mysql) and the export
Job's receive-buffer sysctl would be injection machinery if a process table sample ever caught them. The launcher is
gone; this keeps such a command line from coming back unnoticed.
"""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
