"""Experiment 6: Autonomous Lifecycle Decision Engine.

Deterministic business-rules engine (not an LLM call) synthesizing damage
score, device age, repair history, warranty status, and user role into a
lifecycle recommendation. Mirrors the report's 6-rule decision tree and
cost model exactly.
"""

EXECUTIVE_FIELD_AGE_THRESHOLD_MONTHS = 36
STANDARD_AGE_THRESHOLD_MONTHS = 48
REPAIR_TO_REPLACEMENT_DISPOSAL_RATIO = 0.60

REPAIR_COST_BY_SEVERITY = {
    "Minor": 150,
    "Moderate": 450,
    "Severe": 1200,
}
REFURBISH_COST = 600
DISPOSE_COST = 50
WARRANTY_COST_REDUCTION = 0.80  # 80% cost reduction if in warranty


def _repair_cost(severity_category: str, warranty_active: bool) -> float:
    base = REPAIR_COST_BY_SEVERITY.get(severity_category, REPAIR_COST_BY_SEVERITY["Moderate"])
    if warranty_active:
        base = base * (1 - WARRANTY_COST_REDUCTION)
    return round(base, 2)


def run(
    *,
    damage_severity_score: int,
    severity_category: str,
    device_age_months: int,
    repair_count: int,
    warranty_active: bool,
    employee_role: str | None,
    replacement_value: float,
) -> dict:
    role = (employee_role or "standard").lower()
    age_threshold = (
        EXECUTIVE_FIELD_AGE_THRESHOLD_MONTHS
        if role in ("executive", "field")
        else STANDARD_AGE_THRESHOLD_MONTHS
    )
    is_aged_device = device_age_months >= age_threshold

    decision = None
    reason = None
    rule_applied = None
    estimated_cost = 0.0

    # Rule 1: Critical damage (>75) -> Dispose
    if damage_severity_score > 75:
        decision, reason, rule_applied = "Dispose", "Critical damage severity (>75).", "rule_1_critical_damage"
        estimated_cost = DISPOSE_COST

    # Rule 2: Severe damage + aged device -> Dispose
    elif severity_category == "Severe" and is_aged_device:
        decision, reason, rule_applied = (
            "Dispose",
            f"Severe damage on a device past the {age_threshold}-month age threshold for role '{role}'.",
            "rule_2_severe_and_aged",
        )
        estimated_cost = DISPOSE_COST

    # Rule 3: Excessive repairs (>=3) -> Refurbish
    elif repair_count >= 3:
        decision, reason, rule_applied = (
            "Refurbish",
            f"Device has {repair_count} prior repairs, exceeding the 3-repair threshold.",
            "rule_3_excessive_repairs",
        )
        estimated_cost = REFURBISH_COST

    # Rule 4: Moderate/Severe damage -> Repair (warranty-adjusted, cost-threshold gated)
    elif severity_category in ("Moderate", "Severe"):
        repair_cost = _repair_cost(severity_category, warranty_active)
        disposal_threshold = replacement_value * REPAIR_TO_REPLACEMENT_DISPOSAL_RATIO
        if repair_cost >= disposal_threshold:
            decision = "Dispose"
            reason = (
                f"Estimated repair cost ${repair_cost} is >= "
                f"{int(REPAIR_TO_REPLACEMENT_DISPOSAL_RATIO * 100)}% of replacement value "
                f"(${replacement_value}); disposal is more economical."
            )
            rule_applied = "rule_4b_repair_uneconomical"
            estimated_cost = DISPOSE_COST
        else:
            decision = "Repair"
            reason = f"{severity_category} damage; repair estimated at ${repair_cost}."
            rule_applied = "rule_4a_repair"
            estimated_cost = repair_cost

    # Rule 5: Minor damage -> Continue usage
    elif severity_category == "Minor":
        decision, reason, rule_applied = (
            "Continue Use",
            "Minor damage does not warrant repair or replacement.",
            "rule_5_minor_continue",
        )
        estimated_cost = 0.0

    # Rule 6: Age-based refresh for executive/developer roles
    elif is_aged_device and role in ("executive", "developer"):
        decision, reason, rule_applied = (
            "Refresh",
            f"Device exceeds {age_threshold}-month refresh threshold for role '{role}'.",
            "rule_6_age_based_refresh",
        )
        estimated_cost = replacement_value

    else:
        decision, reason, rule_applied = (
            "Continue Use",
            "No damage, repair, or age threshold triggered.",
            "default_continue",
        )
        estimated_cost = 0.0

    return {
        "agent": "lifecycle_decision",
        "decision": decision,
        "reason": reason,
        "rule_applied": rule_applied,
        "estimated_cost_usd": estimated_cost,
        "is_aged_device": is_aged_device,
        "age_threshold_months": age_threshold,
        "requires_dual_approval": decision == "Dispose",
    }
