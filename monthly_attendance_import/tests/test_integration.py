"""Run ONLY on an isolated test_monthly_* site with all three required apps installed.

Includes real two-connection concurrency and database rollback tests. These tests
are intentionally separate from the portable unit suite and commit test fixtures.
"""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import frappe
from openpyxl import load_workbook

from monthly_attendance_import import service
from monthly_attendance_import.template import make_template


class TestMonthlyIntegration(unittest.TestCase):
    def setUp(self):
        if not (frappe.local.site.startswith('test_monthly_') or frappe.local.site == 'monthly-import-v16.localhost'):
            self.skipTest('Requires a disposable test_monthly_* site; never run on production')
        frappe.set_user('Administrator')
        self.suffix = uuid4().hex[:8]
        self.company = frappe.get_doc(dict(doctype='Company', company_name='MAI Test ' + self.suffix,
            abbr=self.suffix[:5], default_currency='SAR', country='Saudi Arabia')).insert().name
        holiday = frappe.get_doc(dict(doctype='Holiday List', holiday_list_name='MAI ' + self.suffix,
            from_date='2024-01-01', to_date='2024-12-31')).insert()
        self.employee = frappe.get_doc(dict(doctype='Employee', first_name='Monthly Test',
            company=self.company, employee_number='0007-' + self.suffix, gender='Male',
            date_of_birth='1990-01-01', date_of_joining='2024-01-01', status='Active',
            holiday_list=holiday.name)).insert()
        frappe.get_doc(dict(doctype='Holiday List Assignment', holiday_list=holiday.name,
            applicable_for='Company', assigned_to=self.company, from_date='2024-01-01')).insert().submit()
        component = frappe.get_doc(dict(doctype='Salary Component', salary_component='MAI OT ' + self.suffix,
            salary_component_abbr=self.suffix, type='Earning')).insert()
        overtime_type = frappe.get_doc(dict(doctype='Overtime Type', name='MAI OT ' + self.suffix,
            overtime_salary_component=component.name, overtime_calculation_method='Fixed Hourly Rate',
            hourly_rate=0, standard_multiplier=1, applicable_for_weekend=0)).insert()
        self.policy = frappe.get_doc(dict(doctype=service.POLICY, company=self.company, enabled=1,
            identifier_field='employee_number', thresholds_confirmed=1, present_hours=8,
            half_day_hours=4, half_day_remainder='Absent', blank_rule='Skip', zero_rule='Absent',
            shift_rule='No shift', holiday_rule='Use daily rule', overtime_type=overtime_type.name, standard_working_hours=8,
            overtime_only_rule='Record only', f_rule='Block', vacation_rule='Block',
            **{c.lower()+'_rule':'Block' for c in ('EA','AB','SB','RH','HD','SL','UH','MD')})).insert()
        frappe.db.commit()

    def batch(self, hours=8, overtime=2):
        batch = frappe.get_doc(dict(doctype=service.BATCH, company=self.company, year=2024, month=11)).insert()
        workbook = load_workbook(BytesIO(make_template(2024, 11)))
        sheet = workbook.active
        sheet['D6'] = self.employee.employee_number
        sheet['F6'], sheet['F7'] = hours, overtime
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        source = frappe.get_doc(dict(doctype='File', file_name='monthly.xlsx', is_private=1,
            attached_to_doctype=service.BATCH, attached_to_name=batch.name,
            content=stream.getvalue())).insert()
        batch.attachment = source.file_url
        batch.save()
        service.preview(batch.name)
        batch.reload()
        self.assertEqual(batch.status, 'Ready', batch.error)
        return batch

    def test_import_retry_and_conflicting_reupload(self):
        batch = self.batch()
        self.assertEqual(service.start_import(batch.name)['status'], 'Imported')
        batch.reload()
        self.assertEqual(batch.status, 'Imported', batch.error)
        self.assertEqual(batch.created_count, 1)
        attendance = frappe.get_doc('Attendance', batch.results[0].attendance)
        self.assertEqual(attendance.working_hours, 8)
        self.assertEqual(attendance.docstatus, 0)
        self.assertEqual(service.start_import(batch.name)['status'], 'Imported')
        retry = self.batch()
        service.execute(retry)
        retry.reload()
        self.assertEqual(retry.created_count, 0)
        self.assertEqual(frappe.db.count('Attendance', {'employee': self.employee.name}), 1)
        # Changed hours fail preview without overwriting the existing record.
        with self.assertRaises(AssertionError): self.batch(hours=9)
        attendance.reload()
        self.assertEqual(attendance.working_hours, 8)

    def test_present_code_preserves_confirmed_hours(self):
        self.policy.present_hours = 9
        self.policy.save()
        batch = self.batch(hours='P')
        service.execute(batch)
        batch.reload()
        self.assertEqual(batch.status, 'Imported', batch.error)
        attendance = frappe.get_doc('Attendance', batch.results[0].attendance)
        self.assertEqual(attendance.status, 'Present')
        self.assertEqual(attendance.working_hours, 9)

    def test_weekly_off_retains_native_overtime_without_attendance(self):
        self.policy.f_rule = 'Skip attendance; retain overtime'
        self.policy.save()
        batch = self.batch(hours='F', overtime=5)
        service.execute(batch)
        batch.reload()
        self.assertEqual(batch.status, 'Imported', batch.error)
        self.assertFalse(frappe.db.exists('Attendance', {'employee': self.employee.name}))
        slip = frappe.get_doc('Overtime Slip', batch.results[0].overtime_slip)
        self.assertEqual(slip.total_overtime_duration, 5)
        self.assertEqual(slip.docstatus, 0)
        self.assertFalse(slip.overtime_details[0].reference_document)

    def test_native_approved_leave_is_reused_unchanged(self):
        leave_type = frappe.get_doc(dict(doctype='Leave Type', leave_type_name='MAI Annual ' + self.suffix, include_holiday=1)).insert()
        frappe.get_doc(dict(doctype='Leave Allocation', employee=self.employee.name,
            leave_type=leave_type.name, from_date='2024-01-01', to_date='2024-12-31', new_leaves_allocated=31)).insert().submit()
        leave = frappe.get_doc(dict(doctype='Leave Application', employee=self.employee.name,
            leave_type=leave_type.name, from_date='2024-11-01', to_date='2024-11-01',
            status='Approved', leave_approver='Administrator')).insert()
        leave.submit()
        name = frappe.db.get_value('Attendance', {'employee':self.employee.name, 'leave_application':leave.name}, 'name')
        before = frappe.get_doc('Attendance', name).as_dict()
        self.policy.update(dict(vacation_rule='Approved Leave', vacation_leave_type=leave_type.name))
        self.policy.save()
        batch = self.batch(hours='VACATION', overtime=None)
        service.execute(batch)
        batch.reload()
        self.assertEqual(batch.status, 'Imported', batch.error)
        self.assertEqual(batch.created_count, 0)
        self.assertEqual(batch.results[0].attendance, name)
        self.assertEqual(frappe.get_doc('Attendance', name).as_dict(), before)

    def test_database_atomic_rollback(self):
        batch = self.batch()
        from hrms.hr.doctype.overtime_slip.overtime_slip import OvertimeSlip
        with patch.object(OvertimeSlip, 'before_insert', create=True, side_effect=ValueError('injected overtime failure')):
            service.execute(batch)
        batch.reload()
        self.assertEqual(batch.status, 'Failed')
        self.assertIn('injected overtime failure', batch.error)
        self.assertFalse(frappe.db.exists('Attendance', {'employee': self.employee.name}))
        self.assertFalse(frappe.db.exists(service.OVERTIME, {'employee': self.employee.name}))
        self.assertFalse(any(r.attendance or r.overtime_slip for r in batch.results))
        service.preview(batch.name)
        batch.reload()
        service.execute(batch)
        self.assertEqual(frappe.db.count('Attendance', {'employee': self.employee.name}), 1)

    def test_stale_policy_and_inputs(self):
        batch = self.batch()
        self.policy.present_hours = 9
        self.policy.save()
        service.execute(batch)
        batch.reload()
        self.assertEqual(batch.status, 'Failed')
        self.assertFalse(frappe.db.exists('Attendance', {'employee': self.employee.name}))
        batch.month = 10
        batch.save()
        self.assertEqual(batch.status, 'Draft')
        self.assertFalse(batch.fingerprint)

    def test_two_native_attendance_writers_serialize(self):
        # Both begin with no Attendance row. Employee row lock must serialize them.
        frappe.db.commit()
        site, sites_path = frappe.local.site, frappe.local.sites_path
        employee, company = self.employee.name, self.company
        barrier = threading.Barrier(2)

        def writer():
            try:
                frappe.init(site=site, sites_path=sites_path)
                frappe.connect()
                frappe.set_user('Administrator')
                barrier.wait(timeout=20)
                frappe.get_doc(dict(doctype='Attendance', employee=employee, company=company,
                    attendance_date='2024-11-01', status='Present', working_hours=8)).insert()
                frappe.db.commit()
                return 'created'
            except Exception as exc:
                frappe.db.rollback()
                return type(exc).__name__
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(writer), pool.submit(writer)]
            outcomes = [f.result(timeout=40) for f in futures]
        self.assertEqual(outcomes.count('created'), 1, outcomes)
        self.assertIn('DuplicateAttendanceError', outcomes)
        frappe.db.rollback()
        self.assertEqual(frappe.db.count('Attendance', {'employee': employee}), 1)

    def tearDown(self):
        frappe.db.rollback()

    def test_two_native_overtime_writers_serialize(self):
        frappe.db.commit()
        site, sites_path = frappe.local.site, frappe.local.sites_path
        employee, company, overtime_type = self.employee.name, self.company, self.policy.overtime_type
        barrier = threading.Barrier(2)

        def writer():
            try:
                frappe.init(site=site, sites_path=sites_path)
                frappe.connect()
                frappe.set_user('Administrator')
                barrier.wait(timeout=20)
                frappe.get_doc(dict(doctype='Overtime Slip', employee=employee, company=company,
                    posting_date='2024-11-01', start_date='2024-11-01', end_date='2024-11-30',
                    total_overtime_duration=2, overtime_details=[dict(date='2024-11-01',
                    overtime_type=overtime_type, overtime_duration=2, standard_working_hours=8)])).insert()
                frappe.db.commit()
                return 'created'
            except Exception as exc:
                frappe.db.rollback()
                return type(exc).__name__
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(writer), pool.submit(writer)]
            outcomes = [job.result(timeout=40) for job in jobs]
        self.assertEqual(outcomes.count('created'), 1, outcomes)
        self.assertIn('ValidationError', outcomes)
        frappe.db.rollback()
        self.assertEqual(frappe.db.count('Overtime Slip', {'employee': employee}), 1)
