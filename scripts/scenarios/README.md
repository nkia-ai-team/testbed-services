# 64개 시나리오 실행 진입점

이 디렉터리는 64개의 얇은 실행 스크립트, 64개의 개별 YAML manifest와 하나의
공통 안전 실행기를 제공한다. 개별 파일은 로직을 복제하지 않고 `catalog.json`과
동일 slug의 `manifests/*.yaml`을 선택한다. manifest는 JSON-compatible YAML이라
별도 YAML parser 없이 `jq`로도 검증할 수 있다.

```bash
scripts/scenarios/bin/f07-h-north-south-surge.sh
```

기본 동작은 부작용 없는 plan dry-run이다. `--run`과 `--cleanup`도 현재는 각각의
정규화 계획만 출력한다. 세 action 모두 `compile-plan.py`가 만든 동일한 normalized
plan, scenario/manifest/registry/plan digest, profile executor hash, approved query ID,
location ID를 포함하고 `selected_action`만 action에 맞게 선택한다. live는 trusted
dispatcher가 연결되기 전까지 readiness와 무관하게 모든 항목에서 fail-closed로
거부한다.

```bash
scripts/scenarios/bin/f07-h-north-south-surge.sh --run
scripts/scenarios/bin/f07-h-north-south-surge.sh --cleanup
```

64개 파일은 공통 injector family를 조합하는 진입점이다. 실제 DB, Kubernetes,
MockServer, host, network 명령은 검증된 runner가 소유하며 이 저장소의 wrapper에
복제하지 않는다.

manifest는 catalog와 실행 위치 결정표에서 파생하며 아래 명령으로 드리프트를
검사한다. catalog를 의도적으로 수정한 경우에만 `--write`로 재생성한다.

```bash
python3 scripts/scenarios/generate-manifests.py --check
python3 scripts/scenarios/generate-manifests.py --write
python3 scripts/scenarios/compile-plan.py --scenario f07-h-north-south-surge
scripts/scenarios/test-scenarios.sh
```

compiler는 catalog, manifest, location/profile/query registry의 참조 폐쇄성을 함께
검증한다. kubectl location은 `/root/tb-kubeconfig`만 허용하고, profile executor는
실제 파일과 SHA-256으로 plan에 결박한다. `readiness: ready`는 설계 준비도이며
`live_allowed`와 동일한 값으로 취급하지 않는다.
