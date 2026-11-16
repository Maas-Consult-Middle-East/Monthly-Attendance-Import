frappe.ui.form.on("Monthly Attendance Import", {
    refresh(frm) {
        if (frm.is_new()) return;
        frm.add_custom_button(__("Download Template"), () => {
            if (frm.is_dirty()) return frappe.msgprint(__("Save the batch first."));
            window.open(`/api/method/monthly_attendance_import.service.download_template?name=${encodeURIComponent(frm.doc.name)}`);
        });
        if (frm.doc.status !== "Imported") {
            frm.add_custom_button(__("Validate & Preview"), async () => {
                if (frm.is_dirty()) await frm.save();
                await frappe.call({method: "monthly_attendance_import.service.preview",
                    args: {name: frm.doc.name}, freeze: true, freeze_message: __("Validating workbook…")});
                await frm.reload_doc();
            });
        }
        if (frm.doc.status === "Ready") {
            frm.add_custom_button(__("Import"), async () => {
                if (frm.is_dirty()) return frappe.msgprint(__("Save and preview your changes first."));
                await frappe.call({method: "monthly_attendance_import.service.start_import",
                    args: {name: frm.doc.name}, freeze: true});
                await frm.reload_doc();
                frappe.msgprint(__("Import queued. Reload this document to see results. Records remain drafts for approval."));
            }).addClass("btn-primary");
        }
        if (frm.doc.status === "Queued") {
            frm.dashboard.set_headline_alert(__("Import queued or running. Reload to check results."));
        }
        frm.set_df_property("attachment", "description", __("Upload a private .xlsx file (maximum 5 MiB). Source files are retained for audit."));
    }
});
