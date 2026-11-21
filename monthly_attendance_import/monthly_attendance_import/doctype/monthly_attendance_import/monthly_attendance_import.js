frappe.ui.form.on("Monthly Attendance Import", {
    refresh(frm) {
        // Month is stored as 1–12; show the month names instead.
        frm.set_df_property("month", "options", [{label: "", value: ""}].concat(
            moment.months().map((label, i) => ({label: __(label), value: String(i + 1)}))));
        clearTimeout(frm.monthly_progress_timer);
        frm.monthly_progress_generation = (frm.monthly_progress_generation || 0) + 1;
        if (frm.is_new()) return;
        const title = __("Import progress");
        if (frm.doc.status === "Imported") {
            frm.dashboard.show_progress(title, 100, __("Completed: {0} created, {1} skipped.", [frm.doc.created_count, frm.doc.skipped_count]));
        } else if (frm.doc.status === "Failed") {
            frm.dashboard.show_progress(title, 0, __("Import failed. Business-record changes were rolled back; review the errors."));
        } else if (frm.monthly_import_running) {
            frm.dashboard.show_progress(title, 0, __("Importing now — validating the batch."));
        } else {
            frm.dashboard.show_progress(title, 0, frm.doc.status === "Queued"
                ? __("Queued — waiting for the worker.")
                : __("{0} entries proposed, {1} skipped. Import has not started.", [frm.doc.proposed_count || 0, frm.doc.skipped_count || 0]));
        }
        if (frm.monthly_import_running || frm.doc.status === "Queued") {
            const name = frm.doc.name;
            const generation = frm.monthly_progress_generation;
            const active = () => {
                const route = frappe.get_route();
                return generation === frm.monthly_progress_generation && cur_frm === frm && route[0] === "Form" && route[1] === frm.doctype && route[2] === name;
            };
            const poll = async () => {
                if (!active()) return;
                try {
                    const response = await frappe.call({method: "monthly_attendance_import.service.get_progress", args: {name}});
                    if (!active()) return;
                    const p = response.message;
                    if (!frm.monthly_import_running && p.status !== "Queued") {
                        await frm.reload_doc();
                        return;
                    }
                    const percent = p.total ? Math.min(99, Math.floor(100 * p.completed / p.total)) : 0;
                    const detail = p.total ? __("{0} of {1} steps processed (entries and overtime slips).", [p.completed, p.total]) : __("Waiting for processing counts.");
                    frm.dashboard.show_progress(title, percent,
                        `${__(p.phase)} — ${detail} ${__("Records are saved only when the whole batch succeeds.")}`);
                } catch (error) {
                    if (active()) frm.dashboard.show_progress(title, 0, __("Progress unavailable. Retrying; the import may still be running."));
                }
                if (active()) frm.monthly_progress_timer = setTimeout(poll, 2000);
            };
            frm.monthly_progress_timer = setTimeout(poll, 500);
        }
        if (frm.monthly_import_running) return;
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
                if (frm.monthly_import_running) return;
                frm.monthly_import_running = true;
                frm.clear_custom_buttons();
                frm.disable_save();
                await frm.trigger("refresh");
                try {
                    const response = await frappe.call({method: "monthly_attendance_import.service.start_import",
                        args: {name: frm.doc.name}});
                    frappe.show_alert({message: response.message.status === "Imported"
                        ? __("Import completed.") : __("Import failed. Review the errors."),
                        indicator: response.message.status === "Imported" ? "green" : "red"});
                } finally {
                    frm.monthly_import_running = false;
                    clearTimeout(frm.monthly_progress_timer);
                    frm.monthly_progress_generation += 1;
                    frm.enable_save();
                    await frm.reload_doc();
                }
            }).addClass("btn-primary");
        }
        if (frm.doc.status === "Imported") {
            frm.add_custom_button(__("View Attendance"), () => {
                const start = `${frm.doc.year}-${String(frm.doc.month).padStart(2, "0")}-01`;
                const end = moment(start).endOf("month").format("YYYY-MM-DD");
                frappe.route_options = {
                    company: frm.doc.company,
                    attendance_date: ["between", [start, end]],
                };
                frappe.set_route("List", "Attendance");
            }).addClass("btn-primary");
        }
        if (frm.doc.status === "Imported") {
            frm.add_custom_button(__("View Attendance"), () => {
                const start = `${frm.doc.year}-${String(frm.doc.month).padStart(2, "0")}-01`;
                const end = moment(start).endOf("month").format("YYYY-MM-DD");
                frappe.route_options = {
                    company: frm.doc.company,
                    attendance_date: ["between", [start, end]],
                };
                frappe.set_route("List", "Attendance");
            }).addClass("btn-primary");
        }
        if (frm.doc.status === "Queued") {
            frm.dashboard.set_headline_alert(__("Import queued or running. Progress updates automatically."));
        }
        frm.set_df_property("attachment", "description", __("Upload a private .xlsx file (maximum 5 MiB). Source files are retained for audit."));
    }
});
