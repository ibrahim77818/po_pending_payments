# -*- coding: utf-8 -*-
import io
from datetime import date

from odoo import Command, fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import float_compare

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None


@tagged('post_install', '-at_install', 'po_pending_payments')
class TestPoPendingPayments(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls, chart_template_ref=None):
        super().setUpClass(chart_template_ref=chart_template_ref)
        cls.company = cls.company_data['company']
        cls.usd = cls.company.currency_id
        cls.gold = cls.currency_data['currency']  # 1 USD = 3 Gold in 2016, 2 Gold from 2017
        cls.env.user.groups_id |= cls.env.ref('purchase.group_purchase_manager')
        cls.env.user.tz = 'UTC'
        cls.product = cls.env['product.product'].create({
            'name': 'Pending Payments Product',
            'type': 'consu',
            'purchase_method': 'purchase',
            'supplier_taxes_id': [Command.clear()],
            'standard_price': 100.0,
        })
        cls.product_b = cls.env['product.product'].create({
            'name': 'Pending Payments Product B',
            'type': 'consu',
            'purchase_method': 'purchase',
            'supplier_taxes_id': [Command.clear()],
        })
        cls.vendor_a = cls.env['res.partner'].create({'name': 'AAA Vendor', 'is_company': True})
        cls.vendor_a_contact = cls.env['res.partner'].create({
            'name': 'AAA Vendor, Contact', 'parent_id': cls.vendor_a.id})
        cls.vendor_b = cls.env['res.partner'].create({'name': 'BBB Vendor', 'is_company': True})
        cls.bank_journal = cls.company_data['default_journal_bank']

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _po(self, partner, lines, currency=None, date_order='2019-01-10 09:00:00'):
        po = self.env['purchase.order'].create({
            'partner_id': partner.id,
            'currency_id': (currency or self.usd).id,
            'date_order': date_order,
            'order_line': [Command.create({
                'product_id': product.id,
                'product_qty': qty,
                'price_unit': price,
                'taxes_id': [Command.clear()],
                'date_planned': date_order,
            }) for product, qty, price in lines],
        })
        po.button_confirm()
        # button_confirm resets date_order to now: restore the scenario date
        po.date_order = date_order
        return po

    def _bill(self, lines, invoice_date='2019-01-15', due=None, refund=False, post=True, currency=None):
        """lines: [(po_line, qty)]"""
        first = lines[0][0]
        bill = self.env['account.move'].create({
            'move_type': 'in_refund' if refund else 'in_invoice',
            'partner_id': first.order_id.partner_id.id,
            'invoice_date': invoice_date,
            'date': invoice_date,
            'currency_id': (currency or first.order_id.currency_id).id,
            'invoice_line_ids': [Command.create({
                'product_id': pol.product_id.id,
                'quantity': qty,
                'price_unit': pol.price_unit,
                'tax_ids': [Command.clear()],
                'purchase_line_id': pol.id,
            }) for pol, qty in lines],
        })
        if due:
            bill.invoice_date_due = due
        if post:
            bill.action_post()
        return bill

    def _pay(self, bill, amount, pay_date='2019-01-20', currency=None):
        wizard = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=bill.ids).create({
                'amount': amount,
                'payment_date': pay_date,
                'currency_id': (currency or bill.currency_id).id,
                'journal_id': self.bank_journal.id,
            })
        return wizard._create_payments()

    def _wizard(self, **vals):
        vals.setdefault('as_of_date', '2019-03-01')
        return self.env['po.pending.payment.wizard'].create(vals)

    def _row(self, data, order):
        rows = [r for r in data['rows'] if r['order'] == order]
        return rows[0] if rows else None

    def assertAmount(self, value, expected, currency=None, msg=None):
        currency = currency or self.usd
        self.assertEqual(float_compare(value, expected, precision_rounding=currency.rounding), 0,
                         msg or '%s != %s' % (value, expected))

    # ------------------------------------------------------------------
    # data integrity
    # ------------------------------------------------------------------
    def test_01_partial_bill_partial_payment(self):
        po = self._po(self.vendor_a, [(self.product, 10, 100.0)])
        bill = self._bill([(po.order_line, 6)])
        self._pay(bill, 250.0)
        # a draft bill is not a liability: it must not reduce the not-billed amount
        self._bill([(po.order_line, 2)], post=False)

        data = self._wizard(purchase_order_ids=[Command.set(po.ids)])._get_report_data()
        row = self._row(data, po)
        self.assertAmount(row['amount_order'], 1000.0)
        self.assertAmount(row['billed'], 600.0)
        self.assertAmount(row['paid'], 250.0)
        self.assertAmount(row['due'], 350.0)
        self.assertAmount(row['unbilled'], 400.0)
        self.assertAmount(row['due_company'], 350.0)
        self.assertAmount(row['outstanding_company'], 750.0)
        self.assertEqual(len(row['bills']), 1)
        payments = row['bills'][0]['payments']
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]['kind'], 'payment')
        self.assertAmount(payments[0]['amount_currency'], 250.0)
        # totals equal the single row
        self.assertAmount(data['totals']['outstanding_company'], 750.0)
        self.assertEqual(data['order_count'], 1)

    def test_02_vendor_filter_includes_contacts(self):
        po_a = self._po(self.vendor_a, [(self.product, 1, 100.0)])
        po_contact = self._po(self.vendor_a_contact, [(self.product, 2, 100.0)])
        po_b = self._po(self.vendor_b, [(self.product, 3, 100.0)])
        data = self._wizard(partner_ids=[Command.set(self.vendor_a.ids)])._get_report_data()
        orders = {r['order'] for r in data['rows']}
        self.assertIn(po_a, orders)
        self.assertIn(po_contact, orders)
        self.assertNotIn(po_b, orders)
        # both orders are grouped under the commercial vendor
        vendors = [v for v in data['vendors'] if v['partner'] == self.vendor_a]
        self.assertEqual(len(vendors), 1)
        self.assertEqual(len(vendors[0]['rows']), 2)
        self.assertAmount(vendors[0]['totals']['outstanding_company'], 300.0)

    def test_03_purchase_order_filter_and_states(self):
        po1 = self._po(self.vendor_a, [(self.product, 1, 100.0)])
        po2 = self._po(self.vendor_a, [(self.product, 1, 200.0)])
        rfq = self.env['purchase.order'].create({
            'partner_id': self.vendor_a.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_qty': 1, 'price_unit': 50})],
        })
        data = self._wizard(purchase_order_ids=[Command.set(po2.ids)])._get_report_data()
        self.assertEqual([r['order'] for r in data['rows']], [po2])
        all_orders = {r['order'] for r in self._wizard()._get_report_data()['rows']}
        self.assertTrue({po1, po2} <= all_orders)
        self.assertNotIn(rfq, all_orders, 'Unconfirmed RFQs are never reported')

    def test_04_order_date_period(self):
        po_jan = self._po(self.vendor_a, [(self.product, 1, 100.0)], date_order='2019-01-10 09:00:00')
        po_mar = self._po(self.vendor_a, [(self.product, 1, 100.0)], date_order='2019-03-10 09:00:00')
        orders = {r['order'] for r in self._wizard(
            date_from='2019-02-01', date_to='2019-03-31')._get_report_data()['rows']}
        self.assertIn(po_mar, orders)
        self.assertNotIn(po_jan, orders)
        # bounds are inclusive, on the user's local day
        orders = {r['order'] for r in self._wizard(
            date_from='2019-01-10', date_to='2019-01-10')._get_report_data()['rows']}
        self.assertEqual(orders, {po_jan})

    def test_05_bill_date_period(self):
        po = self._po(self.vendor_a, [(self.product, 10, 100.0)])
        self._bill([(po.order_line, 3)], invoice_date='2019-01-15')
        self._bill([(po.order_line, 2)], invoice_date='2019-03-05')
        po_without_bill = self._po(self.vendor_b, [(self.product, 1, 100.0)])
        data = self._wizard(date_type='bill', date_from='2019-03-01', date_to='2019-03-31',
                            as_of_date='2019-04-01')._get_report_data()
        row = self._row(data, po)
        self.assertAmount(row['billed'], 200.0)
        self.assertAmount(row['due'], 200.0)
        self.assertAmount(row['unbilled'], 0.0, msg='Not-billed amounts are excluded on a bill-date period')
        self.assertFalse(self._row(data, po_without_bill))

    def test_06_due_date_thresholds(self):
        po = self._po(self.vendor_a, [(self.product, 10, 100.0)])
        bill_old = self._bill([(po.order_line, 1)], invoice_date='2019-01-15', due='2019-01-31')
        bill_new = self._bill([(po.order_line, 2)], invoice_date='2019-02-15', due='2019-04-15')
        self.assertEqual(bill_old.invoice_date_due, date(2019, 1, 31))
        self.assertEqual(bill_new.invoice_date_due, date(2019, 4, 15))

        def bills(**vals):
            row = self._row(self._wizard(purchase_order_ids=[Command.set(po.ids)], **vals)._get_report_data(), po)
            return row and {b['bill'] for b in row['bills']}, row

        found, row = bills(due_filter='overdue')
        self.assertEqual(found, {bill_old})
        self.assertAmount(row['due'], 100.0)
        self.assertAmount(row['unbilled'], 0.0)
        self.assertEqual(row['max_days_overdue'], 29)  # 2019-01-31 -> 2019-03-01
        self.assertAmount(row['overdue_company'], 100.0)

        found, _row = bills(due_filter='not_due')
        self.assertEqual(found, {bill_new})
        found, _row = bills(due_filter='due_within', due_days=30)
        self.assertFalse(found, 'Nothing is due between 2019-03-01 and 2019-03-31')
        found, _row = bills(due_filter='due_within', due_days=45)
        self.assertEqual(found, {bill_new})
        found, _row = bills(due_filter='due_range', due_date_from='2019-01-01', due_date_to='2019-02-28')
        self.assertEqual(found, {bill_old})

        found, row = bills(due_filter='all')
        self.assertEqual(found, {bill_old, bill_new})
        self.assertAmount(row['unbilled'], 700.0)
        self.assertEqual(row['next_due_date'], date(2019, 1, 31))

    def test_07_foreign_currency(self):
        # Order and bill in 2016 (1 USD = 3 Gold); partial payment in 2017 (1 USD = 2 Gold)
        po = self._po(self.vendor_a, [(self.product, 10, 30.0)], currency=self.gold,
                      date_order='2016-06-01 09:00:00')
        bill = self._bill([(po.order_line, 10)], invoice_date='2016-06-10')
        self._pay(bill, 150.0, pay_date='2017-02-01')
        po_open = self._po(self.vendor_a, [(self.product, 4, 50.0)], currency=self.gold,
                           date_order='2016-06-01 09:00:00')

        data = self._wizard(partner_ids=[Command.set(self.vendor_a.ids)],
                            as_of_date='2017-06-01')._get_report_data()
        row = self._row(data, po)
        payable = bill.line_ids.filtered(lambda l: l.account_id.account_type == 'liability_payable')
        # document currency
        self.assertAmount(row['billed'], 300.0, self.gold)
        self.assertAmount(row['paid'], 150.0, self.gold)
        self.assertAmount(row['due'], 150.0, self.gold)
        # company currency comes straight from the journal items
        self.assertAmount(row['billed_company'], 100.0)
        self.assertAmount(row['billed_company'], -sum(payable.mapped('balance')))
        self.assertAmount(row['due_company'], -sum(payable.mapped('amount_residual')))
        self.assertAmount(row['paid_company'], row['billed_company'] - row['due_company'])
        self.assertAmount(row['amount_order_company'], 100.0, msg='PO converted at its order-date rate')

        open_row = self._row(data, po_open)
        self.assertAmount(open_row['unbilled'], 200.0, self.gold)
        self.assertAmount(open_row['unbilled_company'], 100.0, msg='Not billed converted at the as-of rate')

        gold_summary = [c for c in data['currencies'] if c['currency'] == self.gold][0]
        self.assertEqual(gold_summary['count'], 2)
        self.assertAmount(gold_summary['due'], 150.0, self.gold)
        self.assertAmount(gold_summary['unbilled'], 200.0, self.gold)
        self.assertAmount(data['totals']['outstanding_company'],
                          row['outstanding_company'] + open_row['outstanding_company'])

    def test_08_bill_spanning_two_orders_is_allocated(self):
        po1 = self._po(self.vendor_a, [(self.product, 3, 100.0)])
        po2 = self._po(self.vendor_a, [(self.product_b, 1, 100.0)])
        bill = self._bill([(po1.order_line, 3), (po2.order_line, 1)])
        self._pay(bill, 200.0)
        data = self._wizard(partner_ids=[Command.set(self.vendor_a.ids)])._get_report_data()
        r1, r2 = self._row(data, po1), self._row(data, po2)
        self.assertAmount(r1['bills'][0]['ratio'], 0.75)
        self.assertAmount(r1['billed'], 300.0)
        self.assertAmount(r1['paid'], 150.0)
        self.assertAmount(r1['due'], 150.0)
        self.assertAmount(r2['billed'], 100.0)
        self.assertAmount(r2['paid'], 50.0)
        self.assertAmount(r2['due'], 50.0)
        # the split never creates or loses money
        self.assertAmount(r1['due_company'] + r2['due_company'], bill.amount_residual)
        self.assertAmount(r1['billed_company'] + r2['billed_company'], bill.amount_total)

    def test_09_credit_note_reduces_billed_and_due(self):
        po = self._po(self.vendor_a, [(self.product, 10, 100.0)])
        bill = self._bill([(po.order_line, 10)])
        refund = self._bill([(po.order_line, 2)], refund=True, invoice_date='2019-01-20')
        row = self._row(self._wizard(purchase_order_ids=[Command.set(po.ids)])._get_report_data(), po)
        self.assertAmount(row['billed'], 800.0)
        self.assertAmount(row['due'], 800.0)
        self.assertAmount(row['unbilled'], 200.0, msg='Refunded quantities become billable again, as in Odoo')

        # reconcile the credit note with the bill: due stays 800, the allocation is traced
        (bill + refund).line_ids.filtered(lambda l: l.account_id.account_type == 'liability_payable').reconcile()
        row = self._row(self._wizard(purchase_order_ids=[Command.set(po.ids)])._get_report_data(), po)
        self.assertAmount(row['due'], 800.0)
        bill_vals = [b for b in row['bills'] if b['bill'] == bill][0]
        self.assertEqual(bill_vals['payments'][0]['kind'], 'credit_note')
        self.assertAmount(bill_vals['payments'][0]['amount_currency'], 200.0)

    def test_10_settled_orders_and_scope(self):
        po_paid = self._po(self.vendor_a, [(self.product, 1, 100.0)])
        bill = self._bill([(po_paid.order_line, 1)])
        self._pay(bill, 100.0)
        po_unbilled = self._po(self.vendor_b, [(self.product, 1, 100.0)])

        data = self._wizard()._get_report_data()
        self.assertFalse(self._row(data, po_paid), 'Settled orders are hidden by default')
        self.assertTrue(self._row(data, po_unbilled))

        row = self._row(self._wizard(include_settled=True)._get_report_data(), po_paid)
        self.assertAmount(row['outstanding_company'], 0.0)

        data = self._wizard(pending_scope='billed')._get_report_data()
        self.assertFalse(self._row(data, po_unbilled), 'Bills-only scope ignores amounts not yet billed')
        self.assertFalse(data['unbilled_counted'])

    def test_11_validation(self):
        with self.assertRaises(ValidationError):
            self._wizard(date_from='2019-02-01', date_to='2019-01-01')
        with self.assertRaises(ValidationError):
            self._wizard(due_filter='due_range')
        with self.assertRaises(UserError):
            self._wizard(partner_ids=[Command.set(self.vendor_b.ids)]).action_print_pdf()

    def test_12_access_rights(self):
        user = self.env['res.users'].create({
            'name': 'Purchase User', 'login': 'ppp_purchase_user',
            'groups_id': [Command.set([self.env.ref('purchase.group_purchase_user').id])],
            'company_id': self.company.id, 'company_ids': [Command.set(self.company.ids)],
        })
        with self.assertRaises(AccessError):
            self.env['po.pending.payment.wizard'].with_user(user).create({})

    # ------------------------------------------------------------------
    # outputs
    # ------------------------------------------------------------------
    def _scenario(self):
        po1 = self._po(self.vendor_a, [(self.product, 10, 100.0)])
        bill = self._bill([(po1.order_line, 6)], due='2019-02-01')
        self._pay(bill, 250.0)
        po2 = self._po(self.vendor_b, [(self.product, 4, 30.0)], currency=self.gold,
                       date_order='2016-06-01 09:00:00')
        self._bill([(po2.order_line, 4)], invoice_date='2016-06-10', due='2016-07-10')
        return po1, po2

    def test_13_screen_view(self):
        po1, po2 = self._scenario()
        wizard = self._wizard(partner_ids=[Command.set((self.vendor_a + self.vendor_b).ids)])
        data = wizard._get_report_data()
        action = wizard.action_view()
        self.assertEqual(action['res_model'], 'po.pending.payment.line')
        lines = self.env['po.pending.payment.line'].search(action['domain'])
        self.assertEqual(set(lines.order_id.ids), {po1.id, po2.id})
        self.assertAmount(sum(lines.mapped('outstanding_company')), data['totals']['outstanding_company'])
        line1 = lines.filtered(lambda l: l.order_id == po1)
        self.assertEqual(line1.status, 'overdue')
        self.assertEqual(line1.bill_count, 1)
        self.assertIn('Payment', line1.bill_line_ids.payment_summary)
        # running the view again replaces the previous result
        wizard.action_view()
        self.assertEqual(len(wizard.line_ids), 2)
        self.assertEqual(line1.action_open_bills()['res_id'], line1.bill_ids.id)

    def test_14_pdf_report(self):
        po1, po2 = self._scenario()
        wizard = self._wizard(partner_ids=[Command.set((self.vendor_a + self.vendor_b).ids)])
        action = wizard.with_context(discard_logo_check=True).action_print_pdf()
        self.assertEqual(action['report_name'], 'po_pending_payments.report_pending_payments')
        html = self.env['ir.actions.report']._render_qweb_html(
            'po_pending_payments.action_report_po_pending_payments', wizard.ids)[0].decode()
        for needle in (po1.name, po2.name, 'AAA Vendor', 'BBB Vendor', 'Currency Breakdown', 'ppp-ltr'):
            self.assertIn(needle, html)
        self.assertIn(po1.order_line.invoice_lines.move_id.name, html, 'Bills are printed when details are on')
        wizard.show_details = False
        html = self.env['ir.actions.report']._render_qweb_html(
            'po_pending_payments.action_report_po_pending_payments', wizard.ids)[0].decode()
        self.assertNotIn('ppp-bill', html)

    def test_15_excel_export(self):
        if openpyxl is None:
            self.skipTest('openpyxl not installed')
        po1, po2 = self._scenario()
        wizard = self._wizard(partner_ids=[Command.set((self.vendor_a + self.vendor_b).ids)])
        data = wizard._get_report_data()
        self.assertEqual(wizard.action_export_xlsx()['url'], '/po_pending_payments/xlsx/%s' % wizard.id)
        book = openpyxl.load_workbook(io.BytesIO(wizard._generate_xlsx()))
        self.assertEqual(book.sheetnames, ['Pending Payments', 'Bills & Payments', 'Currency Summary'])
        ws = book['Pending Payments']
        values = [[c.value for c in row] for row in ws.iter_rows()]
        flat = [v for row in values for v in row if v is not None]
        self.assertIn(po1.name, flat)
        self.assertIn(po2.name, flat)
        # two vendor subtotals + one grand total, all SUBTOTAL formulas
        formulas = [v for v in flat if isinstance(v, str) and v.startswith('=SUBTOTAL(9,')]
        self.assertEqual(len(formulas), 3 * 6)
        grand_row = [row for row in values if row[0] and str(row[0]).startswith('Grand Total')][0]
        self.assertTrue(str(grand_row[13]).startswith('=SUBTOTAL(9,N'))
        # the rows the formulas add up equal the engine's grand total
        po_rows = [row for row in values if row[1] in (po1.name, po2.name)]
        self.assertAmount(sum(row[13] for row in po_rows), data['totals']['outstanding_company'])
        self.assertAmount(sum(row[11] for row in po_rows), data['totals']['due_company'])
        details = book['Bills & Payments']
        names = [c.value for c in details['C'] if c.value]
        self.assertIn(po1.order_line.invoice_lines.move_id.name, names)

    def test_16_arabic_rtl_and_translations(self):
        self.env['res.lang']._activate_lang('ar_001')
        self.env['ir.module.module']._load_module_terms(['po_pending_payments'], ['ar_001'], overwrite=True)
        self._scenario()
        wizard = self._wizard().with_context(lang='ar_001')
        self.assertTrue(wizard._is_rtl())
        html = self.env['ir.actions.report'].with_context(lang='ar_001')._render_qweb_html(
            'po_pending_payments.action_report_po_pending_payments', wizard.ids)[0].decode()
        self.assertIn('dir="rtl"', html)
        self.assertIn('ppp-rtl', html)
        self.assertIn('تقرير الدفعات المعلقة', html, 'Report title is translated')
        if openpyxl:
            book = openpyxl.load_workbook(io.BytesIO(wizard._generate_xlsx()))
            ws = book.worksheets[0]
            self.assertTrue(ws.sheet_view.rightToLeft)
            self.assertEqual(ws.title, 'الدفعات المعلقة')
        selection = dict(wizard._fields['due_filter']._description_selection(wizard.env))
        self.assertEqual(selection['overdue'], 'المتأخرة فقط')

    def test_17_bidi_isolation(self):
        from odoo.addons.po_pending_payments.tools.bidi import bidi_chunks, bidi_html
        # English text is printed untouched
        self.assertEqual(str(bidi_html('Gulf Steel Trading & Co')), 'Gulf Steel Trading &amp; Co')
        # mixed sequence numbers are split into directional pieces, punctuation isolated
        self.assertEqual(bidi_chunks('فاتور/2026/08/0001'), [
            ['فاتور', 'rtl', True], ['/', None, True], ['2026/08/0001', 'ltr', False]])
        # an amount with an Arabic currency symbol keeps "ر.س" in one right-to-left piece
        self.assertEqual(bidi_chunks('1,250.00 ر.س'), [['1,250.00', 'ltr', False], ['ر.س', 'rtl', False]])
        # consecutive words of the same direction stay together
        self.assertEqual(bidi_chunks('المبالغ بعملة USD ما لم يُذكر خلاف ذلك'), [
            ['المبالغ بعملة', 'rtl', False], ['USD', 'ltr', False], ['ما لم يُذكر خلاف ذلك', 'rtl', False]])
        html = str(bidi_html('مؤسسة <النور>'))
        self.assertNotIn('<النور>', html, 'content is escaped')
        self.assertIn('dir="rtl"', html)
