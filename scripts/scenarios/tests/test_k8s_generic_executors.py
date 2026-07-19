from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
sys.path.insert(0, str(PROFILES))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, PROFILES / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resource = load("k8s_resource_executor")
probe = load("k8s_probe_executor")
env = load("k8s_env_executor")


def plan(profile_id: str, scenario_id: str, parameters: dict) -> dict:
    return {
        "scenario": {"id": scenario_id},
        "profile_instances": [{"profile_id": profile_id, "parameters": parameters}],
    }


class GenericKubernetesExecutorTests(unittest.TestCase):
    def test_resource_executor_supports_only_named_payment_memory_and_inventory_cpu(self) -> None:
        payment = resource.F05_R_LEVELS[1]
        inventory = {
            "namespace": "rca-testbed-commerce", "deployment": "testbed-inventory",
            "container": "inventory-service", "resource": "cpu",
            "baseline": {"requests": {"cpu": "200m"}, "limits": {"cpu": "500m"}},
            "fault": {"requests": {"cpu": "50m"}, "limits": {"cpu": "100m"}},
        }
        resource.validate("F05-R", payment, {})
        resource.validate("F09-P", inventory, {})
        tampered = dict(payment, deployment="testbed-order")
        with self.assertRaisesRegex(resource.ExecutorError, "not allowlisted"):
            resource.validate("F05-R", tampered, {})
        with self.assertRaisesRegex(resource.ExecutorError, "not allowlisted"):
            resource.validate("F12-H", inventory, {})

    def test_resource_script_snapshots_then_restores_exact_original(self) -> None:
        params = resource.F05_R_LEVELS[2]
        argv, stdin = resource.build_invocation(plan(resource.PROFILE_ID, "F05-R", params), "run")
        self.assertEqual(argv[:3], ["/usr/bin/bash", "-s", "--"])
        self.assertIn("rca-testbed-commerce", argv)
        script = stdin.decode()
        self.assertIn("'{original:$original,applied:$applied}'", script)
        self.assertIn("[[ \"$(current)\" == \"$applied\" ]]", script)
        self.assertIn("original=$(jq -Sc '.original'", script)
        self.assertIn('patch "$original"', script)
        self.assertIn("--type=strategic", script)
        self.assertNotIn("kubectl exec", script)

    def test_probe_executor_allows_only_payment_liveness(self) -> None:
        params = probe.F05_H_PARAMETERS
        probe.validate("F05-H", params, {})
        with self.assertRaisesRegex(probe.ExecutorError, "not allowlisted"):
            probe.validate("F05-H", dict(params, probe="readinessProbe"), {})
        script = probe.build_invocation(plan(probe.PROFILE_ID, "F05-H", params), "cleanup")[1].decode()
        self.assertIn('arg p "$probe"', script)
        self.assertIn('patch "$original"', script)

    def test_env_executor_whitelists_target_and_changed_key(self) -> None:
        hikari = {
            "namespace": "rca-testbed-commerce", "deployment": "testbed-payment", "container": "payment-service",
            "baseline": [{"name": "SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE", "value": "10"}, {"name": "SAFE", "value": "x"}],
            "fault": [{"name": "SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE", "value": "2"}, {"name": "SAFE", "value": "x"}],
        }
        gc = {
            "namespace": "rca-testbed-commerce", "deployment": "testbed-order", "container": "order-service",
            "baseline": [{"name": "JAVA_TOOL_OPTIONS", "value": "-XX:MaxRAMPercentage=75"}],
            "fault": [{"name": "JAVA_TOOL_OPTIONS", "value": "-XX:MaxRAMPercentage=35"}],
        }
        env.validate("F03-P", hikari, {})
        env.validate("F09-H", gc, {})
        unsafe = dict(hikari, fault=hikari["fault"] + [{"name": "SAFE", "value": "changed"}])
        with self.assertRaisesRegex(env.ExecutorError, "non-allowlisted key"):
            env.validate("F03-P", unsafe, {})
        script = env.build_invocation(plan(env.PROFILE_ID, "F09-H", gc), "recovery")[1].decode()
        self.assertIn("(.env // [])", script)
        self.assertIn('[[ ! -e "$state" ]]', script)

    def test_registry_gate_is_fail_closed_when_present(self) -> None:
        params = resource.F05_R_LEVELS[0]
        with self.assertRaisesRegex(resource.ExecutorError, "not enabled"):
            resource.validate("F05-R", params, {"parameter_contract": {"allowed_scenarios": []}})


if __name__ == "__main__":
    unittest.main()
