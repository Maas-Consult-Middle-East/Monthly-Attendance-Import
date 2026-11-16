import math
import frappe
from frappe.model.document import Document
from frappe.utils import getdate, nowdate
from monthly_attendance_import.service import current_rows, lock_employee_document


class EmployeeOvertimeEntry(Document):
    def validate(self):
        lock_employee_document(self)
        if self.is_new() and not self.flags.from_monthly_import:
            frappe.throw("Create imported overtime through Monthly Attendance Import")
        employee = frappe.get_doc("Employee", self.employee)
        employee.check_permission("read")
        frappe.get_doc("Company", self.company).check_permission("read")
        frappe.get_doc("Monthly Attendance Import", self.import_batch).check_permission("read")
        if employee.company != self.company or employee.status != "Active":
            frappe.throw("Employee company or active status does not match")
        day = getdate(self.attendance_date)
        if day > getdate(nowdate()) or (employee.date_of_joining and day < getdate(employee.date_of_joining)) or (
            employee.relieving_date and day > getdate(employee.relieving_date)
        ):
            frappe.throw("Overtime date is outside active employment or in the future")
        if not math.isfinite(self.overtime_hours) or not 0 < self.overtime_hours <= 24:
            frappe.throw("Overtime must be finite and greater than zero, at most 24 hours")
        if self.attendance:
            attendance = frappe.get_doc("Attendance", self.attendance)
            attendance.check_permission("read")
            if (attendance.employee != self.employee or attendance.company != self.company
                or getdate(attendance.attendance_date) != day or attendance.docstatus == 2
                or (attendance.working_hours or 0) + self.overtime_hours > 24):
                frappe.throw("Linked attendance does not match overtime")
        if self.docstatus < 2:
            for row in current_rows(self.doctype, [("employee", "=", self.employee),
                ("attendance_date", "=", day), ("docstatus", "<", 2)]):
                if row.name != self.name:
                    frappe.throw("Overtime already exists for this employee/date")
        previous = self.get_doc_before_save()
        if previous:
            for field in ("employee", "company", "attendance_date", "overtime_hours", "attendance", "import_batch", "source_cells"):
                if self.get(field) != previous.get(field):
                    frappe.throw("Imported overtime values are immutable; cancel and re-import corrections")
        if self.docstatus == 0:
            self.approval_status = "Pending"

    def before_submit(self):
        if self.attendance and frappe.db.get_value("Attendance", self.attendance, "docstatus") != 1:
            frappe.throw("Submit linked Attendance through its approval workflow first")
        self.approval_status = "Approved"

    def before_cancel(self):
        lock_employee_document(self)
        self.approval_status = "Cancelled"

    def on_trash(self):
        frappe.throw("Overtime entries are retained for audit; cancel instead")
