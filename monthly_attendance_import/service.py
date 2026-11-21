"""Permission checks, interpretation, current reads and atomic posting."""
import hashlib
import json
from decimal import Decimal

import frappe
from frappe.utils import getdate, now_datetime, nowdate

from monthly_attendance_import.parser import parse
from monthly_attendance_import.rules import decide, normal_hours, validate_policy, RuleError

BATCH = "Monthly Attendance Import"
POLICY = "Monthly Attendance Import Policy"
OVERTIME = "Overtime Slip"
INPUTS = ("company", "month", "year", "attachment", "worksheet")


def current_rows(doctype, conditions):
    """Current (locking) read, not a stale repeatable-read snapshot."""
    table = frappe.qb.DocType(doctype)
    query = frappe.qb.from_(table).select(table.star)
    for field, operator, value in conditions:
        column = table[field]
        expression = {"=": lambda: column == value, "<": lambda: column < value,
                      "<=": lambda: column <= value, ">=": lambda: column >= value}[operator]()
        query = query.where(expression)
    return query.for_update().run(as_dict=True)


def lock_employee_document(doc, method=None):
    employees = {doc.get("employee")}
    if not doc.is_new():
        employees.add(frappe.db.get_value(doc.doctype, doc.name, "employee"))
    for employee in sorted(e for e in employees if e):
        table = frappe.qb.DocType("Employee")
        frappe.qb.from_(table).select(table.name).where(table.name == employee).for_update().run()


def protect_file(doc, method=None):
    previous = doc.get_doc_before_save() if method != "on_trash" else doc
    if not previous and not doc.is_new():
        previous = frappe.get_doc("File", doc.name)
    for candidate in (previous, doc):
        if candidate and candidate.attached_to_doctype == BATCH and candidate.attached_to_name:
            if method == "on_trash" or (previous and any(
                previous.get(f) != doc.get(f) for f in
                ("file_url", "is_private", "attached_to_doctype", "attached_to_name", "content_hash")
            )):
                frappe.throw("Import source files are retained for audit; upload a new attachment instead")
            if not doc.is_private:
                frappe.throw("Monthly attendance workbooks must be private")


def authorize(batch):
    if not frappe.db.get_value("User", frappe.session.user, "enabled"):
        frappe.throw("The initiating user is disabled", frappe.PermissionError)
    batch.check_permission("read")
    batch.check_permission("write")
    frappe.get_doc("Company", batch.company, for_update=True).check_permission("read")


def load_batch(name):
    doc = frappe.get_doc(BATCH, name)
    authorize(doc)
    doc = frappe.get_doc(BATCH, name, for_update=True)
    authorize(doc)
    return doc


def read_source(batch):
    names = frappe.get_all("File", filters={"file_url": batch.attachment,
        "attached_to_doctype": BATCH, "attached_to_name": batch.name, "is_private": 1}, pluck="name")
    if not names:
        frappe.throw("Attach a private .xlsx file to this saved import batch")
    source = frappe.get_doc("File", names[0])
    source.check_permission("read")
    if not source.file_name.lower().endswith(".xlsx") or not source.file_url.startswith("/private/files/"):
        frappe.throw("A local private .xlsx attachment is required")
    from pathlib import Path
    from monthly_attendance_import.parser import MAX_BYTES
    if Path(source.get_full_path()).stat().st_size > MAX_BYTES:
        frappe.throw("File exceeds 5 MiB")
    return source.get_content()


def policy_for(batch):
    policy = frappe.get_doc(POLICY, batch.company, for_update=True)
    policy.check_permission("read")
    if not policy.enabled:
        frappe.throw("Enable and configure the company import policy before previewing")
    validate_policy(policy)
    if policy.identifier_field not in ("name", "employee_number"):
        frappe.throw("Select Employee ID (name) or employee_number in the company policy")
    return policy


def resolve_employee(identifier, policy, company):
    matches = current_rows("Employee", [(policy.identifier_field, "=", identifier)])
    if len(matches) != 1:
        raise RuleError("Employee number is unknown or ambiguous")
    employee = frappe.get_doc("Employee", matches[0].name, for_update=True)
    employee.check_permission("read")
    if employee.company != company:
        raise RuleError("Employee belongs to a different company")
    return employee


def resolve_shift(employee, day, policy):
    assignments = current_rows("Shift Assignment", [
        ("employee", "=", employee.name), ("docstatus", "=", 1),
        ("status", "=", "Active"), ("start_date", "<=", day)])
    assignments = [a for a in assignments if not a.end_date or getdate(a.end_date) >= day]
    if len(assignments) > 1:
        raise RuleError("Multiple shift assignments on this date require manual review")
    shift = assignments[0].shift_type if assignments else employee.default_shift
    if policy.shift_rule == "No shift":
        if shift:
            raise RuleError("No-shift policy conflicts with an assigned/default shift")
        return None
    if policy.shift_rule != "Require assignment or default" or not shift:
        raise RuleError("Configure shift policy and a unique assignment/default shift")
    frappe.get_doc("Shift Type", shift, for_update=True).check_permission("read")
    return shift


def workflow_snapshot(doctype):
    names = frappe.get_all("Workflow", filters={"document_type": doctype, "is_active": 1}, pluck="name")
    if len(names) > 1:
        raise RuleError("Multiple active workflows require administrator review")
    if not names:
        return None
    workflow = frappe.get_doc("Workflow", names[0], for_update=True)
    if not workflow.states or int(workflow.states[0].doc_status) != 0:
        raise RuleError("Importer requires a draft initial workflow state")
    return workflow.as_dict()


def is_holiday_for(employee, shift, day):
    from hrms.utils.holiday_list import get_holiday_list_for_employee, get_holiday_dates_between
    holiday_list = (frappe.get_doc("Shift Type", shift).get_holiday_list(employee, day)
                    if shift else get_holiday_list_for_employee(employee, as_on=day))
    if not holiday_list:
        raise RuleError("Configure an effective Holiday List Assignment before importing")
    frappe.get_doc("Holiday List", holiday_list, for_update=True)
    return bool(get_holiday_dates_between(holiday_list, day, day))


def same_attendance(existing, proposed, approved_leave=False):
    fields = ("employee", "company", "attendance_date", "status", "shift", "leave_type",
              "leave_application", "half_day_status", "modify_half_day_status", "late_entry", "early_exit")
    from frappe.model.workflow import get_workflow_name, get_workflow
    native_leave = (approved_leave and existing.docstatus == 1 and proposed.status == "On Leave"
        and proposed.leave_application and not get_workflow_name("Attendance"))
    if existing.docstatus != 0 and not native_leave:
        return False
    for field in fields:
        if str(existing.get(field) or "") != str(proposed.get(field) or ""):
            return False
    if Decimal(str(existing.working_hours or 0)) != Decimal(str(proposed.working_hours or 0)):
        return False
    if (existing.in_time or existing.out_time or existing.attendance_request
        or existing.get("overtime_type") or existing.get("actual_overtime_duration")):
        return False
    if get_workflow_name("Attendance"):
        wf = get_workflow("Attendance")
        if existing.get(wf.workflow_state_field) != proposed.get(wf.workflow_state_field):
            return False
    return True


def validate_document(doc):
    doc._action = "save"
    doc.check_permission("create")
    original_name = doc.name
    # Native mandatory checks require child.parent even before an insert allocates
    # a naming-series value. Keep this temporary identity out of the saved plan.
    if not original_name:
        doc.name = "new-monthly-import-preview"
    doc.set_parent_in_children()
    try:
        doc._validate_links()
        doc.run_method("before_validate")
        doc.run_method("validate")
        doc._validate()
    finally:
        doc.name = original_name
        doc.set_parent_in_children()


def plan_entry(batch, policy, entry, employee):
    decision = decide(entry, policy)
    normal = normal_hours(entry, policy)
    row = dict(employee_number=entry.identifier, employee=employee.name,
        attendance_date=str(entry.date), normal_hours="" if normal is None else str(normal),
        overtime_hours="" if entry.overtime_hours is None else str(entry.overtime_hours),
        attendance_code=entry.code, source_cells=entry.cells, proposed_status=decision.status,
        result="Skipped", message=decision.reason)
    if not decision.status and not decision.overtime:
        return row, None, None
    if employee.status != "Active" or (employee.date_of_joining and entry.date < getdate(employee.date_of_joining)) or (
        employee.relieving_date and entry.date > getdate(employee.relieving_date)
    ):
        raise RuleError("Date is outside active employment")
    if entry.date > getdate(nowdate()):
        raise RuleError("Future attendance/overtime requires manual processing")
    shift = resolve_shift(employee, entry.date, policy)
    holiday = is_holiday_for(employee.name, shift, entry.date)
    if holiday:
        if policy.holiday_rule == "Skip":
            if decision.overtime:
                raise RuleError("Holiday skip cannot discard overtime")
            row.update(message="Holiday: explicit skip policy", proposed_status=None)
            return row, None, None
        if policy.holiday_rule != "Use daily rule":
            raise RuleError("Configure holiday handling; holidays are not assumed unpaid")
    leave = current_rows("Leave Application", [("employee", "=", employee.name),
        ("docstatus", "=", 1), ("status", "=", "Approved"),
        ("from_date", "<=", entry.date), ("to_date", ">=", entry.date)])
    if decision.status == "On Leave":
        expected_type = policy.get(entry.code.lower() + "_leave_type") if entry.code in ("VACATION", "SL") else None
        if entry.code in ("VACATION", "SL") and not expected_type:
            raise RuleError(f"Select the {entry.code} Leave Type in the import policy")
        if expected_type:
            frappe.get_doc("Leave Type", expected_type).check_permission("read")
        if len(leave) != 1 or (leave[0].half_day and getdate(leave[0].half_day_date) == entry.date):
            raise RuleError(f"Leave code requires exactly one approved full-day Leave Application{(' of type ' + expected_type) if expected_type else ''}")
        if expected_type and leave[0].leave_type != expected_type:
            raise RuleError(f"{entry.code} requires {expected_type}; approved leave is {leave[0].leave_type}")
        frappe.get_doc("Leave Application", leave[0].name).check_permission("read")
        leave_type = frappe.get_doc("Leave Type", leave[0].leave_type, for_update=True)
        leave_type.check_permission("read")
        if holiday and not leave_type.include_holiday:
            if current_rows("Attendance", [("employee", "=", employee.name),
                ("attendance_date", "=", entry.date), ("docstatus", "<", 2)]):
                raise RuleError("Existing attendance on a holiday excluded from leave requires review")
            row.update(message="Holiday excluded by approved Leave Type: no Attendance", proposed_status=None)
            return row, None, None
    elif leave:
        raise RuleError("Conflicts with approved leave; review through the native leave workflow")
    attendance = None
    if decision.status:
        attendance = frappe.new_doc("Attendance")
        attendance.update(dict(employee=employee.name,
            company=batch.company, attendance_date=entry.date, status=decision.status,
            shift=shift, working_hours=float(normal or 0),
            half_day_status="Absent" if decision.status == "Half Day" else None))
        if leave:
            attendance.update(dict(leave_type=leave[0].leave_type, leave_application=leave[0].name))
        attendance._action = "save"
        attendance.validate_workflow()
    existing = current_rows("Attendance", [("employee", "=", employee.name),
        ("attendance_date", "=", entry.date), ("docstatus", "<", 2)])
    if existing:
        if attendance is None:
            raise RuleError("Overtime-only import conflicts with existing attendance; review manually")
        if len(existing) != 1:
            raise RuleError("Multiple existing attendance records require manual review")
        old = frappe.get_doc("Attendance", existing[0].name, for_update=True)
        old.check_permission("read")
        if not same_attendance(old, attendance, approved_leave=decision.status == "On Leave" and bool(leave)):
            raise RuleError("Conflicting attendance values or workflow state; existing record is untouched")
        row["attendance"] = old.name
        attendance = None
    elif attendance:
        expected = attendance.status
        validate_document(attendance)
        if attendance.status != expected:
            raise RuleError("Native attendance validation changed the proposed status")
    row["result"] = "Proposed" if attendance or decision.overtime else "Skipped"
    if row["result"] == "Skipped" and row.get("attendance"):
        row["message"] = "Matching approved leave Attendance already exists" if decision.status == "On Leave" else "Identical draft Attendance already exists"
    return row, attendance, None


def build_plan(batch):
    content = read_source(batch)
    policy = policy_for(batch)
    entries, errors = parse(content, int(batch.year), int(batch.month), batch.get("worksheet"))
    workflows = [workflow_snapshot(dt) for dt in ("Attendance", OVERTIME)]
    employees, employee_errors = {}, {}
    # Stable employee lock order avoids cross-batch lock inversion.
    identifiers = sorted({e.identifier for e in entries})
    matched = {}
    for identifier in identifiers:
        matches = frappe.get_all("Employee", filters={policy.identifier_field: identifier}, pluck="name")
        matched[identifier] = matches[0] if len(matches) == 1 else ""
    for identifier in sorted(identifiers, key=lambda i: (matched[i], i)):
        try:
            employees[identifier] = resolve_employee(identifier, policy, batch.company)
        except Exception as exc:
            employee_errors[identifier] = str(exc)
    plan = []
    for entry in entries:
        try:
            if entry.identifier in employee_errors:
                raise RuleError(employee_errors[entry.identifier])
            plan.append(plan_entry(batch, policy, entry, employees[entry.identifier]))
        except Exception as exc:
            message = f"{entry.identifier} {entry.date} ({entry.cells}): {exc}"
            errors.append(message)
            plan.append((dict(employee_number=entry.identifier, attendance_date=str(entry.date),
                source_cells=entry.cells, result="Failed", message=message), None, None))
    if not errors:
        try:
            from monthly_attendance_import.native_overtime import prepare
            prepare(batch, policy, plan)
        except Exception as exc:
            errors.append(str(exc))
    candidate_fields = ("employee", "company", "attendance_date", "status", "shift", "working_hours",
        "leave_type", "leave_application", "half_day_status", "overtime_hours", "approval_status")
    candidates = [[{f: d.get(f) for f in candidate_fields} if d else None for d in (a, o)]
                  for _, a, o in plan]
    payload = dict(file=hashlib.sha256(content).hexdigest(), inputs=[batch.get(f) for f in INPUTS],
        policy=policy.as_dict(), workflows=workflows, rows=[r for r, _, _ in plan],
        candidates=candidates,
        native_slips=[dict(employee=o.employee, start=str(o.start_date), end=str(o.end_date),
            type_revision=o.flags.overtime_type_revision,
            details=[{f:d.get(f) for f in ("date", "overtime_type", "overtime_duration", "standard_working_hours", "reference_document")} for d in o.overtime_details])
            for _, _, o in plan if o],
        employees={key: value.modified for key, value in employees.items()})
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return plan, errors, fingerprint


def record(batch, action, plan, errors, fingerprint, status):
    batch.status, batch.fingerprint = status, fingerprint
    batch.error = "\n".join(errors)
    batch.set("results", [r for r, _, _ in plan])
    for name, state in (("proposed", "Proposed"), ("created", "Created"), ("skipped", "Skipped")):
        batch.set(name + "_count", sum(r["result"] == state for r, _, _ in plan))
    batch.failed_count = len(errors)
    batch.append("history", dict(occurred_at=now_datetime(), actor=frappe.session.user,
        action=action, attachment=batch.attachment, fingerprint=fingerprint,
        details=json.dumps(dict(status=status, errors=errors, results=[r for r, _, _ in plan]), default=str)))
    save_owned(batch)


def save_owned(batch):
    batch.flags.from_monthly_import = True
    try:
        batch.save()
    finally:
        batch.flags.from_monthly_import = False


@frappe.whitelist(methods=["POST"])
def preview(name):
    batch = load_batch(name)
    if batch.status == "Queued":
        from frappe.utils.background_jobs import is_job_enqueued
        if is_job_enqueued(f"monthly-attendance:{batch.name}"):
            frappe.throw("Import is queued or running; wait for it to finish")
        # Also recovers jobs killed by worker timeout: their transaction rolled back.
    if batch.status == "Imported":
        frappe.throw("This batch is locked; create a new batch for a different upload")
    frappe.db.savepoint("monthly_preview")
    message_count = len(frappe.local.message_log)
    try:
        plan, errors, fingerprint = build_plan(batch)
    except Exception as exc:
        plan, errors, fingerprint = [], [str(exc)], ""
    # Native validations msgprint per row (e.g. "Employee is on Leave"); preview reports
    # blocking problems through `errors`, so drop those informational popups.
    del frappe.local.message_log[message_count:]
    # Native validation hooks must not persist incidental writes during preview.
    frappe.db.rollback(save_point="monthly_preview")
    record(batch, "Validate & Preview", plan, errors, fingerprint, "Invalid" if errors else "Ready")
    return {"status": batch.status, "errors": errors}


@frappe.whitelist(methods=["POST"])
def start_import(name):
    batch = load_batch(name)
    if batch.status == "Imported":
        return {"status": "Imported"}
    if batch.status == "Queued":
        frappe.throw("This older batch is queued. Wait for it to finish or Validate & Preview after its job ends.")
    if batch.status != "Ready" or not batch.fingerprint:
        frappe.throw("Validate & Preview successfully before importing")
    batch.requested_by = frappe.session.user
    save_owned(batch)
    # Run immediately under the caller's authorization and existing batch lock.
    # Frappe commits the request after validation and atomic posting completes.
    execute(batch)
    return {"status": batch.status}


def run_import(name, actor):
    """Compatibility only for jobs queued before immediate importing was introduced."""
    original_user = frappe.session.user
    try:
        frappe.set_user(actor)
        batch = load_batch(name)
        if batch.status != "Queued" or batch.requested_by != actor:
            return
        execute(batch)
    finally:
        frappe.set_user(original_user)


def report_progress(batch, phase, completed, total):
    # Progress is advisory, outside the business transaction; never commit records
    # to expose intermediate results. A cache outage must not change posting.
    try:
        frappe.cache.set_value("monthly-import-progress:" + batch.name,
            dict(phase=phase, completed=completed, total=total, fingerprint=batch.fingerprint), expires_in_sec=3600)
    except Exception:
        pass


@frappe.whitelist()
def get_progress(name):
    batch = frappe.get_doc(BATCH, name)
    batch.check_permission("read")
    frappe.get_doc("Company", batch.company).check_permission("read")
    progress = (frappe.cache.get_value("monthly-import-progress:" + name) or {}) if batch.status in ("Ready", "Queued") else {}
    if progress.get("fingerprint") != batch.fingerprint:
        progress = {}
    return dict(status=batch.status, phase=progress.get("phase", batch.status),
        completed=progress.get("completed", 0), total=progress.get("total", 0))


def execute(batch):
    plan, fingerprint = [], ""
    frappe.db.savepoint("monthly_post")
    try:
        report_progress(batch, "Validating", 0, 0)
        plan, errors, fingerprint = build_plan(batch)
        if errors:
            raise RuleError("\n".join(errors))
        if fingerprint != batch.fingerprint:
            raise RuleError("Preview is stale: input, policy, employee, workflow or existing records changed; preview again")
        total = len(plan) + sum(bool(o) for _, _, o in plan)
        completed = 0
        report_progress(batch, "Processing entries", 0, total)
        for row, attendance, overtime in plan:
            try:
                if attendance:
                    expected = attendance.status
                    attendance.insert()
                    if attendance.status != expected or attendance.docstatus != 0:
                        raise RuleError("Native hooks changed attendance status; review manually")
                    row["attendance"] = attendance.name
                if attendance:
                    row["result"] = "Created"
                completed += 1
                if completed % 25 == 0 or completed == len(plan):
                    report_progress(batch, "Processing entries", completed, total)
            except Exception as exc:
                raise RuleError(f"{row['employee_number']} {row['attendance_date']} ({row['source_cells']}): {exc}") from exc
        from monthly_attendance_import.native_overtime import insert as insert_overtime
        for row, _, overtime in plan:
            if overtime:
                try:
                    insert_overtime(overtime)
                    completed += 1
                    report_progress(batch, "Recording overtime", completed, total)
                except Exception as exc:
                    raise RuleError(f"{row['employee_number']} {row['attendance_date']} ({row['source_cells']}): {exc}") from exc
        report_progress(batch, "Finalizing", completed, total)
        record(batch, "Import", plan, [], fingerprint, "Imported")
    except Exception as exc:
        frappe.db.rollback(save_point="monthly_post")
        # Discard names from rolled-back inserts, preserving only pre-existing links.
        for row, attendance, overtime in plan:
            if attendance:
                row.pop("attendance", None)
            if overtime:
                for slip_row in overtime.flags.import_rows:
                    slip_row.pop("overtime_slip", None)
            if row["result"] in ("Created", "Proposed"):
                row["result"] = "Failed"
                row["message"] = "Batch rolled back: " + str(exc)
        batch.reload()
        record(batch, "Import failed", plan, [str(exc)], "", "Failed")


@frappe.whitelist()
def download_template(name):
    batch = load_batch(name)
    from monthly_attendance_import.template import make_template
    frappe.local.response.filename = "monthly-attendance-template.xlsx"
    frappe.local.response.filecontent = make_template(int(batch.year), int(batch.month), batch.get("worksheet"))
    frappe.local.response.type = "binary"
