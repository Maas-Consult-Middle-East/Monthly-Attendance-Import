"""Remove the obsolete schema only when no legacy records or audit links exist."""
import frappe


def execute():
    legacy = "Employee Overtime Entry"
    if not frappe.db.exists("DocType", legacy):
        return
    if frappe.db.count(legacy):
        frappe.throw("Employee Overtime Entry contains historical records. Archive or migrate them before removing this DocType.")
    result = "Monthly Attendance Import Result"
    if frappe.db.has_column(result, "overtime_entry") and frappe.db.exists(result, {"overtime_entry": ["is", "set"]}):
        frappe.throw("Import results still reference Employee Overtime Entry. Reconcile those audit links before removal.")
    frappe.reload_doc("monthly_attendance_import", "doctype", "monthly_attendance_import_result")
    frappe.delete_doc("DocType", legacy)
    frappe.clear_cache()
