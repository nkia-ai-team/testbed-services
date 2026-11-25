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
    # host_probe joined on 2026-07-28 for the storage-saturation scenarios: KCM
    # publishes node CPU and memory but nothing per-device, so disk busy time is
    # read off the host itself. Without it those scenarios can only show that a
    # database got slow, which is the one thing every other DB fault also shows.
    # clickhouse joined on 2026-07-30: service error rate is read from the trace
    # table because the APM rollup dropped 20-288x of the denominator, leaving
    # every percentage threshold to be decided by a single sampled request.
    required_adapters = {
        "loadgen_summary", "http_probe", "prometheus", "kubernetes",
        "database", "business_probe", "capture_status", "host_probe",
        "clickhouse",
    }
    if set(adapters) != required_adapters:
        raise ContractError(
            "adapter registry must contain exactly the approved adapters: "
            f"missing {sorted(required_adapters - set(adapters))}, "
            f"unexpected {sorted(set(adapters) - required_adapters)}"
        )
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
    validate_controllers(catalog, profiles_doc, queries_doc, controllers_doc, locations_doc)


# VM testbeds (docs/spec-testbed-vm-3tier.md, spec-testbed-sms-only.md): no Kubernetes, so no pod to be Ready again.
VM_DOMAINS = frozenset({"civil-petition", "civil-petition-sms"})


def scenario_location_domain(scenario: dict[str, Any], locations_doc: dict[str, Any] | None) -> str | None:
    """The domain of the scenario's injection location (catalog alias -> location), None when unknown."""
    if not locations_doc:
        return None
    location_id = locations_doc.get("catalog_aliases", {}).get(scenario.get("injection_location"))
    return locations_doc.get("locations", {}).get(location_id, {}).get("domain")


def validate_controllers(
    catalog_doc: dict[str, Any],
    profiles_doc: dict[str, Any],
    queries_doc: dict[str, Any],
    controllers_doc: dict[str, Any],
    locations_doc: dict[str, Any] | None = None,
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
        if baseline.get("clean_window") != "30m" or not {
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
        # catastrophic abort는 사용자 영향 단일 기준이다(2026-07-20 게이트 재정의):
        # entry_status==0(진입 전멸)이 필수, available_replicas==0(서빙 전멸)은 선택.
        # pod_ready는 abort에 넣을 수 없다 — "재난인가"와 "이 pod이 트래픽을 받나"의
        # 의미 이중성 때문에 시나리오의 의도된 rollout·eviction·의존성 전파가 자기
        # abort를 트리거한 계보가 5건(F08-H·F05-G·F09-P·F11-G·F11-R)이다.
        if any(obs == "pod_ready" for obs, _op, _val in abort_pairs):
            raise ContractError(f"controller {scenario_id} uses pod_ready as abort gate (forbidden)")
        if ("entry_status", "eq", 0) not in abort_pairs:
            raise ContractError(f"controller {scenario_id} lacks catastrophic abort gates")
        recovery_items = controller["recovery"].get("all", [])
        recovery_pairs = {(item.get("observation"), item.get("op"), item.get("value")) for item in recovery_items}
        domain = scenario_location_domain(scenario, locations_doc)
        if domain in VM_DOMAINS:
            # A VM testbed has no pod to be Ready again (2026-11-25, F72-P the first one). Its serving path is back when
            # the domain's own entry answers 200 again: the resident loadgen's last business request (entry_status of
            # the domain's baseline document), the same observation the catastrophic abort above reads. Kubernetes
            # observations would read another testbed.
            entry = next((item for item in observations if item.get("id") == "entry_status"), {})
            if (
                ("target_health", "eq", 200) not in recovery_pairs
                or ("entry_status", "eq", 200) not in recovery_pairs
                or entry.get("query_id") != "http.entry_health"
                or (entry.get("parameters") or {}).get("domain") != domain
            ):
                raise ContractError(f"controller {scenario_id} lacks health/entry recovery for VM domain {domain}")
            if any(item.get("adapter") == "kubernetes" for item in observations):
                raise ContractError(f"controller {scenario_id} reads kubernetes on VM domain {domain}")
        elif ("target_health", "eq", 200) not in recovery_pairs or ("pod_ready", "eq", True) not in recovery_pairs:
            raise ContractError(f"controller {scenario_id} lacks health/pod recovery")
        for found in recovery_own_load_reads(scenario_id, controller, queries_doc):
            raise ContractError(
                f"controller {scenario_id} recovery reads the scenario's own load output ({found})"
            )
        capture = controller.get("capture", {})
        if capture != {
            "enabled": True,
            "pre_window": "10m",
            "post_window": "20m",
            "model_snapshot": "/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json",
            "create_golden_anomaly": False,
        }:
            raise ContractError(f"controller {scenario_id} capture policy mismatch")


def recovery_own_load_reads(
    scenario_id: str, controller: dict[str, Any], queries_doc: dict[str, Any]
) -> list[str]:
    """recovery 조건 가운데 시나리오 자기 k6 출력을 읽는 것을 "<id>:recovery:<조건 id>" 로 돌려준다.

    domain 을 받는 질의(loadgen_summary 전부, http.entry_health, business.checkout_invariant)는
    domain 없이 쓰면 tb-runner 의 /tmp/rca-scenario-<id>-live.json 을 읽는다. 그 파일은
    load.north_south 가 도는 동안만 있고, 그 cleanup 이 k6 를 끝내며 지운다. recovery 는 cleanup
    이 모두 성공한 뒤에만 평가되므로 그 신호는 recovery 내내 unusable 이고, all 조건은 참이 될 수
    없어 recovery_timeout, dirty, 큐 정지로 끝난다(08-03 9dfeefb 가 9종에서 고친 것, 11-18 F69-P
    재발 뒤 같은 꼴로 고침). 회복은 상주 기준선(같은 질의에 domain)으로 읽는다. 예외는 두지 않는다.
    """
    queries = queries_doc.get("queries", {})
    observation_by_id = {item.get("id"): item for item in controller.get("observations", [])}
    recovery = controller.get("recovery") or {}
    found: list[str] = []
    for match_key in ("all", "any"):
        for item in recovery.get(match_key, []):
            observation = observation_by_id.get(item.get("observation")) or {}
            query = queries.get(observation.get("query_id"), {})
            if "domain" in query.get("allowed_parameters", []) and "domain" not in (
                observation.get("parameters") or {}
            ):
                found.append(f"{scenario_id}:recovery:{item.get('id')}")
    return found


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
            "query_window": "[t1-10m,t2+20m]",
            "model_snapshot_at": "t2+20m",
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
