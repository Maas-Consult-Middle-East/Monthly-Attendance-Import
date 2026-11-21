import math
import frappe
from frappe.model.document import Document
from monthly_attendance_import.rules import validate_policy, RuleError


class MonthlyAttendanceImportPolicy(Document):
    def validate(self):
        frappe.get_doc("Company", self.company).check_permission("read")
        try:
            validate_policy(self)
        except RuleError as exc:
            frappe.throw(str(exc))
        if self.thresholds_confirmed and not (
            math.isfinite(self.half_day_hours) and math.isfinite(self.present_hours)
            and 0 < self.half_day_hours < self.present_hours <= 24
        ):
            frappe.throw("Thresholds must satisfy 0 < Half Day < Present <= 24")
        if self.has_value_changed("company") and not self.is_new():
            frappe.throw("Create a separate policy for a different company")
