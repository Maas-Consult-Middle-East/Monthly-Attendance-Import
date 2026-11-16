"""One native draft Overtime Slip per employee/month; never submit or pay."""
from calendar import monthrange
from datetime import date
from decimal import Decimal

import frappe
from frappe.utils import getdate
from monthly_attendance_import.rules import RuleError


def lock_and_check_overlap(doc, method=None):
    from monthly_attendance_import.service import current_rows, lock_employee_document
    lock_employee_document(doc)
    if not doc.employee or doc.docstatus == 2:
        return
    if not doc.start_date or not doc.end_date:
        doc.get_frequency_and_dates()
    for existing in current_rows("Overtime Slip", [("employee", "=", doc.employee),
        ("docstatus", "<", 2), ("start_date", "<=", doc.end_date), ("end_date", ">=", doc.start_date)]):
        if existing.name != doc.name:
            frappe.throw("An active native Overtime Slip already overlaps this employee's period")


def detail_values(detail):
    return (str(getdate(detail.get("date"))), detail.get("overtime_type"),
            Decimal(str(detail.get("overtime_duration") or 0)),
            Decimal(str(detail.get("standard_working_hours") or 0)),
            detail.get("reference_document") or "")


def identical(existing, proposed):
    if existing.docstatus != 0 or existing.company != proposed.company or existing.employee != proposed.employee:
        return False
    if (getdate(existing.start_date), getdate(existing.end_date)) != (getdate(proposed.start_date), getdate(proposed.end_date)):
        return False
    if existing.get("salary_slip") or existing.get("payroll_entry") or existing.get("submitted_via_payroll_entry"):
        return False
    if Decimal(str(existing.total_overtime_duration or 0)) != Decimal(str(proposed.total_overtime_duration)):
        return False
    if sorted(map(detail_values, existing.overtime_details)) != sorted(map(detail_values, proposed.overtime_details)):
        return False
    from frappe.model.workflow import get_workflow_name, get_workflow
    if get_workflow_name("Overtime Slip"):
        workflow = get_workflow("Overtime Slip")
        if existing.get(workflow.workflow_state_field) != proposed.get(workflow.workflow_state_field):
            return False
    return True


def prepare(batch, policy, plan):
    from monthly_attendance_import.service import current_rows, validate_document
    groups = {}
    for index, (row, attendance, _) in enumerate(plan):
        if row.get("result") != "Failed" and (attendance or row.get("attendance") or Decimal(row.get("overtime_hours") or "0") > 0):
            groups.setdefault(row["employee"], []).append(index)
    if not groups:
        return
    has_overtime = any(Decimal(plan[i][0].get("overtime_hours") or "0") > 0 for indices in groups.values() for i in indices)
    if has_overtime and not policy.overtime_type:
        raise RuleError("Select an existing native Overtime Type in the import policy")
    standard = Decimal(str(policy.standard_working_hours or 0))
    if has_overtime and (not standard.is_finite() or not 0 < standard <= 24):
        raise RuleError("Configure standard working hours (greater than 0, at most 24)")
    overtime_type = frappe.get_doc("Overtime Type", policy.overtime_type, for_update=True) if has_overtime else None
    if overtime_type:
        overtime_type.check_permission("read")
    maximum = Decimal(str(overtime_type.maximum_overtime_hours_allowed or 0)) if overtime_type else Decimal(0)
    start = date(int(batch.year), int(batch.month), 1)
    end = date(start.year, start.month, monthrange(start.year, start.month)[1])
    for employee, indices in groups.items():
        existing = current_rows("Overtime Slip", [("employee", "=", employee), ("docstatus", "<", 2),
            ("start_date", "<=", end), ("end_date", ">=", start)])
        indices = [i for i in indices if Decimal(plan[i][0].get("overtime_hours") or "0") > 0]
        if not indices:
            if existing:
                raise RuleError(f"{employee}: existing Overtime Slip conflicts with an upload proposing no overtime")
            continue
        rows = [plan[i][0] for i in indices]
        details = []
        for row in rows:
            duration = Decimal(row["overtime_hours"])
            if maximum > 0 and duration > maximum:
                raise RuleError(f"{row['employee_number']} {row['attendance_date']} ({row['source_cells']}): overtime exceeds the selected type's maximum")
            details.append(dict(date=row["attendance_date"], overtime_type=overtime_type.name,
                overtime_duration=float(duration), standard_working_hours=float(standard),
                reference_document=row.get("attendance")))
        slip = frappe.new_doc("Overtime Slip")
        slip.update(dict(employee=employee, company=batch.company,
            posting_date=start, start_date=start, end_date=end, overtime_details=details,
            total_overtime_duration=sum(d["overtime_duration"] for d in details)))
        slip._action = "save"
        slip.validate_workflow()
        if existing:
            if len(existing) != 1:
                raise RuleError(f"{employee}: multiple overlapping Overtime Slips require manual review")
            old = frappe.get_doc("Overtime Slip", existing[0].name, for_update=True)
            old.check_permission("read")
            if not identical(old, slip):
                raise RuleError(f"{rows[0]['employee_number']} ({rows[0]['source_cells']}): existing Overtime Slip conflicts in period, hours, type, reference or workflow state")
            for i in indices:
                row, attendance, _ = plan[i]
                row["overtime_slip"] = old.name
                row["result"] = "Proposed" if attendance else "Skipped"
                row["message"] = "Identical native draft Overtime Slip already exists"
        else:
            validate_document(slip)
            slip.flags.import_rows = rows
            slip.flags.overtime_type_revision = str(overtime_type.modified)
            first = indices[0]
            plan[first] = (plan[first][0], plan[first][1], slip)


def insert(slip):
    rows = slip.flags.import_rows
    for detail, row in zip(slip.overtime_details, rows, strict=True):
        detail.reference_document = row.get("attendance")
    slip.insert()
    if slip.docstatus != 0:
        raise RuleError("Native Overtime Slip must remain draft; submission can generate Additional Salary")
    for row in rows:
        row["overtime_slip"] = slip.name
        row["result"] = "Created"
