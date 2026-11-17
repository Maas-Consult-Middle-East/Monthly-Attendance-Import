"""Focused service tests with mocked Frappe I/O; not database integration proof."""
import unittest
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import frappe
from monthly_attendance_import import service as s
from monthly_attendance_import.parser import Entry
from monthly_attendance_import.rules import RuleError


class PostingTests(unittest.TestCase):
    def test_import_runs_immediately_without_enqueuing(self):
        batch = frappe._dict(name='BATCH', status='Ready', fingerprint='preview')
        def finish(doc):
            self.assertEqual(doc.requested_by, 'importer')
            self.assertEqual(doc.status, 'Ready')
            doc.status = 'Imported'
        with patch.object(s, 'load_batch', return_value=batch), \
             patch.object(s, 'save_owned'), patch.object(s, 'execute', side_effect=finish) as execute, \
             patch.object(frappe, 'enqueue') as enqueue, \
             patch.object(frappe, 'session', frappe._dict(user='importer')):
            self.assertEqual(s.start_import('BATCH'), {'status': 'Imported'})
            execute.assert_called_once_with(batch)
            enqueue.assert_not_called()
            self.assertEqual(s.start_import('BATCH'), {'status': 'Imported'})
            execute.assert_called_once()

    def test_legacy_removal_refuses_records_or_audit_links(self):
        from monthly_attendance_import.patches.remove_unused_overtime_entry import execute
        for records, linked in [(1, False), (0, True)]:
            with patch.object(frappe, 'db', MagicMock()) as db, \
                 patch.object(frappe, 'throw', side_effect=ValueError('blocked')), \
                 patch.object(frappe, 'delete_doc') as delete:
                db.exists.return_value = True
                db.count.return_value = records
                db.has_column.return_value = linked
                with self.assertRaises(ValueError): execute()
                delete.assert_not_called()

    def test_progress_permissions_and_committed_status(self):
        batch = MagicMock(status='Ready', fingerprint='preview')
        with patch.object(frappe, 'get_doc', return_value=batch), patch.object(frappe, 'cache', MagicMock()) as cache:
            batch.check_permission.side_effect = frappe.PermissionError('denied')
            with self.assertRaises(frappe.PermissionError): s.get_progress('PRIVATE')
            cache.get_value.assert_not_called()
            batch.check_permission.side_effect = None
            cache.get_value.return_value = dict(phase='Finalizing', completed=10, total=10, fingerprint='preview')
            progress = s.get_progress('PRIVATE')
            self.assertEqual(progress['status'], 'Ready')
            self.assertEqual(progress['completed'], 10)
            batch.fingerprint = 'changed'
            self.assertEqual(s.get_progress('PRIVATE')['completed'], 0)
            batch.status = 'Failed'
            result = s.get_progress('PRIVATE')
            self.assertEqual(result['phase'], 'Failed')
            self.assertEqual(result['completed'], 0)

    def test_progress_cache_failure_does_not_change_transaction(self):
        with patch.object(frappe, 'cache', MagicMock()) as cache, patch.object(frappe, 'db', MagicMock()) as db:
            cache.set_value.side_effect = RuntimeError('cache unavailable')
            s.report_progress(frappe._dict(name='BATCH'), 'Processing entries', 25, 100)
            db.commit.assert_not_called()
            db.rollback.assert_not_called()

    def test_atomic_failure_clears_rolled_back_links(self):
        batch = MagicMock(fingerprint='fresh')
        attendance = MagicMock(status='Present', docstatus=0)
        attendance.name = 'ATT-NEW'
        overtime = MagicMock()
        overtime.insert.side_effect = ValueError('overtime permission denied')
        row = dict(employee_number='0007', attendance_date='2024-11-01', source_cells='F6/F7', result='Proposed')
        plan = [(row, attendance, overtime)]
        with patch.object(s, 'build_plan', return_value=(plan, [], 'fresh')), \
             patch.object(s, 'record') as record, patch.object(frappe, 'db', MagicMock()) as db:
            s.execute(batch)
            attendance.insert.assert_called_once()
            db.rollback.assert_called_once_with(save_point='monthly_post')
            self.assertNotIn('attendance', row)
            self.assertNotIn('overtime_entry', row)
            self.assertEqual(row['result'], 'Failed')
            self.assertEqual(record.call_args.args[-1], 'Failed')
            self.assertIn('F6/F7', record.call_args.args[3][0])

    def test_stale_preview_creates_nothing(self):
        batch = MagicMock(fingerprint='old')
        record_doc = MagicMock()
        row = dict(result='Proposed')
        with patch.object(s, 'build_plan', return_value=([(row, record_doc, None)], [], 'changed')), \
             patch.object(s, 'record') as record, patch.object(frappe, 'db', MagicMock()):
            s.execute(batch)
            record_doc.insert.assert_not_called()
            self.assertIn('stale', record.call_args.args[3][0])

    def test_whole_batch_validated_before_any_insert(self):
        doc = MagicMock()
        with patch.object(s, 'build_plan', return_value=([(dict(result='Proposed'), doc, None)], ['bad second employee'], 'x')), \
             patch.object(s, 'record'), patch.object(frappe, 'db', MagicMock()):
            s.execute(MagicMock())
            doc.insert.assert_not_called()

    def test_identical_retry_does_not_insert(self):
        batch = MagicMock(fingerprint='same')
        row = dict(result='Skipped', attendance='existing')
        with patch.object(s, 'build_plan', return_value=([(row, None, None)], [], 'same')), \
             patch.object(s, 'record') as record, patch.object(frappe, 'db', MagicMock()) as db:
            s.execute(batch)
            db.rollback.assert_not_called()
            self.assertEqual(record.call_args.args[-1], 'Imported')

    def test_employee_matching_unknown_ambiguous_company_and_permission(self):
        policy = frappe._dict(identifier_field='employee_number')
        for matches in ([], [frappe._dict(name='a'), frappe._dict(name='b')]):
            with patch.object(s, 'current_rows', return_value=matches), self.assertRaises(RuleError):
                s.resolve_employee('0007', policy, 'Company A')
        employee = MagicMock(company='Company B')
        with patch.object(s, 'current_rows', return_value=[frappe._dict(name='a')]), \
             patch.object(frappe, 'get_doc', return_value=employee):
            with self.assertRaisesRegex(RuleError, 'different company'):
                s.resolve_employee('0007', policy, 'Company A')
            employee.company = 'Company A'
            employee.check_permission.side_effect = frappe.PermissionError('denied')
            with self.assertRaises(frappe.PermissionError):
                s.resolve_employee('0007', policy, 'Company A')

    def test_permission_gate_before_batch_lock(self):
        batch = MagicMock()
        batch.check_permission.side_effect = frappe.PermissionError('denied')
        with patch.object(frappe, 'get_doc', return_value=batch) as get_doc, \
             patch.object(frappe, 'db', MagicMock()), patch.object(frappe, 'session', frappe._dict(user='test')):
            with self.assertRaises(frappe.PermissionError): s.load_batch('private-batch')
            self.assertEqual(get_doc.call_count, 1)

    def test_existing_attendance_state_and_hours(self):
        fields = dict(employee='E', company='C', attendance_date=date(2024, 11, 1), status='Present',
            shift=None, working_hours=8, docstatus=0, in_time=None, out_time=None, attendance_request=None)
        old = frappe._dict(fields)
        proposed = frappe._dict(fields)
        with patch('frappe.model.workflow.get_workflow_name', return_value=None):
            self.assertTrue(s.same_attendance(old, proposed))
            old.working_hours = 9
            self.assertFalse(s.same_attendance(old, proposed))
            old.working_hours, old.docstatus = 8, 1
            self.assertFalse(s.same_attendance(old, proposed))
            old.status = proposed.status = 'On Leave'
            old.working_hours = proposed.working_hours = 0
            old.leave_application = proposed.leave_application = 'APPROVED-LEAVE'
            old.leave_type = proposed.leave_type = 'ANNUAL LEAVE'
            self.assertFalse(s.same_attendance(old, proposed))
            self.assertTrue(s.same_attendance(old, proposed, approved_leave=True))
            old.leave_application = 'DIFFERENT-LEAVE'
            self.assertFalse(s.same_attendance(old, proposed, approved_leave=True))
            old.leave_application = proposed.leave_application
            with patch('frappe.model.workflow.get_workflow_name', return_value='Custom Workflow'):
                self.assertFalse(s.same_attendance(old, proposed, approved_leave=True))

    def test_leave_conflict_blocks_attendance(self):
        entry = Entry('0007', date(2024, 11, 1), Decimal(8), None, None, 'F6/F7')
        employee = frappe._dict(name='E', company='C', status='Active', date_of_joining=None, relieving_date=None)
        policy = frappe._dict(thresholds_confirmed=1, present_hours=8, half_day_hours=4, half_day_remainder='Absent')
        with patch.object(s, 'resolve_shift', return_value=None), \
             patch.object(s, 'is_holiday_for', return_value=False), \
             patch.object(s, 'nowdate', return_value='2024-12-01'), \
             patch.object(s, 'current_rows', return_value=[frappe._dict(name='leave')]):
            with self.assertRaisesRegex(RuleError, 'approved leave'):
                s.plan_entry(frappe._dict(company='C'), policy, entry, employee)

    def test_leave_code_requires_selected_type_and_approved_application(self):
        employee = frappe._dict(name='E', company='C', status='Active')
        for code, expected in [('VACATION', 'ANNUAL LEAVE'), ('SL', 'Sick Leave')]:
            entry = Entry('0007', date(2024, 11, 1), None, None, code, 'F6/F7')
            policy = frappe._dict({code.lower() + '_rule': 'Approved Leave', code.lower() + '_leave_type': expected})
            with patch.object(s, 'resolve_shift', return_value=None), \
                 patch.object(s, 'is_holiday_for', return_value=False), \
                 patch.object(s, 'nowdate', return_value='2024-12-01'), \
                 patch.object(frappe, 'get_doc', return_value=MagicMock()) as get_doc, \
                 patch.object(s, 'current_rows', return_value=[]) as rows:
                with self.assertRaisesRegex(RuleError, expected):
                    s.plan_entry(frappe._dict(company='C'), policy, entry, employee)
                get_doc.return_value.check_permission.assert_called_with('read')
                rows.return_value = [frappe._dict(half_day=0, leave_type='Wrong Type')]
                with self.assertRaisesRegex(RuleError, 'approved leave is Wrong Type'):
                    s.plan_entry(frappe._dict(company='C'), policy, entry, employee)
                policy[code.lower() + '_leave_type'] = None
                with self.assertRaisesRegex(RuleError, 'Select the'):
                    s.plan_entry(frappe._dict(company='C'), policy, entry, employee)

    def test_approved_leave_excludes_holidays_without_creating_attendance(self):
        entry = Entry('0007', date(2024, 11, 1), None, None, 'VACATION', 'F6/F7')
        employee = frappe._dict(name='E', company='C', status='Active')
        policy = frappe._dict(vacation_rule='Approved Leave', vacation_leave_type='Annual', holiday_rule='Use daily rule')
        leave = frappe._dict(name='APPROVED', half_day=0, leave_type='Annual')
        with patch.object(s, 'resolve_shift', return_value=None), \
             patch.object(s, 'is_holiday_for', return_value=True), \
             patch.object(s, 'nowdate', return_value='2024-12-01'), \
             patch.object(frappe, 'get_doc', return_value=MagicMock(include_holiday=0)), \
             patch.object(s, 'current_rows', side_effect=[[leave], []]), \
             patch.object(frappe, 'new_doc') as new_doc:
            row, attendance, overtime = s.plan_entry(frappe._dict(company='C'), policy, entry, employee)
            self.assertEqual(row['result'], 'Skipped')
            self.assertIn('Holiday excluded', row['message'])
            self.assertIsNone(attendance)
            new_doc.assert_not_called()

    def test_all_native_writers_use_same_lock(self):
        from monthly_attendance_import.hooks import doc_events
        for dt in ('Attendance', 'Leave Application', 'Shift Assignment'):
            self.assertEqual(doc_events[dt]['before_validate'], 'monthly_attendance_import.service.lock_employee_document')
        table = MagicMock()
        qb = MagicMock()
        qb.DocType.return_value = table
        doc = MagicMock()
        doc.get.return_value = 'EMP'
        doc.is_new.return_value = True
        with patch.object(frappe, 'qb', qb):
            s.lock_employee_document(doc)
            qb.from_.return_value.select.return_value.where.return_value.for_update.assert_called_once()


if __name__ == '__main__':
    unittest.main()
