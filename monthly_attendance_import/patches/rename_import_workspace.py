"""Separate workspace and DocType routes without changing import records."""
import frappe


def execute():
    old, new = "Monthly Attendance Import", "Monthly Attendance Imports"
    if not frappe.db.exists("Workspace", old):
        return
    workspace = frappe.get_doc("Workspace", old)
    if workspace.module != "Monthly Attendance Import":
        frappe.throw("The conflicting workspace is not owned by Monthly Attendance Import")
    if frappe.db.exists("Workspace", new):
        frappe.throw("Resolve the duplicate Monthly Attendance Imports workspace before migrating")
    frappe.rename_doc("Workspace", old, new)
    workspace = frappe.get_doc("Workspace", new)
    workspace.title = new
    workspace.label = new
    workspace.save()
    frappe.clear_cache()
