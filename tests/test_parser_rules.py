import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from io import BytesIO

from openpyxl import load_workbook
from monthly_attendance_import.parser import Entry, WorkbookError, parse, MAX_BYTES
from monthly_attendance_import.rules import decide, validate_policy, RuleError
from monthly_attendance_import.template import make_template


def workbook(changes=None, year=2024, month=11):
    book = load_workbook(BytesIO(make_template(year, month)))
    sheet = book.active
    sheet['D6'] = '0007'
    for cell, value in (changes or {}).items():
        sheet[cell] = value
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


class ParserTests(unittest.TestCase):
    def test_selected_sheet_codes_and_merged_vacation(self):
        template = load_workbook(BytesIO(make_template(2024, 11, 'ALAbraqi')))
        self.assertEqual(template.sheetnames, ['ALAbraqi'])
        template.close()
        book = load_workbook(BytesIO(workbook({'F6': 'VACATION'})))
        sheet = book.active
        sheet.title = 'ALAbraqi'
        sheet.merge_cells('F6:H7')
        sheet['I6'], sheet['I7'] = 'P', '-'
        sheet['J6'], sheet['J7'] = 'F', 5
        book.create_sheet('Unrelated')['A1'] = '=1/0'
        output = BytesIO(); book.save(output); book.close()
        with self.assertRaisesRegex(WorkbookError, 'Select the worksheet'):
            parse(output.getvalue(), 2024, 11)
        entries, errors = parse(output.getvalue(), 2024, 11, 'ALAbraqi')
        self.assertFalse(errors)
        self.assertEqual([e.code for e in entries[:5]], ['VACATION'] * 3 + ['P', 'F'])
        self.assertIn('merged F6:H7', entries[1].cells)
        self.assertIsNone(entries[3].overtime_hours)
        self.assertEqual(entries[4].overtime_hours, 5)
        with self.assertRaises(WorkbookError):
            parse(output.getvalue(), 2024, 11, 'Missing')
        book = load_workbook(output)
        book['Unrelated'].sheet_state = 'hidden'
        output = BytesIO(); book.save(output); book.close()
        entries, errors = parse(output.getvalue(), 2024, 11)
        self.assertFalse(errors)
        self.assertEqual(entries[0].code, 'VACATION')

    def test_merged_numeric_hours_are_not_distributed(self):
        book = load_workbook(BytesIO(workbook({'F6': 8})))
        book.active.merge_cells('F6:H7')
        output = BytesIO(); book.save(output); book.close()
        with self.assertRaisesRegex(WorkbookError, 'single employee'):
            parse(output.getvalue(), 2024, 11)

    def test_valid_pair_and_leading_zeros(self):
        entries, errors = parse(workbook({'F6': 7.5, 'F7': 2}), 2024, 11)
        self.assertFalse(errors)
        self.assertEqual(len(entries), 30)
        self.assertEqual(entries[0].identifier, '0007')
        self.assertEqual(entries[0].normal_hours, Decimal('7.5'))
        self.assertEqual(entries[0].overtime_hours, 2)
        self.assertIn('F6/F7', entries[0].cells)

    def test_blank_distinct_from_zero_and_code(self):
        entries, _ = parse(workbook({'G6': 0, 'H6': 'AB'}), 2024, 11)
        self.assertIsNone(entries[0].normal_hours)
        self.assertEqual(entries[1].normal_hours, 0)
        self.assertEqual(entries[2].code, 'AB')

    def test_november_31_empty_or_populated(self):
        for value in (0, 1, '=1', '#VALUE!'):
            with self.subTest(value=value):
                _, errors = parse(workbook({'AJ6': value}), 2024, 11)
                self.assertIn('invalid calendar day', errors[0])
                self.assertIn('AJ6/AJ7', errors[0])

    def test_leap_february(self):
        entries, errors = parse(workbook({'AH6': 8}, 2024, 2), 2024, 2)
        self.assertFalse(errors)
        self.assertEqual(entries[-1].date, date(2024, 2, 29))
        _, errors = parse(workbook({'AH6': 8}, 2023, 2), 2023, 2)
        self.assertIn('invalid calendar day', errors[0])

    def test_rejects_bad_hours_formulas_errors(self):
        for value in (-1, 25, 'NaN', 'Infinity', '=4+4', '#VALUE!', True, 0.001, 'UNKNOWN'):
            with self.subTest(value=value):
                _, errors = parse(workbook({'F6': value}), 2024, 11)
                self.assertTrue(errors)
                self.assertIn('0007 2024-11-01', errors[0])

    def test_total_over_24(self):
        _, errors = parse(workbook({'F6': 20, 'F7': 5}), 2024, 11)
        self.assertIn('exceed 24', errors[0])

    def test_period_headers_pairs_duplicates(self):
        for change in ({'M3': 'MONTH OF -NOV- 2025'}, {'D4': 'NAME'}, {'F5': 2}, {'E7': 'N.T'},
                       {'D8': '0007', 'E8': 'N.T', 'E9': 'O.T'}):
            with self.subTest(change=change), self.assertRaises(WorkbookError):
                parse(workbook(change), 2024, 11)

    def test_ignore_totals_notes_and_unrelated_date(self):
        entries, errors = parse(workbook({'AK6': '=SUM(F6:AJ6)', 'AN5': 'July 2025', 'A30': 'note'}), 2024, 11)
        self.assertFalse(errors)
        self.assertEqual(len(entries), 30)

    def test_size_dimension_and_numeric_identifier(self):
        with self.assertRaises(WorkbookError): parse(b'x' * (MAX_BYTES + 1), 2024, 11)
        with self.assertRaises(WorkbookError): parse(workbook({'A2001': 'note'}), 2024, 11)
        # D6 is explicitly text-formatted in the template: numeric coercion is forbidden.
        with self.assertRaises(WorkbookError): parse(workbook({'D6': 12345}), 2024, 11)


class RulesTests(unittest.TestCase):
    def test_enabled_policy_reports_missing_decisions_together(self):
        validate_policy({'enabled': 0})
        with self.assertRaises(RuleError) as error:
            validate_policy({'enabled': 1, 'present_hours': 8})
        self.assertIn('Shift Rule', str(error.exception))
        self.assertIn('Holiday Rule', str(error.exception))
        self.assertIn('Overtime Type', str(error.exception))
        policy = dict(enabled=1, identifier_field='employee_number', thresholds_confirmed=1,
            present_hours=8, half_day_hours=4, half_day_remainder='Absent', blank_rule='Skip',
            zero_rule='Skip', overtime_only_rule='Block', shift_rule='No shift', holiday_rule='Block',
            overtime_type='Test', standard_working_hours=8, f_rule='Block', vacation_rule='Block',
            **{c.lower() + '_rule':'Block' for c in ('EA','AB','SB','RH','HD','SL','UH','MD')})
        validate_policy(policy)
        for code, label in [('vacation', 'VACATION'), ('sl', 'SL')]:
            pending = dict(policy, **{code+'_rule':'Approved Leave'})
            with self.assertRaisesRegex(RuleError, label+' Leave Type'):
                validate_policy(pending)
            pending[code+'_leave_type'] = 'Approved type'
            validate_policy(pending)
        with self.assertRaisesRegex(RuleError, 'Standard Working Hours'):
            validate_policy(dict(policy, standard_working_hours=25))

    def test_present_weekly_off_and_vacation(self):
        entry = replace(self.entry, normal_hours=None, code='P')
        with self.assertRaises(RuleError): decide(entry, dict(self.policy, present_hours=0))
        self.policy.update(f_rule='Skip attendance; retain overtime', vacation_rule='Approved Leave')
        self.assertEqual(decide(entry, self.policy).status, 'Present')
        self.assertEqual(decide(replace(entry, code='F', overtime_hours=Decimal(5)), self.policy).overtime, 5)
        self.assertIsNone(decide(replace(entry, code='F'), self.policy).status)
        self.assertEqual(decide(replace(entry, code='VACATION', overtime_hours=None), self.policy).status, 'On Leave')
        with self.assertRaises(RuleError): decide(replace(entry, overtime_hours=Decimal(17)), self.policy)

    def test_p_uses_present_hours_even_with_old_separate_value(self):
        from monthly_attendance_import.rules import normal_hours
        entry = replace(self.entry, normal_hours=None, code='P')
        policy = dict(self.policy, present_hours=9, p_hours=6)
        self.assertEqual(normal_hours(entry, policy), 9)
        policy['present_hours'] = 7
        self.assertEqual(normal_hours(entry, policy), 7)
        for value in (0, -1, 25, 'NaN', 'Infinity'):
            with self.assertRaises(RuleError):
                normal_hours(entry, dict(policy, present_hours=value))

    def setUp(self):
        self.entry = Entry('0007', date(2024, 11, 1), Decimal(8), Decimal(2), None, 'F6/F7')
        self.policy = dict(thresholds_confirmed=1, half_day_hours=4, present_hours=8,
            half_day_remainder='Absent', blank_rule='Skip', zero_rule='Absent')

    def test_threshold_boundaries(self):
        self.assertEqual(decide(self.entry, self.policy).status, 'Present')
        self.assertEqual(decide(replace(self.entry, normal_hours=Decimal(4)), self.policy).status, 'Half Day')
        with self.assertRaises(RuleError): decide(replace(self.entry, normal_hours=Decimal(3)), self.policy)

    def test_blank_zero_and_overtime_only(self):
        self.assertIsNone(decide(replace(self.entry, normal_hours=None, overtime_hours=None), self.policy).status)
        self.assertEqual(decide(replace(self.entry, normal_hours=0, overtime_hours=None), self.policy).status, 'Absent')
        with self.assertRaises(RuleError): decide(replace(self.entry, normal_hours=None), self.policy)
        self.policy['overtime_only_rule'] = 'Record only'
        self.assertEqual(decide(replace(self.entry, normal_hours=None), self.policy).overtime, 2)

    def test_codes_require_explicit_rules(self):
        for code in ('EA', 'AB', 'SB', 'RH', 'HD', 'SL', 'UH', 'MD'):
            entry = replace(self.entry, normal_hours=None, overtime_hours=None, code=code)
            with self.assertRaises(RuleError): decide(entry, self.policy)
            self.policy[code.lower() + '_rule'] = 'Approved Leave'
            self.assertEqual(decide(entry, self.policy).status, 'On Leave')

    def test_half_day_and_thresholds_not_invented(self):
        with self.assertRaises(RuleError): decide(self.entry, {})
        self.policy['half_day_remainder'] = ''
        with self.assertRaises(RuleError): decide(replace(self.entry, normal_hours=Decimal(4)), self.policy)


if __name__ == '__main__':
    unittest.main()
