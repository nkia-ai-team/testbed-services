"""녹화 전 이미지 점검 (scripts/scenarios/image-drift-check.py)."""
from __future__ import annotations

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
_spec = importlib.util.spec_from_file_location("image_drift_check", ROOT / "image-drift-check.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _workload(namespace, name, containers, *, kind="Deployment", annotations=None, env=None):
    return {
        "kind": kind,
        "metadata": {"namespace": namespace, "name": name, "annotations": annotations or {}},
        "spec": {
            "template": {
                "metadata": {"annotations": annotations or {}},
                "spec": {
                    "containers": [
                        {"name": c, "image": image, "env": env or []} for c, image in containers.items()
                    ]
                },
            }
        },
    }


def _live_from_manifests(expected):
    return {
        "items": [
            _workload(namespace, name, containers, kind=kind)
            for (kind, namespace, name), containers in expected.items()
        ]
    }


class ImageDriftCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = check.load_manifests(REPO)

    def _run(self, document) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(document))), redirect_stdout(out):
            code = check.main(["--manifest-root", str(REPO)])
        return code, out.getvalue()

    def test_reads_every_app_and_store_workload_from_the_manifests(self) -> None:
        self.assertEqual(
            self.expected[("Deployment", "rca-testbed-banking", "testbed-transfer")],
            {"transfer-service": "core-banking-transfer:latest"},
        )
        self.assertEqual(
            self.expected[("StatefulSet", "rca-testbed-commerce", "testbed-postgres")],
            {"postgres": "postgres:16-alpine"},
        )
        self.assertGreaterEqual(len(self.expected), 30)

    def test_decoy_and_routine_deployments_pass(self) -> None:
        """v4 미끼는 이미지를 바꾸지 않는다: 재시작 표식, annotation, env, ConfigMap 값만."""
        document = _live_from_manifests(self.expected)
        for item in document["items"]:
            item["metadata"]["annotations"]["kubernetes.io/change-cause"] = "revision abc123"
            item["spec"]["template"]["metadata"]["annotations"] = {
                "kubectl.kubernetes.io/restartedAt": "2026-11-10T01:20:00Z"
            }
        code, out = self._run(document)
        self.assertEqual(code, 0, out)

    def test_foreign_image_of_another_service_is_caught(self) -> None:
        """2026-10-24 F77-R: transfer 에 ledger 이미지가 남은 잔류. 목록 어딘가엔 있는 이미지라
        예전 점검은 통과시켰다."""
        document = _live_from_manifests(self.expected)
        for item in document["items"]:
            if item["metadata"]["name"] == "testbed-transfer":
                item["spec"]["template"]["spec"]["containers"][0]["image"] = "core-banking-ledger:latest"
        code, out = self._run(document)
        self.assertEqual(code, 1)
        self.assertIn("rca-testbed-banking/testbed-transfer transfer-service: core-banking-ledger:latest", out)
        self.assertIn("기대 core-banking-transfer:latest", out)

    def test_fault_tag_and_unknown_objects_are_still_caught(self) -> None:
        document = _live_from_manifests(self.expected)
        for item in document["items"]:
            if item["metadata"]["name"] == "testbed-pricing":
                item["spec"]["template"]["spec"]["containers"][0]["image"] = "commerce-pricing:fault-1"
        document["items"].append(
            _workload("rca-testbed-commerce", "left-behind", {"x": "busybox:latest"})
        )
        document["items"].append(
            _workload("kube-system", "coredns", {"coredns": "registry.k8s.io/coredns:v1"})
        )
        code, out = self._run(document)
        self.assertEqual(code, 1)
        self.assertIn("testbed-pricing pricing-service: commerce-pricing:fault-1", out)
        self.assertIn("left-behind x: busybox:latest", out)
        self.assertNotIn("coredns", out)

    def test_unreadable_input_fails_the_check(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("not json")), redirect_stdout(out):
            self.assertEqual(check.main(["--manifest-root", str(REPO)]), 2)
        self.assertEqual(self._run({"items": []})[0], 2)


if __name__ == "__main__":
    unittest.main()
