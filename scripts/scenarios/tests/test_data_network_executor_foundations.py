from __future__ import annotations

import copy
import importlib.util
import json
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


db_ddl = load("db_ddl_executor")
kafka = load("kafka_control_executor")
network = load("network_fault_executor")


def plan(profile_id: str, scenario_id: str, params: dict, location: dict) -> dict:
    return {
        "scenario": {"id": scenario_id},
        "profile_instances": [{
            "profile_id": profile_id,
            "parameters": copy.deepcopy(params),
            "location": location,
        }],
    }


class DataNetworkExecutorFoundationTests(unittest.TestCase):
    def test_f02r_and_f04r_controller_evaluation_contracts(self) -> None:
        controllers = json.loads((ROOT / "registry" / "controllers.json").read_text())
        self.assertEqual(
            controllers["live_scenario_ids"][-6:],
            ["F02-R", "F04-R", "F12-H", "F06-G", "F05-R", "F05-H"],
        )

        f02 = controllers["controllers"]["F02-R"]
        self.assertEqual(f02["profile"]["levels"][0]["parameters"], db_ddl.CONTRACTS["F02-R"])
        self.assertEqual(
            {(rule["observation"], rule["op"], rule["value"]) for rule in f02["success"]["all"]},
            {("index_present", "eq", False), ("entry_status", "ne", 0)},
        )

        f04 = controllers["controllers"]["F04-R"]
        self.assertEqual(f04["profile"]["levels"][0]["parameters"], kafka.CONTRACTS["F04-R"])
        self.assertEqual(
            {(rule["observation"], rule["op"], rule["value"]) for rule in f04["success"]["all"]},
            {("shipping_replicas", "eq", 0), ("shipping_lag", "gt", 0)},
        )
        self.assertEqual(
            {rule["observation"] for rule in f04["must_rule_out"]["any"]},
            {"entry_status", "pod_ready"},
        )

    def test_f04r_kafka_contract_snapshots_replicas_and_requires_lag_drain(self) -> None:
        params = kafka.CONTRACTS["F04-R"]
        kafka.validate("F04-R", params, {})
        invocation, stdin = kafka.build_invocation(plan(
            "kafka.control", "F04-R", params,
            {"transport": "kubectl", "namespace": "rca-testbed-commerce"},
        ), "cleanup")
        self.assertEqual(invocation[0], "/usr/bin/bash")
        script = stdin.decode()
        self.assertIn('current_replicas >"$state.tmp"', script)
        self.assertIn('--replicas="$original"', script)
        self.assertIn("kafka-consumer-groups.sh", script)
        self.assertIn("wait_lag_zero", script)
        self.assertIn('[[ "$(lag)" == "0" ]]', script)

    def test_kafka_rejects_unverified_outbox_and_rate_control_surfaces(self) -> None:
        for scenario_id in ("F04-H", "F04-P"):
            with self.assertRaisesRegex(kafka.ExecutorError, "no verified independent"):
                kafka.validate(scenario_id, {}, {})

    def test_f02r_postgres_contract_snapshots_exact_inverse_ddl(self) -> None:
        params = db_ddl.CONTRACTS["F02-R"]
        db_ddl.validate("F02-R", params, {})
        invocation, stdin = db_ddl.build_invocation(plan(
            "db.ddl", "F02-R", params,
            {"transport": "ssh", "host": "192.168.122.206", "user": "nkia"},
        ), "run")
        self.assertEqual(invocation[0], "/usr/bin/ssh")
        self.assertIn("BatchMode=yes", invocation)
        self.assertIn("StrictHostKeyChecking=yes", invocation)
        script = stdin.decode()
        self.assertIn('current_definition >"$state.tmp"', script)
        self.assertIn('sql "$original;"', script)
        self.assertIn('[[ "$original" == "$expected" ]]', script)
        self.assertIn("pg_stat_activity", script)

    def test_db_ddl_rejects_mysql_without_verified_client_and_credentials(self) -> None:
        with self.assertRaisesRegex(db_ddl.ExecutorError, "no verified database client"):
            db_ddl.validate("F02-P", {}, {})

    def test_exact_contracts_reject_parameter_drift(self) -> None:
        kafka_params = copy.deepcopy(kafka.CONTRACTS["F04-R"])
        kafka_params["consumer_group"] = "shipping-service-copy"
        with self.assertRaisesRegex(kafka.ExecutorError, "exactly match"):
            kafka.validate("F04-R", kafka_params, {})
        db_params = copy.deepcopy(db_ddl.CONTRACTS["F02-R"])
        db_params["index"] = "idx_products_category"
        with self.assertRaisesRegex(db_ddl.ExecutorError, "exactly match"):
            db_ddl.validate("F02-R", db_params, {})

    def test_all_current_network_scenarios_fail_closed(self) -> None:
        for scenario_id, reason in network.BLOCKED.items():
            with self.subTest(scenario_id=scenario_id):
                with self.assertRaisesRegex(network.ExecutorError, reason.split()[0]):
                    network.validate(scenario_id, {}, {})
                with self.assertRaises(network.ExecutorError):
                    network.build_invocation({"scenario": {"id": scenario_id}}, "run")


if __name__ == "__main__":
    unittest.main()
