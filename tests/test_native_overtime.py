import unittest
from unittest.mock import MagicMock, patch
import frappe
from monthly_attendance_import import native_overtime as native


class NativeOvertimeTests(unittest.TestCase):
    def slip(self, hours=2):
        return frappe._dict(employee='E', company='C', docstatus=0,
            start_date='2026-11-01', end_date='2026-11-30', total_overtime_duration=hours,
            overtime_details=[frappe._dict(date='2026-11-01', overtime_type='Normal',
                overtime_duration=hours, standard_working_hours=8, reference_document='ATT-1')])

    def test_identical_slip_requires_hours_type_period_and_workflow(self):
        with patch('frappe.model.workflow.get_workflow_name', return_value=None):
            self.assertTrue(native.identical(self.slip(), self.slip()))
            self.assertFalse(native.identical(self.slip(3), self.slip()))
            old = self.slip();old.docstatus=1
            self.assertFalse(native.identical(old,self.slip()))
            old=self.slip();old.overtime_details[0].overtime_type='Other'
            self.assertFalse(native.identical(old,self.slip()))
            old=self.slip();old.overtime_details[0].reference_document='ATT-OTHER'
            self.assertFalse(native.identical(old,self.slip()))

    def test_insert_links_attendance_and_never_submits(self):
        slip=MagicMock(docstatus=0)
        slip.name='OT-1'
        rows=[dict(attendance='ATT-1'),dict(attendance='ATT-2')]
        slip.flags.import_rows=rows
        slip.overtime_details=[frappe._dict(),frappe._dict()]
        native.insert(slip)
        slip.insert.assert_called_once()
        slip.submit.assert_not_called()
        self.assertEqual([d.reference_document for d in slip.overtime_details],['ATT-1','ATT-2'])
        self.assertTrue(all(r['overtime_slip']=='OT-1' and r['result']=='Created' for r in rows))

    def test_no_overtime_type_is_invented(self):
        row=dict(employee='E',result='Proposed',overtime_hours='2')
        with self.assertRaisesRegex(ValueError,'Overtime Type'):
            native.prepare(frappe._dict(),frappe._dict(overtime_type=None),[(row,None,None)])
