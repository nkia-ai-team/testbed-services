#!/usr/bin/env python3
"""녹화 전 이미지 점검: 테스트베드 앱이 «자기» 매니페스트 이미지로 도는가.

결함 버전 이미지 시나리오는 장애 동안 앱 이미지를 바꾸고 cleanup 에서 되돌린다. 되돌리기가
실패한 채 다음 녹화를 시작하면 그 녹화 전체에 앞 시나리오의 결함이 깔린다.

예전 점검(운영 스킬의 image-drift-guard.sh)은 라이브 이미지가 매니페스트 이미지 목록
«어딘가»에 있는지만 봤다. 그래서 다른 서비스의 이미지로 덮어쓰는 잔류(transfer 컨테이너에
core-banking-ledger:latest)를 통과시켰다(2026-10-24 F77-R, 장부 §6). 여기서는 Deployment 와
StatefulSet 마다, 컨테이너마다 자기 매니페스트의 이미지와 비교한다.

무해한 배포(v4 미끼, 평시 배포)는 이미지를 바꾸지 않는다(재시작 표식, ConfigMap 값,
annotation 만). 그래서 이 점검을 그대로 통과하고, 이미지가 바뀐 진짜 잔류만 걸린다.

사용:
  kubectl get deploy,sts -A -o json | image-drift-check.py --manifest-root <저장소 루트>
종료 코드 0 = 통과, 1 = 드리프트(목록 출력), 2 = 점검 실패(입력을 읽을 수 없음).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DOMAINS = ("commerce", "core-banking", "food-delivery")
NAMESPACE_PREFIX = "rca-testbed-"
WORKLOAD_KINDS = ("Deployment", "StatefulSet")

_COMMENT = re.compile(r"\s+#.*$")


def _strip(line: str) -> str:
    return _COMMENT.sub("", line).rstrip() if not line.lstrip().startswith("#") else ""


def parse_manifests(texts: list[str]) -> dict[tuple[str, str, str], dict[str, str]]:
    """(kind, namespace, name) -> {container name: image} from manifest YAML.

    The manifests are plain, so a line scan is enough and needs no YAML library
    (the operate host does not guarantee one). Container and image come in
    pairs: each ``image:`` belongs to the nearest ``- name:`` above it.
    """
    expected: dict[tuple[str, str, str], dict[str, str]] = {}
    for text in texts:
        for document in re.split(r"(?m)^---\s*$", text):
            lines = [_strip(line) for line in document.splitlines()]
            kind = next((m.group(1) for line in lines if (m := re.match(r"^kind:\s*(\S+)", line))), None)
            if kind not in WORKLOAD_KINDS:
                continue
            name = next((m.group(1) for line in lines if (m := re.match(r"^  name:\s*(\S+)", line))), None)
            namespace = next(
                (m.group(1) for line in lines if (m := re.match(r"^  namespace:\s*(\S+)", line))), None
            )
            if not name or not namespace:
                continue
            containers: dict[str, str] = {}
            last_name = None
            for line in lines:
                named = re.match(r"^\s*-\s+name:\s*(\S+)", line)
                if named:
                    last_name = named.group(1)
                    continue
                image = re.match(r"^\s*-?\s*image:\s*(\S+)", line)
                if image and last_name is not None:
                    containers[last_name] = image.group(1).strip("\"'")
                    last_name = None
            expected[(kind, namespace, name)] = containers
    return expected


def load_manifests(root: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    texts = [
        path.read_text(encoding="utf-8")
        for domain in DOMAINS
        for path in sorted((root / domain / "k8s").glob("*.yaml"))
    ]
    return parse_manifests(texts)


def live_workloads(document: dict) -> dict[tuple[str, str, str], dict[str, str]]:
    live: dict[tuple[str, str, str], dict[str, str]] = {}
    for item in document.get("items", []):
        kind = item.get("kind")
        metadata = item.get("metadata") or {}
        namespace = str(metadata.get("namespace", ""))
        if kind not in WORKLOAD_KINDS or not namespace.startswith(NAMESPACE_PREFIX):
            continue
        pod_spec = ((item.get("spec") or {}).get("template") or {}).get("spec") or {}
        containers = {
            str(container.get("name")): str(container.get("image"))
            for key in ("initContainers", "containers")
            for container in pod_spec.get(key) or []
        }
        live[(kind, namespace, str(metadata.get("name")))] = containers
    return live


def find_drift(
    expected: dict[tuple[str, str, str], dict[str, str]],
    live: dict[tuple[str, str, str], dict[str, str]],
) -> list[str]:
    known_images = {image for containers in expected.values() for image in containers.values()}
    problems: list[str] = []
    for key in sorted(live):
        kind, namespace, name = key
        containers = live[key]
        want = expected.get(key)
        if want is None:
            # Not in the manifests (left behind by an injection, or added by
            # hand): keep the former rule so nothing that passed is lost.
            for container, image in sorted(containers.items()):
                if image not in known_images:
                    problems.append(f"- {namespace}/{name} {container}: {image} (매니페스트에 없는 객체와 이미지)")
            continue
        for container, image in sorted(containers.items()):
            wanted = want.get(container)
            if wanted is None:
                if image not in known_images:
                    problems.append(f"- {namespace}/{name} {container}: {image} (매니페스트에 없는 컨테이너)")
                continue
            if image != wanted:
                problems.append(f"- {namespace}/{name} {container}: {image} (기대 {wanted})")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        expected = load_manifests(args.manifest_root)
        live = live_workloads(json.load(sys.stdin))
    except (OSError, ValueError) as error:
        print(f"점검 실패: {error}")
        return 2
    if not expected:
        print("점검 실패: 매니페스트 워크로드 목록이 비었다")
        return 2
    if not live:
        print("점검 실패: 라이브 테스트베드 워크로드가 없다")
        return 2
    problems = find_drift(expected, live)
    if problems:
        print("\n".join(problems))
        print("위 컨테이너가 자기 매니페스트와 다른 이미지로 돈다. 앞 시나리오의 cleanup 이 이미지를 되돌리지 못했을 수 있다")
        return 1
    print("통과: 테스트베드 앱 이미지가 모두 자기 매니페스트와 같다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
