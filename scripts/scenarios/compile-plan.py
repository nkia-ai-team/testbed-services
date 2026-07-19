#!/usr/bin/env python3
"""Compile the static scenario catalog into a trusted, side-effect-free plan."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "registry"


class ContractError(ValueError):
    pass


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ContractError(f"registry must be an object: {path}")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def load_contracts() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    catalog = _load(ROOT / "catalog.json")
    locations = _load(REGISTRY / "locations.json")
    profiles = _load(REGISTRY / "profiles.json")
    queries = _load(REGISTRY / "queries.json")
    controllers = _load(REGISTRY / "controllers.json")
    validate_contracts(catalog, locations, profiles, queries, controllers)
    return catalog, locations, profiles, queries, controllers


def validate_contracts(
    catalog: dict[str, Any],
    locations_doc: dict[str, Any],
    profiles_doc: dict[str, Any],
    queries_doc: dict[str, Any],
    controllers_doc: dict[str, Any],
) -> None:
    locations = locations_doc.get("locations", {})
    aliases = locations_doc.get("catalog_aliases", {})
    profiles = profiles_doc.get("profiles", {})
    adapters = queries_doc.get("adapters", {})
    queries = queries_doc.get("queries", {})
    controllers = controllers_doc.get("controllers", {})
    required_adapters = {
        "loadgen_summary", "http_probe", "prometheus", "kubernetes",
        "database", "business_probe", "capture_status",
    }
    if set(adapters) != required_adapters:
        raise ContractError("adapter registry must contain exactly the seven approved adapters")
    for location_id, location in locations.items():
        if location.get("transport") == "kubectl" and location.get("kubeconfig") != "/root/tb-kubeconfig":
            raise ContractError(f"non-canonical kubeconfig at location {location_id}")
    for query_id, query in queries.items():
        if query.get("adapter") not in adapters:
            raise ContractError(f"query {query_id} references unknown adapter")
        if "promql" in query:
            raise ContractError(f"query {query_id} contains forbidden raw promql")
        if query.get("adapter") == "prometheus" and not query.get("template_id"):
            raise ContractError(f"prometheus query {query_id} requires template_id")
    for profile_id, profile in profiles.items():
        executor = ROOT / profile["executor"]
        if not executor.is_file():
            raise ContractError(f"profile {profile_id} executor missing: {executor}")
        for location_id in profile.get("allowed_locations", []):
            if location_id not in locations:
                raise ContractError(f"profile {profile_id} references unknown location {location_id}")
        for query_id in profile.get("default_queries", []):
            if query_id not in queries:
                raise ContractError(f"profile {profile_id} references unknown query {query_id}")
        if profile.get("live_supported"):
            allowed = profile.get("parameter_contract", {}).get("allowed_scenarios", [])
            parameters = profile.get("scenario_parameters", {})
            if not allowed or set(allowed) != set(parameters):
                raise ContractError(
                    f"live profile {profile_id} requires exact allowlisted scenario parameters"
                )
        for scenario_id, levels in profile.get("scenario_levels", {}).items():
            if scenario_id not in profile.get("scenario_parameters", {}):
                raise ContractError(f"profile {profile_id} levels reference unknown scenario {scenario_id}")
            if not isinstance(levels, list) or not levels:
                raise ContractError(f"profile {profile_id} scenario {scenario_id} requires levels")
            level_ids = [level.get("level_id") for level in levels]
            if len(level_ids) != len(set(level_ids)) or not all(
                isinstance(level_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]+", level_id)
                for level_id in level_ids
            ):
                raise ContractError(f"profile {profile_id} scenario {scenario_id} has invalid level ids")
            level_parameters = [level.get("parameters") for level in levels]
            if profile["scenario_parameters"][scenario_id] not in level_parameters:
                raise ContractError(
                    f"profile {profile_id} scenario {scenario_id} selected parameters are not a declared level"
                )
    for scenario in catalog.get("scenarios", []):
        alias = scenario.get("injection_location")
        if alias not in aliases:
            raise ContractError(f"scenario {scenario.get('id')} has unknown location alias {alias}")
        for profile_id in scenario.get("profiles", []):
            if profile_id not in profiles:
                raise ContractError(f"scenario {scenario.get('id')} references unknown profile {profile_id}")
    validate_controllers(catalog, profiles_doc, queries_doc, controllers_doc)


def validate_controllers(
    catalog_doc: dict[str, Any],
    profiles_doc: dict[str, Any],
    queries_doc: dict[str, Any],
    controllers_doc: dict[str, Any],
) -> None:
    scenarios = {item["id"]: item for item in catalog_doc.get("scenarios", [])}
    profiles = profiles_doc.get("profiles", {})
    queries = queries_doc.get("queries", {})
    controllers = controllers_doc.get("controllers", {})
    live_ids = controllers_doc.get("live_scenario_ids")
    if (
        not isinstance(live_ids, list)
        or len(live_ids) != len(set(live_ids))
        or set(live_ids) != set(controllers)
    ):
        raise ContractError("controller live_scenario_ids must exactly cover controllers")
    for scenario_id, controller in controllers.items():
        scenario = scenarios.get(scenario_id)
        if scenario is None or scenario.get("readiness") != "ready":
            raise ContractError(f"controller {scenario_id} must bind a ready catalog scenario")
        if controller.get("dispatcher_mode") != "trusted" or controller.get("live_enabled") is not True:
            raise ContractError(f"controller {scenario_id} must use trusted live dispatch")
        if controller.get("tick_interval") != "15s":
            raise ContractError(f"controller {scenario_id} requires bounded 15s ticks")
        if controller.get("settle_after_change") != "30s":
            raise ContractError(f"controller {scenario_id} requires bounded 30s settling")
        baseline = controller.get("baseline", {})
        required_checks = {item.get("check") for item in baseline.get("required", []) if isinstance(item, dict)}
        if baseline.get("clean_window") != "2h" or not {
            "coordinator-clean", "clean-window", "baseline-traffic", "target-health"
        } <= required_checks:
            raise ContractError(f"controller {scenario_id} baseline is not fail-closed")
        profile = controller.get("profile", {})
        primary = profile.get("primary_ref")
        companions = profile.get("companion_refs")
        if primary != scenario["profiles"][0] or companions != scenario["profiles"][1:]:
            raise ContractError(f"controller {scenario_id} profile binding/order mismatch")
        if primary not in profiles:
            raise ContractError(f"controller {scenario_id} references unknown primary profile")
        levels = profile.get("levels")
        if not isinstance(levels, list) or not levels:
            raise ContractError(f"controller {scenario_id} requires controller levels")
        mode = controller.get("mode")
        if mode == "evaluation":
            if profile.get("kind") != "fixed" or len(levels) != 1:
                raise ContractError(f"controller {scenario_id} evaluation requires one fixed level")
            approved_profile_id = profile.get("approved_profile_id")
            if approved_profile_id != primary:
                raise ContractError(f"controller {scenario_id} evaluation profile is not approved")
            expected_parameters = profiles[primary].get("scenario_parameters", {}).get(scenario_id)
            if levels[0].get("parameters") != expected_parameters:
                raise ContractError(f"controller {scenario_id} fixed parameters are not approved")
        elif mode == "calibration":
            if profile.get("kind") != "adaptive_ladder" or profile.get("approved_profile_id") is not None:
                raise ContractError(f"controller {scenario_id} calibration binding is invalid")
            approved_levels = profiles[primary].get("scenario_levels", {}).get(scenario_id, [])
            actual_levels = [
                {"level_id": level.get("id"), "parameters": level.get("parameters")}
                for level in levels
            ]
            expected_levels = [
                {"level_id": level.get("level_id"), "parameters": level.get("parameters")}
                for level in approved_levels
            ]
            if actual_levels != expected_levels:
                raise ContractError(f"controller {scenario_id} ladder differs from approved levels")
        else:
            raise ContractError(f"controller {scenario_id} uses unsupported mode")
        observations = controller.get("observations", [])
        validate_observation_refs(observations, queries_doc)
        observation_ids = [item.get("id") for item in observations]
        if len(observation_ids) != len(set(observation_ids)) or not all(observation_ids):
            raise ContractError(f"controller {scenario_id} observation ids must be unique")
        for observation in observations:
            query = queries[observation["query_id"]]
            if observation.get("adapter") != query.get("adapter"):
                raise ContractError(f"controller {scenario_id} adapter/query mismatch")
        available = set(observation_ids)
        for condition_name in ("success", "escalate", "abort", "must_rule_out", "recovery"):
            condition_set = controller.get(condition_name)
            if condition_name == "escalate" and mode == "evaluation":
                if condition_set is not None:
                    raise ContractError(f"controller {scenario_id} fixed evaluation cannot escalate")
                continue
            if not isinstance(condition_set, dict):
                raise ContractError(f"controller {scenario_id} requires {condition_name}")
            match_keys = [key for key in ("all", "any") if key in condition_set]
            if len(match_keys) != 1 or not condition_set[match_keys[0]]:
                raise ContractError(f"controller {scenario_id} invalid {condition_name} conditions")
            refs = {item.get("observation") for item in condition_set[match_keys[0]]}
            if not refs <= available:
                raise ContractError(f"controller {scenario_id} has unbound {condition_name} signals")
        abort = controller["abort"]
        abort_items = abort.get("any", [])
        abort_pairs = {(item.get("observation"), item.get("op"), item.get("value")) for item in abort_items}
        if ("entry_status", "eq", 0) not in abort_pairs or ("pod_ready", "eq", False) not in abort_pairs:
            raise ContractError(f"controller {scenario_id} lacks catastrophic abort gates")
        recovery_items = controller["recovery"].get("all", [])
        recovery_pairs = {(item.get("observation"), item.get("op"), item.get("value")) for item in recovery_items}
        if ("target_health", "eq", 200) not in recovery_pairs or ("pod_ready", "eq", True) not in recovery_pairs:
            raise ContractError(f"controller {scenario_id} lacks health/pod recovery")
        capture = controller.get("capture", {})
        if capture != {
            "enabled": True,
            "pre_window": "2h",
            "post_window": "45m",
            "model_snapshot": "/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json",
            "create_golden_anomaly": False,
        }:
            raise ContractError(f"controller {scenario_id} capture policy mismatch")


def validate_observation_refs(observations: list[dict[str, Any]], queries_doc: dict[str, Any]) -> None:
    queries = queries_doc["queries"]
    for observation in observations:
        if "promql" in observation or "query" in observation:
            raise ContractError("raw PromQL/query text is forbidden; reference approved query_id")
        query_id = observation.get("query_id")
        if query_id not in queries:
            raise ContractError(f"unknown approved query_id: {query_id}")


def validate_profile_location(profile_id: str, location_id: str, profiles_doc: dict[str, Any]) -> None:
    profile = profiles_doc.get("profiles", {}).get(profile_id)
    if profile is None:
        raise ContractError(f"unknown profile: {profile_id}")
    if location_id not in profile.get("allowed_locations", []):
        raise ContractError(f"profile {profile_id} cannot use location {location_id}")


def validate_manifest(
    scenario: dict[str, Any],
    manifest: dict[str, Any],
    controllers_doc: dict[str, Any],
) -> None:
    expected_profile_refs = [f"injector-profiles/{profile_id}" for profile_id in scenario["profiles"]]
    expected_gate_state = "satisfied" if scenario["readiness"] == "ready" else "unresolved"
    expected_prerequisite = scenario["prerequisite"].strip() or "none"
    identity_matches = (
        manifest.get("id") == scenario["id"]
        and manifest.get("slug") == scenario["slug"]
        and manifest.get("readiness") == scenario["readiness"]
    )
    injection = manifest.get("injection", {})
    actions = manifest.get("actions", {})
    controller = manifest.get("execution", {}).get("controller", {})
    gate = manifest.get("prerequisite_gate", {})
    if not identity_matches:
        raise ContractError(f"manifest/catalog identity mismatch: {scenario['slug']}")
    if (
        injection.get("location") != scenario["injection_location"]
        or injection.get("profile_refs") != expected_profile_refs
    ):
        raise ContractError(f"manifest/catalog injection mismatch: {scenario['slug']}")
    if (
        actions.get("plan", {}).get("adapter_refs") != scenario["profiles"]
        or actions.get("run", {}).get("adapter_refs") != scenario["profiles"]
        or actions.get("cleanup", {}).get("adapter_refs") != list(reversed(scenario["profiles"]))
    ):
        raise ContractError(f"manifest/catalog action mismatch: {scenario['slug']}")
    registered = controllers_doc.get("controllers", {}).get(scenario["id"])
    if registered is None:
        if (
            controller.get("profile", {}).get("kind") != scenario["load_mode"]
            or controller.get("live_enabled") is not False
        ):
            raise ContractError(f"manifest/catalog controller mismatch: {scenario['slug']}")
    else:
        expected_runtime = json.loads(json.dumps(registered))
        expected_runtime.pop("dispatcher_mode")
        expected_runtime.pop("live_enabled")
        expected_profile = expected_runtime["profile"]
        expected_binding = {
            "primary_ref": expected_profile.pop("primary_ref"),
            "companion_refs": expected_profile.pop("companion_refs"),
        }
        approved_profile_id = expected_profile.pop("approved_profile_id")
        if approved_profile_id is not None:
            expected_binding["approved_profile_id"] = approved_profile_id
        if controller != {
            "dispatcher_mode": "trusted",
            "live_enabled": True,
            "binding": expected_binding,
            "runtime": expected_runtime,
        }:
            raise ContractError(f"manifest/controller registry mismatch: {scenario['slug']}")
    if (
        gate.get("state") != expected_gate_state
        or gate.get("required") != (scenario["readiness"] != "ready")
        or gate.get("description") != expected_prerequisite
        or gate.get("live_allowed") != (registered is not None)
    ):
        raise ContractError(f"manifest/catalog prerequisite mismatch: {scenario['slug']}")


def compile_plan(slug: str) -> dict[str, Any]:
    catalog, locations_doc, profiles_doc, queries_doc, controllers_doc = load_contracts()
    scenario = next((row for row in catalog["scenarios"] if row["slug"] == slug), None)
    if scenario is None:
        raise ContractError(f"unknown scenario slug: {slug}")
    manifest = _load(ROOT / "manifests" / f"{slug}.yaml")
    validate_manifest(scenario, manifest, controllers_doc)
    locations = locations_doc["locations"]
    aliases = locations_doc["catalog_aliases"]
    profiles = profiles_doc["profiles"]
    controller = manifest["execution"]["controller"]
    controller_binding = controller.get("binding", {})
    controller_runtime = controller.get("runtime", {})
    controller_primary = controller_binding.get("primary_ref")
    controller_levels = controller_runtime.get("profile", {}).get("levels", [])
    scenario_location = aliases[scenario["injection_location"]]
    instances: list[dict[str, Any]] = []
    query_ids: set[str] = set()
    for sequence, profile_id in enumerate(scenario["profiles"]):
        profile = profiles[profile_id]
        location_id = profile.get("default_location") if profile["location_strategy"] == "fixed" else scenario_location
        validate_profile_location(profile_id, location_id, profiles_doc)
        executor = ROOT / profile["executor"]
        query_ids.update(profile.get("default_queries", []))
        parameters = profile.get("scenario_parameters", {}).get(scenario["id"], {})
        levels = profile.get("scenario_levels", {}).get(scenario["id"], [])
        approved_levels = levels
        if profile_id == controller_primary:
            approved_levels = [
                {"level_id": level["id"], "parameters": level["parameters"]}
                for level in controller_levels
            ]
        selected_level_id = next(
            (
                level["level_id"]
                for level in approved_levels
                if level["parameters"] == parameters
            ),
            None,
        )
        allowed_scenarios = profile.get("parameter_contract", {}).get("allowed_scenarios", [])
        instance_live_supported = bool(
            profile.get("live_supported", False)
            and scenario["id"] in allowed_scenarios
            and parameters
        )
        instances.append({
            "sequence": sequence,
            "profile_id": profile_id,
            "executor": profile["executor"],
            "executor_sha256": hashlib.sha256(executor.read_bytes()).hexdigest(),
            "location_id": location_id,
            "location": locations[location_id],
            "live_supported": instance_live_supported,
            "parameters": parameters,
            "scenario_levels": levels,
            "selected_level_id": selected_level_id,
            "approved_levels": approved_levels,
        })
    if controller.get("live_enabled") is True:
        query_ids.update(
            observation["query_id"] for observation in controller["runtime"]["observations"]
        )
    controller_complete = bool(
        controller.get("live_enabled") is True
        and controller.get("dispatcher_mode") == "trusted"
        and isinstance(controller.get("binding"), dict)
        and isinstance(controller.get("runtime"), dict)
    )
    plan = {
        "schema_version": "1.0",
        "side_effects": False,
        "scenario": scenario,
        "profile_instances": instances,
        "observation_query_ids": sorted(query_ids),
        "cleanup_order": [item["profile_id"] for item in reversed(instances)],
        "live_allowed": scenario["readiness"] == "ready" and controller_complete and all(
            item["live_supported"] and item["location"].get("resolved") for item in instances
        ),
        "scenario_digest": _digest(scenario),
        "registry_digest": _digest({
            "locations": locations_doc,
            "profiles": profiles_doc,
            "queries": queries_doc,
            "controllers": controllers_doc,
        }),
        "manifest_digest": _digest(manifest),
        "controller": controller,
        "capture": {
            "query_window": "[t1-2h,t2+45m]",
            "model_snapshot_at": "t2+45m",
            "create_golden_anomaly": False,
        },
    }
    plan["plan_digest"] = _digest(plan)
    return plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    args = parser.parse_args()
    print(json.dumps(compile_plan(args.scenario), sort_keys=True, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
