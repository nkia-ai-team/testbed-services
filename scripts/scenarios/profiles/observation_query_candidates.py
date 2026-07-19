"""Fixed, read-only query contracts awaiting runner registry integration."""
from __future__ import annotations

from business_invariant_observer import QUERY_CANDIDATES as BUSINESS_QUERY_CANDIDATES

QUERY_CANDIDATES = {
    **BUSINESS_QUERY_CANDIDATES,
    "prometheus.apm_service_request_rate": {"adapter": "prometheus", "selector": "apm.service.request_rate", "allowed_parameters": ["service", "window"], "value_type": "number"},
    "prometheus.apm_service_error_rate": {"adapter": "prometheus", "selector": "apm.service.error_rate", "allowed_parameters": ["service", "window"], "value_type": "number"},
    "kubernetes.job_active": {"adapter": "kubernetes", "selector": "job.status.active", "allowed_parameters": ["namespace", "job"], "value_type": "integer"},
    "kubernetes.service_selector_matches": {"adapter": "kubernetes", "selector": "service.selector.matches", "allowed_parameters": ["namespace", "service", "selector"], "value_type": "boolean"},
    "business.pricing_quote_canonical_delta": {"adapter": "business_probe", "selector": "pricing.quote_vs_product_canonical.delta", "allowed_parameters": ["scenario_id", "product_id"], "value_type": "number"},
    "business.order_duplicate_count_since_t1": {"adapter": "database", "selector": "order.duplicate_business_key_count_since_t1", "allowed_parameters": ["scenario_id", "run_id", "t1"], "value_type": "integer"},
    "scenario.mock_flap_episode": {"adapter": "business_probe", "selector": "scenario_state.mock_flap.episode", "allowed_parameters": ["scenario_id"], "value_type": "integer"},
    "scenario.mock_flap_fault_active": {"adapter": "business_probe", "selector": "scenario_state.mock_flap.fault_active", "allowed_parameters": ["scenario_id"], "value_type": "boolean"},
    "external.probe_process_active": {"adapter": "business_probe", "selector": "external_57.tagged_process.active", "allowed_parameters": ["scenario_id"], "value_type": "boolean"},
}

SCENARIO_QUERY_IDS = {
    "F15-R": ["scenario.mock_flap_episode", "scenario.mock_flap_fault_active", "business.order_duplicate_count_since_t1"],
    "F07-P": ["kubernetes.job_active", "prometheus.apm_service_request_rate", "prometheus.apm_service_error_rate"],
    "F08-R": ["kubernetes.service_selector_matches", "business.pricing_quote_canonical_delta"],
    "F14-H": ["kubernetes.service_selector_matches", "business.pricing_quote_canonical_delta", "business.order_duplicate_count_since_t1"],
    "F12-G": ["external.probe_process_active"],
    "F06-P": ["business.food_payment_status_mix_since_t1"],
    "F14-G": ["business.inventory_balance_restored_since_t1", "business.order_terminal_consistency_since_t1"],
}
