#!/usr/bin/env python3
"""Build and validate deterministic, JSON-compatible YAML scenario manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
CATALOG_PATH = SCRIPT_DIR / "catalog.json"
MANIFEST_DIR = SCRIPT_DIR / "manifests"
CONTROLLERS_PATH = SCRIPT_DIR / "registry" / "controllers.json"
EXECUTION_MATRIX_PATH = SCRIPT_DIR.parent.parent / "docs" / "spec-scenario-execution-matrix.md"

COMMON_PREFLIGHTS = (
    "catalog-integrity",
    "canonical-kubeconfig",
    "global-dirty-lease",
    "baseline-clean-window",
    "target-health",
    "profile-prerequisites",
)

PROFILE_PREFLIGHTS = {
    "db.lock": "db-session-tag-clean",
    "db.ddl": "db-inverse-ddl-ready",
    "db.workload": "db-session-tag-clean",
    "mock.expectation": "mock-restore-contract",
    "load.north_south": "baseline-loadgen-active",
    "load.east_west": "east-west-job-contract",
    "k8s.patch": "kubernetes-original-spec-snapshot",
    "k8s.env": "kubernetes-original-env-snapshot",
    "k8s.lifecycle": "kubernetes-recovery-capacity",
    "k8s.resource": "kubernetes-original-resource-snapshot",
    "k8s.probe": "kubernetes-original-probe-snapshot",
    "kafka.control": "kafka-drain-capacity",
    "host.stress": "host-placement-and-oob-recovery",
    "cache.control": "cache-warmup-contract",
    "network.fault": "network-oob-recovery",
    "app.release": "rollback-artifact",
    "wpm.probe": "wpm-probe-contract",
    "business.fault": "business-invariant-probe",
    "timeline.compose": "subinjection-timeline",
    "timeline.multi": "subinjection-timeline",
}

TRANSPORT_PREFLIGHTS = {
    "ssh": "transport-ssh-access",
    "kubectl": "transport-kubernetes-rbac",
    "api-via-kubectl": "transport-api-contract",
    "local-orchestrator": "transport-local-runner-state",
    "unresolved": "transport-resolution-blocker",
}


def transport_for(location: str) -> str:
    """Map the execution-matrix location notation to a normalized transport."""
    lowered = location.lower()
    if location == "scenario-runner":
        return "local-orchestrator"
    if "external-pg-mock" in lowered:
        return "api-via-kubectl"
    if any(token in lowered for token in ("cross-domain edge", "probe edge", "probe location")):
        return "unresolved"
    if any(token in lowered for token in ("namespace", "edge route")):
        return "kubectl"
    if any(token in lowered for token in ("tb-runner", "worker", "batch host", "external-server")):
        return "ssh"
    raise ValueError(f"unmapped injection location: {location}")


def controller_contract(load_mode: str, slug: str) -> dict[str, Any]:
    decision_mode = {
        "adaptive": "calibration",
        "fixed": "evaluation",
        "no-load": "none",
    }[load_mode]
    return {
        "dispatcher_mode": "dry-run",
        "decision_mode": decision_mode,
        "profile": {
            "kind": load_mode,
            "ref": f"controller-profiles/{slug}/{load_mode}",
            "requires_approved_fixed_profile": load_mode == "fixed",
        },
        "live_enabled": False,
    }


def live_controller_contract(controller: dict[str, Any]) -> dict[str, Any]:
    """Split registry trust metadata from the runner's human controller schema."""
    runtime = json.loads(json.dumps(controller))
    dispatcher_mode = runtime.pop("dispatcher_mode")
    live_enabled = runtime.pop("live_enabled")
    profile = runtime["profile"]
    binding = {
        "primary_ref": profile.pop("primary_ref"),
        "companion_refs": profile.pop("companion_refs"),
    }
    approved_profile_id = profile.pop("approved_profile_id")
    if approved_profile_id is not None:
        binding["approved_profile_id"] = approved_profile_id
    return {
        "dispatcher_mode": dispatcher_mode,
        "live_enabled": live_enabled,
        "binding": binding,
        "runtime": runtime,
    }


def build_manifest(
    row: dict[str, Any],
    matrix_location_transport: str,
    controllers: dict[str, Any],
) -> dict[str, Any]:
    transport = transport_for(row["injection_location"])
    preflights = list(COMMON_PREFLIGHTS)
    preflights.append(TRANSPORT_PREFLIGHTS[transport])
    for profile in row["profiles"]:
        preflight = PROFILE_PREFLIGHTS[profile]
        if preflight not in preflights:
            preflights.append(preflight)

    prerequisite = row["prerequisite"].strip()
    return {
        "schema_version": "1.0",
        "id": row["id"],
        "slug": row["slug"],
        "readiness": row["readiness"],
        "authoritative_sources": {
            "catalog": "scripts/scenarios/catalog.json",
            "execution_matrix": "docs/spec-scenario-execution-matrix.md",
        },
        "injection": {
            "location": row["injection_location"],
            "matrix_location_transport": matrix_location_transport,
            "transport": transport,
            "profile_refs": [f"injector-profiles/{profile}" for profile in row["profiles"]],
        },
        "execution": {
            "preflight_ids": preflights,
            "controller": (
                live_controller_contract(controllers[row["id"]])
                if row["id"] in controllers
                else controller_contract(row["load_mode"], row["slug"])
            ),
        },
        "actions": {
            "plan": {
                "mode": "dry-run",
                "mutation": False,
                "adapter_refs": row["profiles"],
            },
            "run": {
                "mode": "dry-run",
                "mutation": False,
                "requires_preflight": True,
                "adapter_refs": row["profiles"],
            },
            "cleanup": {
                "mode": "dry-run",
                "mutation": False,
                "required": True,
                "order": "reverse",
                "recovery_gate": True,
                "adapter_refs": list(reversed(row["profiles"])),
            },
        },
        "prerequisite_gate": {
            "state": "satisfied" if row["readiness"] == "ready" else "unresolved",
            "required": row["readiness"] != "ready",
            "description": prerequisite or "none",
            "live_allowed": row["id"] in controllers,
        },
        "capture_policy": {
            "policy_ref": "focused-window-v1",
            "time_basis": "UTC",
            "t1": "earliest-actual-effect-start",
            "t2": "latest-load-end-or-fault-release",
            "query_window": "[t1-10m,t2+20m]",
            "export_not_before": "t2+20m",
            "model_snapshot": "/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json@t2+20m",
            "create_golden_anomaly": False,
            "allowed_case_labels": ["calibration", "evaluation", "failed"],
        },
    }


def render(manifest: dict[str, Any]) -> str:
    # JSON is a YAML 1.2 subset. Keeping this representation lets shell entrypoints
    # validate manifests with jq without introducing a YAML parser dependency.
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def load_catalog() -> list[dict[str, Any]]:
    with CATALOG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)["scenarios"]


def load_controllers() -> dict[str, Any]:
    with CONTROLLERS_PATH.open(encoding="utf-8") as handle:
        document = json.load(handle)
    controllers = document.get("controllers")
    if not isinstance(controllers, dict):
        raise SystemExit("controller registry must contain a controllers object")
    live_ids = document.get("live_scenario_ids")
    if (
        not isinstance(live_ids, list)
        or len(live_ids) != len(set(live_ids))
        or set(live_ids) != set(controllers)
    ):
        raise SystemExit("controller registry live_scenario_ids must exactly cover controllers")
    return controllers


def load_execution_matrix() -> dict[str, str]:
    matrix: dict[str, str] = {}
    for line in EXECUTION_MATRIX_PATH.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| F"):
            continue
        columns = [column.strip() for column in line.strip().strip("|").split("|")]
        if len(columns) < 3:
            continue
        scenario_id, location_transport = columns[0], columns[2]
        matrix[scenario_id] = location_transport
    return matrix


def write_manifests(expected: dict[str, str]) -> None:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in expected.items():
        (MANIFEST_DIR / name).write_text(content, encoding="utf-8")
    for path in MANIFEST_DIR.glob("*.yaml"):
        if path.name not in expected:
            path.unlink()


def check_manifests(expected: dict[str, str]) -> int:
    actual_names = {path.name for path in MANIFEST_DIR.glob("*.yaml")}
    expected_names = set(expected)
    errors: list[str] = []
    for missing in sorted(expected_names - actual_names):
        errors.append(f"missing manifest: {missing}")
    for extra in sorted(actual_names - expected_names):
        errors.append(f"unexpected manifest: {extra}")
    for name in sorted(actual_names & expected_names):
        if (MANIFEST_DIR / name).read_text(encoding="utf-8") != expected[name]:
            errors.append(f"manifest drift: {name}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"[PASS] {len(expected)} deterministic scenario manifests")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()

    scenarios = load_catalog()
    controllers = load_controllers()
    matrix = load_execution_matrix()
    catalog_ids = {row["id"] for row in scenarios}
    if catalog_ids != set(matrix):
        missing = sorted(catalog_ids - set(matrix))
        extra = sorted(set(matrix) - catalog_ids)
        raise SystemExit(f"catalog/execution-matrix ID mismatch: missing={missing}, extra={extra}")
    expected = {
        f"{row['slug']}.yaml": render(build_manifest(row, matrix[row["id"]], controllers))
        for row in scenarios
    }
    if args.write:
        write_manifests(expected)
        print(f"[WRITE] {len(expected)} scenario manifests")
        return 0
    return check_manifests(expected)


if __name__ == "__main__":
    raise SystemExit(main())
