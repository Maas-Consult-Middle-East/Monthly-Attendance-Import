"""Explicit policy interpretation. Unresolved payroll decisions fail closed."""
from dataclasses import dataclass
from decimal import Decimal


class RuleError(ValueError):
    pass


def validate_policy(policy):
    """Require explicit decisions before an enabled policy can be used or saved."""
    if not policy.get("enabled"):
        return
    required = {
        "identifier_field": "Identifier Field", "thresholds_confirmed": "Thresholds Confirmed",
        "present_hours": "Present Hours", "half_day_hours": "Half Day Hours",
        "half_day_remainder": "Half Day Remainder", "blank_rule": "Blank Rule",
        "zero_rule": "Zero Rule", "overtime_only_rule": "Overtime Only Rule",
        "shift_rule": "Shift Rule", "holiday_rule": "Holiday Rule",
        "overtime_type": "Overtime Type", "standard_working_hours": "Standard Working Hours",
        "f_rule": "F Weekly Off Rule", "vacation_rule": "VACATION Rule",
        **{code.lower() + "_rule": code + " code rule" for code in ("EA", "AB", "SB", "RH", "HD", "SL", "UH", "MD")},
    }
    if policy.get("vacation_rule") == "Approved Leave":
        required["vacation_leave_type"] = "VACATION Leave Type"
    if policy.get("sl_rule") == "Approved Leave":
        required["sl_leave_type"] = "SL Leave Type"
    missing = [label for field, label in required.items() if not policy.get(field)]
    if missing:
        raise RuleError("Complete the enabled import policy: " + ", ".join(missing)
            + ". Select Block for unused code rules.")
    present = Decimal(str(policy.get("present_hours")))
    half = Decimal(str(policy.get("half_day_hours")))
    standard = Decimal(str(policy.get("standard_working_hours")))
    if not all(v.is_finite() for v in (present, half, standard)) or not 0 < half < present <= 24:
        raise RuleError("Thresholds must satisfy 0 < Half Day < Present <= 24")
    if not 0 < standard <= 24:
        raise RuleError("Standard Working Hours must be greater than 0 and at most 24")


@dataclass(frozen=True)
class Decision:
    status: str | None
    overtime: Decimal
    reason: str = ""


def normal_hours(entry, policy):
    if entry.code != "P":
        return entry.normal_hours
    hours = Decimal(str(policy.get("present_hours") or 0))
    if not hours.is_finite() or not 0 < hours <= 24:
        raise RuleError("Configure Present Hours for P (greater than 0, at most 24)")
    if hours + (entry.overtime_hours or 0) > 24:
        raise RuleError("P normal hours plus overtime exceed 24")
    return hours


def decide(entry, policy):
    normal, overtime, code = normal_hours(entry, policy), entry.overtime_hours, entry.code
    overtime = overtime or Decimal(0)
    if code == "P":
        return Decision("Present", overtime)
    if code == "F":
        if policy.get("f_rule") != "Skip attendance; retain overtime":
            raise RuleError("Confirm F weekly-off handling in the import policy")
        return Decision(None, overtime, "Weekly off: no Attendance; overtime retained")
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
