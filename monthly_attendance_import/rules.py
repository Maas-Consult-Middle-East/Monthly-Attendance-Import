"""Explicit policy interpretation. Unresolved payroll decisions fail closed."""
from dataclasses import dataclass
from decimal import Decimal


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    status: str | None
    overtime: Decimal
    reason: str = ""


def decide(entry, policy):
    normal, overtime, code = entry.normal_hours, entry.overtime_hours, entry.code
    overtime = overtime or Decimal(0)
    if code:
        if overtime:
            raise RuleError("Attendance code with overtime needs explicit normal hours; review manually")
        action = policy.get(code.lower() + "_rule")
        if not action or action == "Block":
            raise RuleError(f"Configure the {code} code rule")
        if action == "Skip":
            if overtime:
                raise RuleError("Skipped attendance code cannot discard overtime")
            return Decision(None, overtime, f"Explicit {code} policy: skip")
        if action == "Approved Leave":
            if overtime:
                raise RuleError("Leave code with overtime requires manual review")
            return Decision("On Leave", overtime)
        if action == "Half Day" and policy.get("half_day_remainder") != "Absent":
            raise RuleError("Half Day requires an explicit remainder rule")
        if action == "Absent" and overtime:
            raise RuleError("Absence with overtime requires manual review")
        return Decision(action, overtime)
    if normal is None and not overtime and entry.overtime_hours is None:
        if policy.get("blank_rule") != "Skip":
            raise RuleError("Configure blank rule; blanks never mean absence")
        return Decision(None, overtime, "Blank: no attendance proposed")
    if not normal and overtime:
        if policy.get("overtime_only_rule") != "Record only":
            raise RuleError("Configure overtime-only rule")
        return Decision(None, overtime, "Overtime only: no attendance proposed")
    if normal is None:
        raise RuleError("Explicit O.T zero with blank N.T requires a normal-hours value or code")
    if normal == 0:
        action = policy.get("zero_rule")
        if action not in ("Skip", "Absent"):
            raise RuleError("Configure explicit zero rule")
        return Decision("Absent" if action == "Absent" else None, overtime, "Explicit zero")
    if not policy.get("thresholds_confirmed"):
        raise RuleError("Confirm Present and Half Day thresholds")
    half, present = Decimal(str(policy.get("half_day_hours") or 0)), Decimal(str(policy.get("present_hours") or 0))
    if not (0 < half < present <= 24):
        raise RuleError("Thresholds must satisfy 0 < Half Day < Present <= 24")
    if normal >= present:
        return Decision("Present", overtime)
    if normal >= half:
        if policy.get("half_day_remainder") != "Absent":
            raise RuleError("Half Day requires an explicit remainder rule")
        return Decision("Half Day", overtime)
    raise RuleError("Positive normal hours below Half Day threshold require manual review")
