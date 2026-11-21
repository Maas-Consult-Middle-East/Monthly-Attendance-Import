app_name = "monthly_attendance_import"
app_title = "Monthly Attendance Import"
app_publisher = "MAAS"
app_description = "Monthly Excel attendance and overtime-hours import"
app_email = "support@example.com"
app_license = "MIT"
required_apps = ["erpnext", "hrms"]

# All document-API writers participate in the same employee lock, including
# native attendance/leave/shift writers. No core overrides or permission bypass.
doc_events = {
    dt: {event: "monthly_attendance_import.service.lock_employee_document"
         for event in ("before_validate", "before_cancel", "on_trash")}
    for dt in ("Attendance", "Leave Application", "Shift Assignment", "Overtime Slip")
}
doc_events["File"] = {
    "validate": "monthly_attendance_import.service.protect_file",
    "on_trash": "monthly_attendance_import.service.protect_file",
}
doc_events["Overtime Slip"]["before_validate"] = "monthly_attendance_import.native_overtime.lock_and_check_overlap"
