"""Pure helpers and fixed query contracts for compensation/correctness observations."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

QUERY_CANDIDATES = {
    "business.inventory_balance_restored_since_t1": {
        "adapter": "database", "selector": "commerce.inventory.balance_restored_since_t1",
        "allowed_parameters": ["scenario_id", "run_id", "t1"], "value_type": "boolean",
    },
    "business.order_terminal_consistency_since_t1": {
        "adapter": "database", "selector": "commerce.order.terminal_consistency_since_t1",
        "allowed_parameters": ["scenario_id", "run_id", "t1"], "value_type": "boolean",
    },
    "business.food_payment_status_mix_since_t1": {
        "adapter": "database", "selector": "food.payment.status_mix_since_t1",
        "allowed_parameters": ["scenario_id", "run_id", "t1"], "value_type": "object",
    },
}


def pricing_delta(canonical: Any, quoted: Any) -> Decimal:
    return abs(Decimal(str(canonical)) - Decimal(str(quoted)))


def inventory_restored(before: dict[int, int], after: dict[int, int]) -> bool:
    return before == after


def order_terminal_consistent(statuses: list[str]) -> bool:
    terminal = {"COMPLETED", "FAILED", "CANCELLED"}
    return bool(statuses) and all(status in terminal for status in statuses)


def food_status_mix(counts: dict[str, int]) -> bool:
    return counts.get("APPROVED", 0) > 0 and counts.get("FAILED", 0) > 0
