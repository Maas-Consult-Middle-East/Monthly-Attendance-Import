# Monthly Attendance Import

A separate Frappe v15 app for previewing a monthly Excel matrix and creating **draft Attendance and draft Employee Overtime Entry documents**. No check-in timestamps, overtime rates, Additional Salary or payroll payments are generated. No core files or core DocType schemas are changed.

## Supported workbook

The user confirmed the supplied reference orientation: **employees in paired N.T/O.T rows, days in columns**. The initially described transposed orientation is not supported.

Use `monthly_attendance_import/public/templates/monthly-attendance-template.xlsx`, or save an import batch and choose **Download Template** to receive its selected month/year.

| Location | Meaning |
|---|---|
| M3 | Required `MONTH OF -NOV- 2026` style period; must match the batch |
| D4 / E4 | Exact `EMPLOYEE NUMBER` / `HOURS` headers |
| F5:AJ5 | Day numbers 1–31; invalid-month day headers may be blank |
| D6, D8, … | Employee numbers; template cells are text to preserve leading zeros |
| E6/E7, E8/E9, … | Exact N.T/O.T row pairs |
| F6:AJ7, etc. | Daily normal hours or code in N.T, numeric overtime hours in O.T |

Insert copies of rows 6–7 **above the notes**, entering each employee number in the N.T row. Pairs must be contiguous. Optional names/categories are ignored and never used for matching. The O.T identifier cell may be blank, merged, or equal to its N.T identifier. Existing reference numbers stored as integers with General/0 format are accepted without padding; formatted numeric identifiers are rejected because their leading zeros cannot safely be inferred.

Only the fixed daily range and declared headers are interpreted. Totals from AK onward, signatures and notes are ignored, including the reference's unrelated date at AN5. A gap followed by another employee pair is rejected. Invalid dates such as November 31 must be empty, including O.T; explicit zero there is an error. Daily formulas and Excel errors are forbidden. Hours must be finite, nonnegative, at most two decimal places, and normal plus overtime must not exceed 24.

Limits: one worksheet, 5 MiB compressed, 30 MiB expanded ZIP, 1,000 ZIP members, 2,000 rows × 80 columns, 100 employees (up to 3,100 employee-days). Oversized/ambiguous workbooks fail rather than truncate. Blanks, zeros and codes remain distinct in the preview and history.

## Inspection evidence and compatibility

Inspected local source versions: Frappe **15.120.0**, ERPNext **15.121.0**, HRMS **15.64.3**; Frappe already depends on `openpyxl~=3.1.5`. The package declares Frappe/ERPNext/HRMS v15 compatibility only.

Read-only inspection of `attendance.localhost` and `manpower.com` found ERPNext and HRMS installed, no configured Shift Types, no overtime DocTypes, and no custom Attendance hour fields. The local AEC Supervisor Attendance Sheet creates Employee Checkin records from supplied timestamps; the mobile attendance app also owns check-in behavior. Neither establishes monthly-import payroll policies or a suitable daily overtime-hours destination.

Native `Attendance.working_hours` stores normal hours. Native Attendance validation checks duplicate/overlapping shifts, employment and approved leave; the importer retains these validations and additionally rejects future dates, relieving-date violations and silent status changes. Native leave validation can otherwise convert a proposed status, so leave conflicts are explicitly blocked first. Shift holiday-list precedence is reused from HRMS. No native overtime DocType exists in the inspected HRMS version, so this app records overtime only in Employee Overtime Entry.

The supplied reference parsed as **59 employees / 1,770 employee-days with zero parsing errors**. This is not proof of employee matching, policy approval or a successful live import.

## Install

First install and test on a disposable/staging v15 site. Back up the target database and private files before deployment. With this app checked out under `apps/monthly_attendance_import`:

```sh
bench get-app /absolute/path/to/monthly_attendance_import
bench --site your-site install-app monthly_attendance_import
bench --site your-site migrate
bench build --app monthly_attendance_import
bench restart
```

For a checkout already inside the bench that is not yet registered, use the bench's normal local-app registration flow (editable install and `sites/apps.txt` registration) rather than copying DocTypes manually. `get-app` is intended for a source checkout outside the destination bench. Do not install on an incompatible major version or a site already using another daily overtime-hours posting path without reviewing that path first.

Keep Redis and the **long** worker running (`bench worker --queue long` in development). Restart all web and worker processes together after installation: all Attendance, Leave Application and Shift Assignment writers must load this app's lock hooks. Pause importing during a mixed-version rollout. No external service is introduced beyond the bench's existing Redis/RQ infrastructure.

All DocTypes, workspace, form script and template are version-controlled. No fixtures, custom fields, data migration, destructive conversion or backfill is needed. Standard app schema synchronization creates only this app's tables/workspace. There is no automatic site installation or production import in this delivery.

## Configure business rules

An **HR Manager** creates one **Monthly Attendance Import Policy** for each company. Policies start disabled and all payroll-sensitive decisions are unset. No 8-hour, zero-absence or unpaid-holiday assumption is supplied.

| Policy | Required decision |
|---|---|
| Identifier field | `name` means Employee ID; `employee_number` means the Employee Number field. Exact identifier matching only; unknown, duplicate and cross-company matches fail. |
| Thresholds | Explicitly confirm `0 < Half Day < Present <= 24`. Present is inclusive at its threshold; Half Day is inclusive at its threshold and below Present. Positive hours below Half Day are blocked. |
| Half-day remainder | Explicitly select Absent, or leave blocked. Half-day leave reconciliation requires the native workflow/manual review. |
| Blank rule | Skip or Block. Blanks never create absence. |
| Zero rule | Skip, Absent or Block. O.T zero with blank N.T is blocked for clarification. |
| Overtime-only | Record only (no fabricated Attendance) or Block. Existing Attendance on such a day requires manual review. |
| Shift | Require exactly one active assignment or Employee default; or explicitly No shift. No shift is rejected when an assignment/default exists. Multiple assignments require manual review because the sheet has no shift identifier. |
| Holidays | Use daily rule, Skip or Block. A holiday list must exist. Skip never discards positive overtime. |
| Codes | Independently configure EA, AB, SB, RH, HD, SL, UH, MD as Block, Skip, Absent, Approved Leave, Present or Half Day. No mapping is preselected. |

Approved Leave requires exactly one approved submitted full-day Leave Application. Other statuses conflicting with approved leave fail. Code-plus-overtime days are blocked because normal hours are unspecified; review them manually. No leave application or payroll entitlement is invented. A future date with actual attendance/overtime is blocked; blank skipped dates are harmless.

These are explicit scope limits, not inferred company policy. Required unset rules block affected entries, and any error prevents the entire batch from posting. The sample threshold values in tests are test fixtures only.

## Use and approve

1. Open the **Monthly Attendance Import** workspace as an HR Manager.
2. Create a batch with company, month and year, then save it.
3. Download the blank template or use a conforming reference workbook. Upload it as a **private** attachment to that saved batch.
4. Choose **Validate & Preview**. Inspect employee/date, original normal/overtime values, attendance code, source cells, decisions, errors and existing links. Fix the workbook or company policy, save and preview again.
5. Choose **Import** after a successful preview. The long worker reopens the file and revalidates under the initiating user's current permissions. Reload the batch for results.
6. Follow links to review draft Attendance and pending Employee Overtime Entry records. Submit Attendance through its normal approval workflow. An HR Manager can then submit linked overtime (Approved); cancellation marks it Cancelled. Configured native workflows still apply. The importer never auto-submits.

Counts are **employee-day results**, not the sum of Attendance and overtime document counts. A Created result can link both documents. Skipped includes explicit no-action policy decisions and identical existing drafts. Proposed counts remaining proposals; errors are counted separately. History snapshots retain each validation/import outcome, actor, file URL, fingerprint and detailed rows. File replacement requires uploading a new File; old private attachments remain retained. Audit batches/files and overtime entries cannot be deleted through normal document APIs.

## Posting, retries and recovery

The saved batch owns input state and append-only audit history; the browser cannot set processing results or history. Input changes invalidate preview. File bytes, selected inputs, policy, employee revisions, active workflows, proposed values and current records are fingerprinted. Import recalculates the plan; a changed interpretation or relevant state forces another preview. The posting action does not accept browser-supplied rows.

Permission checks cover batch read/write, company access, private File read, Employee read and new business-document create. No `ignore_permissions` is used. Disabled initiating users cannot post. Permission-sensitive preflight validation is repeated inside the worker. Broad internal queries are used only for collision detection, followed by document permission checks before using or exposing matching records.

Batch/company/policy and employee locks serialize competing imports. Current locking reads avoid stale snapshot duplicate checks. Native Attendance, Leave Application and Shift Assignment document-API writers also acquire the same Employee row lock before validating/cancelling/deleting. Native Attendance still performs its own duplicate and overlapping-shift checks. The custom overtime controller acquires that lock and rejects active employee/date duplicates. Direct SQL writers outside Frappe document APIs are outside this concurrency contract and must not be used.

All entries are validated before any business insert. Attendance and overtime inserts, links and successful history are in one transaction. A failure rolls back all new business records; failure history is then saved without rolled-back links. Submitted records are never changed or considered identical to a proposed draft. An existing draft is skipped only if relevant values, timestamps/requests, link and initial workflow state match; conflicts are reported, never overwritten. Retry of an Imported batch is a no-op; retry through a new identical upload skips matching drafts. A changed approval/submission state is a conflict.

For Failed batches, correct the cause and **Validate & Preview** again before Import. If a worker was killed, timed out, lost permission or never received an enqueue callback, check RQ/worker logs, then choose **Validate & Preview**. It refuses while the job is queued/running; after the job ends it reconstructs the preview under the current authorized user. Database transactions roll back on lost connections. This recovery covers failures where the original worker could not persist its error; it does not automatically impersonate an administrator.

Standard Frappe hooks/workflows run during insertion. Site-specific hooks must be transaction-safe and must not commit midway or perform irreversible external side effects; review such hooks in staging. The app itself never commits inside posting and never sends email/messages or creates payroll payments.

## Verification

Executed in the supplied environment:

```sh
PYTHONPATH=apps/monthly_attendance_import env/bin/python -m unittest discover -s apps/monthly_attendance_import/tests -v
node --check apps/monthly_attendance_import/monthly_attendance_import/monthly_attendance_import/doctype/monthly_attendance_import/monthly_attendance_import.js
env/bin/python -m compileall -q apps/monthly_attendance_import
```

The wheel and source distribution also built successfully using cached build dependencies (`uv build --offline`).

**22 unit tests passed.** Tests cover valid pairs, leading zeros, blanks/zero, codes and half-day thresholds, November 31/leap February, invalid hours/formulas/errors, fixed boundaries, employee ambiguity/company/permissions, leave conflict, identical/conflicting attendance state, stale previews, whole-batch preflight, rollback-link cleanup and shared locking hooks. Service I/O is mocked in unit tests; these do **not** establish real database concurrency or live import correctness. The committed template was rendered and visually checked; its period customization and parser compatibility are exercised by tests.

Real database tests are included separately. On a disposable site whose name starts with `test_monthly_`, install ERPNext, HRMS and this app, enable tests, then run:

```sh
bench --site test_monthly_import.localhost set-config allow_tests true
bench --site test_monthly_import.localhost run-tests --module monthly_attendance_import.tests.test_integration
```

They create/commit isolated fixtures and test real inserts/retries/conflicts, atomic rollback, stale policy/input changes, and two simultaneous native Attendance writers. Drop the entire disposable site after reviewing results; do not point this suite at production. These tests and live UI/worker execution were **not run here**: local database read access was available, but database-root access for creating a separate test database was denied. Existing sites were inspected read-only. No production business records were created or modified.

## Rollback and retention

Forward rollout is additive: back up, install app tables/workspace, migrate/build, restart all workers/web, configure policy, test, then enable imports. Existing attendance and check-in paths keep their data shapes and receive only serialization hooks.

For operational rollback, disable company import policies, allow running jobs to finish or fail, and keep this app installed so audit records and overtime links remain readable. Deploy the previous app version if needed after validating schema compatibility. Do **not** uninstall the app to disable it: uninstalling drops its tables and would destroy overtime/audit history. Any deletion/uninstall or cleanup of business records requires a separate approved retention/migration plan and backups. This delivery performs no destructive contraction.
