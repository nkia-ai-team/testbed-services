"""banking 아웃박스 릴레이는 소유자가 하나여야 한다.

transfer·ledger 가 같은 BANKING.outbox_events 를 공유하는데(둘 다 default_schema
미설정) OutboxRelay 의 findPending 이 무스코프라, 릴레이를 둘 이상 켜면 서로의
행을 무차별로 집어간다.

24h 트레이스 전수(2026-08-07): transfer 가 banking.ledger 로 13,154건,
ledger 가 banking.transfers 로 13,696건 — 어느 릴레이에 집히는지가 동전 던지기였다.
그래서 F18-P(transfer 릴레이 정지)가 5회 연속 실패했다. 주입은 정확히 작동했지만
ledger 릴레이가 빈자리를 흡수해 미발행이 쌓이지 않았다.

이 테스트는 결함 자체가 아니라 **재발**을 막는다. 무스코프 findPending 은 그대로
남아 있으므로, 세 번째 relay-enabled banking 서비스가 생기거나 ledger 를 되켜면
조용히 재발하고 재발해도 오류가 나지 않는다 — 5회를 놓친 그대로다.

근본 수리(서비스별 스키마 / 서비스별 테이블 / aggregate_type 스코프)가 들어가면
이 테스트는 그 수리에 맞게 바뀌어야 한다.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BANKING = REPO / "core-banking"

# outbox.relay.enabled 를 선언하는 서비스만 대상. 선언이 없으면 빈이 안 뜬다.
_ENABLED = re.compile(
    r"^outbox:\s*$.*?^\s{2}relay:\s*$.*?^\s{4}enabled:\s*(true|false)\s*$",
    re.MULTILINE | re.DOTALL,
)


def _relay_enabled_services() -> dict[str, bool]:
    found: dict[str, bool] = {}
    for yml in sorted(BANKING.glob("*-service/src/main/resources/application.yml")):
        text = yml.read_text(encoding="utf-8")
        match = _ENABLED.search(text)
        if match:
            found[yml.parents[3].name] = match.group(1) == "true"
    return found


def test_banking_has_at_most_one_relay_owner() -> None:
    services = _relay_enabled_services()
    assert services, "core-banking 에서 outbox.relay.enabled 선언을 하나도 못 찾았다 — 탐색 경로를 확인하라"

    owners = sorted(name for name, enabled in services.items() if enabled)
    assert len(owners) <= 1, (
        "banking 아웃박스 릴레이 소유자가 둘 이상이다: "
        f"{owners}. 공유 BANKING.outbox_events 를 무스코프 findPending 으로 "
        "경쟁 소비하게 되어 F18-P 계열이 다시 조용히 실패한다. "
        "근본 수리(스키마/테이블 분리 또는 aggregate_type 스코프) 없이는 하나만 켤 것."
    )


def test_transfer_is_the_relay_owner() -> None:
    """정지 스위치(control-id)를 가진 서비스가 소유자여야 한다.

    F18-P 는 transfer 릴레이를 app.control 로 세우는 시나리오다. 소유자가 transfer 가
    아니면 그 정지 스위치가 아무것도 멈추지 못한다.
    """
    services = _relay_enabled_services()
    owners = [name for name, enabled in services.items() if enabled]
    if not owners:
        return  # 전부 꺼진 상태는 이 테스트의 관심 밖(위 테스트가 커버)
    assert owners == ["transfer-service"], (
        f"릴레이 소유자가 transfer-service 가 아니다: {owners}. "
        "control-id 를 가진 서비스는 transfer-service 뿐이므로 "
        "F18-P 의 정지 스위치가 무력해진다."
    )
