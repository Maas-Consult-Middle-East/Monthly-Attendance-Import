import frappe
from frappe.model.document import Document
from monthly_attendance_import.service import INPUTS


class MonthlyAttendanceImport(Document):
    def validate(self):
        frappe.get_doc("Company", self.company).check_permission("read")
        if not 1 <= int(self.month or 0) <= 12 or not 1900 <= int(self.year or 0) <= 2200:
            frappe.throw("Select a valid month and year (1900–2200)")
        previous = self.get_doc_before_save()
        if self.flags.from_monthly_import:
            return
        owned = ("status", "fingerprint", "requested_by", "error", "proposed_count", "created_count",
                 "skipped_count", "failed_count", "results", "history")
        if previous:
            if any(self.get(f) != previous.get(f) for f in INPUTS):
                if previous.status in ("Imported", "Queued"):
                    frappe.throw("Queued/imported batches are immutable; create a new batch")
                self.status = "Draft"
                self.fingerprint = ""
                self.error = "Inputs changed; Validate & Preview again"
                self.set("results", [])
                for f in ("proposed_count", "created_count", "skipped_count", "failed_count"):
                    self.set(f, 0)
            else:
                for f in owned:
                    if f not in ("results", "history") and self.get(f) != previous.get(f):
                        frappe.throw("Import results and state are server managed")
                self.set("results", [r.as_dict() for r in previous.results])
            # Always restore append-only history; API clients cannot forge it.
            self.set("history", [r.as_dict() for r in previous.history])
        else:
            self.status = "Draft"
            for f in owned:
                if f != "status":
                    self.set(f, [] if f in ("results", "history") else None)

    def on_trash(self):
        frappe.throw("Import batches are retained for audit")
