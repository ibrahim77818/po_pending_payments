# -*- coding: utf-8 -*-
import io
from collections import OrderedDict, defaultdict
from datetime import datetime, time, timedelta

import pytz
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_is_zero, format_date
from odoo.tools.misc import formatLang

from ..tools.bidi import bidi_html

try:
    import xlsxwriter
    from xlsxwriter.utility import xl_range
except ImportError:  # pragma: no cover - xlsxwriter is an Odoo requirement
    xlsxwriter = xl_range = None

PAYABLE = 'liability_payable'
BILL_TYPES = ('in_invoice', 'in_refund')


class PoPendingPaymentWizard(models.TransientModel):
    _name = 'po.pending.payment.wizard'
    _description = 'Purchase Pending Payments Report Wizard'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True, readonly=True,
        default=lambda self: self.env.company)
    company_currency_id = fields.Many2one(related='company_id.currency_id')

    partner_ids = fields.Many2many(
        'res.partner', 'po_pp_wizard_partner_rel', 'wizard_id', 'partner_id',
        string='Vendors',
        help='Leave empty to include all vendors. Contacts of a selected '
             'company are included automatically.')
    purchase_order_ids = fields.Many2many(
        'purchase.order', 'po_pp_wizard_order_rel', 'wizard_id', 'order_id',
        string='Purchase Orders',
        help='Leave empty to include every confirmed purchase order matching the other filters.')

    date_type = fields.Selection([
        ('order', 'Purchase Order Date'),
        ('bill', 'Vendor Bill Date'),
    ], string='Period Based On', required=True, default='order',
        help='Purchase Order Date: filters orders by their order date and includes amounts not yet billed.\n'
             'Vendor Bill Date: only counts bills dated within the period; amounts not yet billed are excluded.')
    date_from = fields.Date(string='From')
    date_to = fields.Date(string='To')

    due_filter = fields.Selection([
        ('all', 'All Due Dates'),
        ('overdue', 'Overdue Only'),
        ('not_due', 'Not Yet Due'),
        ('due_within', 'Due Within Next N Days'),
        ('due_range', 'Due Date Range'),
    ], string='Due Date Threshold', required=True, default='all',
        help='Applied to vendor bills. Any value other than "All Due Dates" excludes '
             'amounts not yet billed, since they have no due date.')
    due_days = fields.Integer(string='Days', default=30)
    due_date_from = fields.Date(string='Due From')
    due_date_to = fields.Date(string='Due To')
    as_of_date = fields.Date(
        string='As of Date', required=True, default=fields.Date.context_today,
        help='Reference date for overdue days and for converting amounts not yet billed '
             'into the company currency.')

    pending_scope = fields.Selection([
        ('all', 'Bills Due + Not Yet Billed'),
        ('billed', 'Bills Due Only'),
    ], string='Pending Amounts', required=True, default='all')
    include_settled = fields.Boolean(
        string='Include Settled Orders',
        help='Also list purchase orders with nothing due and nothing left to bill.')
    show_details = fields.Boolean(
        string='Show Bills & Payments', default=True,
        help='Print each vendor bill and the payments, credit notes and exchange '
             'differences reconciled with it.')

    line_ids = fields.One2many('po.pending.payment.line', 'wizard_id', string='Result Lines')

    # ------------------------------------------------------------------
    # Constraints / onchanges
    # ------------------------------------------------------------------
    @api.constrains('date_from', 'date_to', 'due_date_from', 'due_date_to', 'due_days', 'due_filter')
    def _check_dates(self):
        for wiz in self:
            if wiz.date_from and wiz.date_to and wiz.date_from > wiz.date_to:
                raise ValidationError(_('The start date must be before the end date.'))
            if wiz.due_date_from and wiz.due_date_to and wiz.due_date_from > wiz.due_date_to:
                raise ValidationError(_('The "Due From" date must be before the "Due To" date.'))
            if wiz.due_filter == 'due_within' and wiz.due_days < 0:
                raise ValidationError(_('The number of days must be zero or positive.'))
            if wiz.due_filter == 'due_range' and not (wiz.due_date_from or wiz.due_date_to):
                raise ValidationError(_('Set at least one bound for the due date range.'))

    @api.onchange('partner_ids')
    def _onchange_partner_ids(self):
        if self.partner_ids and self.purchase_order_ids:
            commercial = self.partner_ids.commercial_partner_id
            self.purchase_order_ids = self.purchase_order_ids.filtered(
                lambda po: po.partner_id.commercial_partner_id in commercial)

    # ------------------------------------------------------------------
    # Data engine
    # ------------------------------------------------------------------
    def _user_tz(self):
        return pytz.timezone(self.env.user.tz or self.env.context.get('tz') or 'UTC')

    def _date_to_utc(self, value, end=False):
        """Convert a date to the naive UTC datetime bounding it in the user's timezone."""
        local = self._user_tz().localize(datetime.combine(value, time.max if end else time.min))
        return local.astimezone(pytz.utc).replace(tzinfo=None)

    def _get_order_domain(self):
        self.ensure_one()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('state', 'in', ('purchase', 'done')),
        ]
        if self.partner_ids:
            domain.append(('partner_id.commercial_partner_id', 'in',
                           self.partner_ids.commercial_partner_id.ids))
        if self.purchase_order_ids:
            domain.append(('id', 'in', self.purchase_order_ids.ids))
        if self.date_type == 'order':
            if self.date_from:
                domain.append(('date_order', '>=', self._date_to_utc(self.date_from)))
            if self.date_to:
                domain.append(('date_order', '<=', self._date_to_utc(self.date_to, end=True)))
        return domain

    def _get_orders(self):
        return self.env['purchase.order'].search(
            self._get_order_domain(), order='partner_id, date_order, id')

    def _bill_matches_filters(self, bill):
        """Period (bill date) and due-date threshold filters, applied per bill."""
        self.ensure_one()
        if self.date_type == 'bill':
            bill_date = bill.invoice_date or bill.date
            if self.date_from and bill_date < self.date_from:
                return False
            if self.date_to and bill_date > self.date_to:
                return False
        due = bill.invoice_date_due or bill.invoice_date or bill.date
        as_of = self.as_of_date
        if self.due_filter == 'overdue':
            return due < as_of
        if self.due_filter == 'not_due':
            return due >= as_of
        if self.due_filter == 'due_within':
            return as_of <= due <= as_of + timedelta(days=self.due_days)
        if self.due_filter == 'due_range':
            if self.due_date_from and due < self.due_date_from:
                return False
            if self.due_date_to and due > self.due_date_to:
                return False
        return True

    def _unbilled_counted(self):
        self.ensure_one()
        return self.pending_scope == 'all' and self.date_type == 'order' and self.due_filter == 'all'

    def _get_bill_values(self, bill, exchange_moves):
        """Amounts of one posted vendor bill / refund, read from its payable journal items.

        Signs: bills are positive, refunds negative. Company-currency amounts come from the
        journal items (balance / amount_residual), so they carry the bill-date rate and every
        partial reconciliation exactly as booked in accounting.
        """
        company_currency = bill.company_id.currency_id
        payable = bill.line_ids.filtered(lambda l: l.account_id.account_type == PAYABLE)
        total_cur = -sum(payable.mapped('amount_currency'))
        residual_cur = -sum(payable.mapped('amount_residual_currency'))
        total_comp = -sum(payable.mapped('balance'))
        residual_comp = -sum(payable.mapped('amount_residual'))

        allocations = []
        for line in payable:
            for partial in line.matched_debit_ids:  # bill payable on the credit side
                allocations.append((partial, partial.debit_move_id, partial.credit_amount_currency, 1))
            for partial in line.matched_credit_ids:  # refund payable on the debit side
                allocations.append((partial, partial.credit_move_id, partial.debit_amount_currency, -1))
        payments = []
        for partial, counterpart, amount_cur, sign in allocations:
            move = counterpart.move_id
            if move in exchange_moves or move.id in exchange_moves.ids:
                kind = 'exchange'
            elif move.origin_payment_id or counterpart.payment_id:
                kind = 'payment'
            elif move.move_type in BILL_TYPES:
                kind = 'credit_note' if move.move_type == 'in_refund' else 'bill'
            else:
                kind = 'entry'
            payments.append({
                'move': move,
                'name': move.name,
                'ref': move.ref or '',
                'date': counterpart.date,
                'journal': move.journal_id.display_name,
                'kind': kind,
                'amount_currency': sign * amount_cur,
                'amount_company': sign * partial.amount,
            })
        # Odoo 18: a payment registered on a journal without an outstanding account gets no journal
        # entry and stays unreconciled with the bill until the bank statement is matched. It is
        # listed for traceability but not counted as paid: the liability is still open in the books.
        sign = -1 if bill.move_type == 'in_refund' else 1
        unposted = bill.matched_payment_ids.filtered(
            lambda p: not p.move_id and p.state in ('in_process', 'paid'))
        for pay in unposted:
            amount_company = abs(pay.amount_company_currency_signed)
            if pay.currency_id == bill.currency_id:
                amount_cur = pay.amount
            else:
                amount_cur = company_currency._convert(amount_company, bill.currency_id, bill.company_id, pay.date)
            payments.append({
                'move': pay,
                'name': pay.name,
                'ref': pay.memo or '',
                'date': pay.date,
                'journal': pay.journal_id.display_name,
                'kind': 'unposted',
                'amount_currency': sign * amount_cur,
                'amount_company': sign * amount_company,
            })
        payments.sort(key=lambda p: (p['date'] or fields.Date.today(), p['name'] or ''))

        due_date = bill.invoice_date_due or bill.invoice_date or bill.date
        days_overdue = 0
        if not float_is_zero(residual_comp, precision_rounding=company_currency.rounding) \
                and due_date < self.as_of_date:
            days_overdue = (self.as_of_date - due_date).days
        return {
            'bill': bill,
            'name': bill.name,
            'ref': bill.ref or '',
            'move_type': bill.move_type,
            'date': bill.invoice_date or bill.date,
            'date_due': due_date,
            'days_overdue': days_overdue,
            'currency': bill.currency_id,
            'total_currency': total_cur,
            'paid_currency': total_cur - residual_cur,
            'residual_currency': residual_cur,
            'total_company': total_comp,
            'paid_company': total_comp - residual_comp,
            'residual_company': residual_comp,
            'payment_state': bill.payment_state,
            'payments': payments,
        }

    def _get_bill_allocations(self, orders):
        """Map each order id to [(bill, ratio)] for posted bills linked through bill lines.

        A bill that covers several purchase orders is split between them in proportion to the
        bill-line totals linked to each order, so the ratios of one bill always sum to 1.
        """
        lines = self.env['account.move.line'].search([
            ('purchase_line_id.order_id', 'in', orders.ids),
            ('move_id.state', '=', 'posted'),
            ('move_id.move_type', 'in', BILL_TYPES),
            ('move_id.company_id', '=', self.company_id.id),
        ])
        shares = defaultdict(lambda: defaultdict(float))
        for line in lines:
            shares[line.move_id][line.purchase_line_id.order_id.id] += abs(line.price_total)
        allocations = defaultdict(list)
        for bill, per_order in shares.items():
            base = sum(per_order.values())
            for order_id, share in per_order.items():
                ratio = share / base if base else 1.0 / len(per_order)
                allocations[order_id].append((bill, ratio))
        return allocations, lines

    def _get_unbilled_amount(self, order, posted_bill_lines):
        """Order amount (tax included, order currency) for quantities not covered by posted bills.

        Draft bills do not reduce this amount: they are not a liability yet.
        """
        billed_qty = defaultdict(float)
        for line in posted_bill_lines:
            po_line = line.purchase_line_id
            if po_line.order_id != order:
                continue
            qty = line.product_uom_id._compute_quantity(line.quantity, po_line.product_uom) \
                if line.product_uom_id and po_line.product_uom else line.quantity
            billed_qty[po_line.id] += qty if line.move_id.move_type == 'in_invoice' else -qty
        amount = 0.0
        for po_line in order.order_line.filtered(lambda l: not l.display_type):
            if float_is_zero(po_line.product_qty, precision_rounding=po_line.product_uom.rounding or 0.01):
                continue
            remaining = max(po_line.product_qty - billed_qty[po_line.id], 0.0)
            amount += po_line.price_total * remaining / po_line.product_qty
        return order.currency_id.round(amount)

    def _get_report_data(self):
        """Single source of truth for the screen, PDF and Excel outputs."""
        self.ensure_one()
        company = self.company_id
        ccy = company.currency_id
        as_of = self.as_of_date
        orders = self._get_orders()
        allocations, bill_lines = self._get_bill_allocations(orders)
        lines_by_order = defaultdict(lambda: self.env['account.move.line'])
        for line in bill_lines:
            lines_by_order[line.purchase_line_id.order_id.id] |= line

        all_bills = self.env['account.move'].browse(
            {bill.id for allocs in allocations.values() for bill, _r in allocs})
        exchange_moves = self.env['account.partial.reconcile'].search([
            '|',
            ('debit_move_id.move_id', 'in', all_bills.ids),
            ('credit_move_id.move_id', 'in', all_bills.ids),
        ]).exchange_move_id
        bill_cache = {}
        count_unbilled = self._unbilled_counted()
        bill_filter_active = self.date_type == 'bill' or self.due_filter != 'all'

        rows = []
        for order in orders:
            po_ccy = order.currency_id
            order_date = fields.Datetime.context_timestamp(self, order.date_order).date() \
                if order.date_order else as_of
            bills = []
            sums = defaultdict(float)
            for bill, ratio in sorted(allocations.get(order.id, []),
                                      key=lambda a: (a[0].invoice_date or a[0].date, a[0].id)):
                if not self._bill_matches_filters(bill):
                    continue
                if bill.id not in bill_cache:
                    bill_cache[bill.id] = self._get_bill_values(bill, exchange_moves)
                vals = dict(bill_cache[bill.id], ratio=ratio)
                for key in ('total', 'paid', 'residual'):
                    comp = ccy.round(vals['%s_company' % key] * ratio)
                    vals['%s_company_alloc' % key] = comp
                    if vals['currency'] == po_ccy:
                        doc = po_ccy.round(vals['%s_currency' % key] * ratio)
                    else:
                        doc = ccy._convert(comp, po_ccy, company, as_of)
                    vals['%s_order_alloc' % key] = doc
                    sums['%s_company' % key] += comp
                    sums['%s_order' % key] += doc
                vals['overdue_company_alloc'] = vals['residual_company_alloc'] if vals['days_overdue'] else 0.0
                sums['overdue_company'] += vals['overdue_company_alloc']
                bills.append(vals)

            if bill_filter_active and not bills:
                continue

            unbilled = self._get_unbilled_amount(order, lines_by_order[order.id]) if count_unbilled else 0.0
            unbilled_comp = po_ccy._convert(unbilled, ccy, company, as_of) if unbilled else 0.0
            due_comp = ccy.round(sums['residual_company'])
            outstanding = ccy.round(due_comp + unbilled_comp)
            is_pending = not float_is_zero(due_comp, precision_rounding=ccy.rounding) \
                or not float_is_zero(unbilled_comp, precision_rounding=ccy.rounding)
            if not is_pending and not self.include_settled:
                continue
            due_dates = [b['date_due'] for b in bills
                         if not float_is_zero(b['residual_company_alloc'], precision_rounding=ccy.rounding)]
            rows.append({
                'order': order,
                'name': order.name,
                'partner': order.partner_id.commercial_partner_id,
                'date_order': order_date,
                'currency': po_ccy,
                'amount_order': order.amount_total,
                'amount_order_company': po_ccy._convert(order.amount_total, ccy, company, order_date),
                'billed': po_ccy.round(sums['total_order']),
                'paid': po_ccy.round(sums['paid_order']),
                'due': po_ccy.round(sums['residual_order']),
                'unbilled': unbilled,
                'billed_company': ccy.round(sums['total_company']),
                'paid_company': ccy.round(sums['paid_company']),
                'due_company': due_comp,
                'unbilled_company': unbilled_comp,
                'outstanding_company': outstanding,
                'overdue_company': ccy.round(sums['overdue_company']),
                'max_days_overdue': max([b['days_overdue'] for b in bills] or [0]),
                'next_due_date': min(due_dates) if due_dates else False,
                'bills': bills,
            })

        return self._assemble(rows)

    def _assemble(self, rows):
        ccy = self.company_id.currency_id
        total_keys = ('amount_order_company', 'billed_company', 'paid_company', 'due_company',
                      'unbilled_company', 'outstanding_company', 'overdue_company')
        vendors = OrderedDict()
        for row in sorted(rows, key=lambda r: ((r['partner'].name or '').lower(), r['partner'].id,
                                               r['date_order'], r['name'])):
            vendor = vendors.setdefault(row['partner'].id, {
                'partner': row['partner'], 'rows': [],
                'totals': dict.fromkeys(total_keys, 0.0)})
            vendor['rows'].append(row)
            for key in total_keys:
                vendor['totals'][key] += row[key]
        grand = dict.fromkeys(total_keys, 0.0)
        for vendor in vendors.values():
            for key in total_keys:
                vendor['totals'][key] = ccy.round(vendor['totals'][key])
                grand[key] += vendor['totals'][key]
        grand = {k: ccy.round(v) for k, v in grand.items()}

        by_currency = OrderedDict()
        for row in rows:
            cur = row['currency']
            entry = by_currency.setdefault(cur.id, {
                'currency': cur, 'count': 0,
                **dict.fromkeys(('amount_order', 'billed', 'paid', 'due', 'unbilled'), 0.0),
                **dict.fromkeys(('due_company', 'unbilled_company', 'outstanding_company'), 0.0)})
            entry['count'] += 1
            for key in ('amount_order', 'billed', 'paid', 'due', 'unbilled'):
                entry[key] = cur.round(entry[key] + row[key])
            for key in ('due_company', 'unbilled_company', 'outstanding_company'):
                entry[key] = ccy.round(entry[key] + row[key])
        currencies = sorted(by_currency.values(), key=lambda e: (e['currency'] != ccy, e['currency'].name))

        return {
            'wizard': self,
            'company': self.company_id,
            'company_currency': ccy,
            'as_of_date': self.as_of_date,
            'filters': self._get_filter_summary(),
            'vendors': list(vendors.values()),
            'rows': rows,
            'currencies': currencies,
            'totals': grand,
            'order_count': len(rows),
            'bill_count': len({b['bill'].id for r in rows for b in r['bills']}),
            'show_details': self.show_details,
            'unbilled_counted': self._unbilled_counted(),
            'is_rtl': self._is_rtl(),
            'labels': {
                'currency_note': _('Amounts in %s unless stated') % ccy.name,
                'count_note': _('%(orders)s orders · %(bills)s bills') % {
                    'orders': len(rows), 'bills': len({b['bill'].id for r in rows for b in r['bills']})},
                'subtotal': _('Subtotal (%s)') % ccy.name,
                'outstanding_ccy': _('Outstanding (%s)') % ccy.name,
                'due_ccy': _('Due (%s)') % ccy.name,
                'unbilled_ccy': _('Not Billed (%s)') % ccy.name,
                'alloc': _('%.1f%% of bill'),
                'note': _('Company-currency amounts of bills and payments come from the posted journal items '
                          '(rates of their accounting dates). Amounts not yet billed are converted at the rate '
                          'of the "As of" date. A bill covering several purchase orders is split in proportion '
                          'to its lines.'),
            },
        }

    def _is_rtl(self):
        lang = self.env['res.lang']._lang_get(self.env.lang or self.env.user.lang or 'en_US')
        return bool(lang) and lang.direction == 'rtl'

    def _selection_label(self, field_name):
        return dict(self._fields[field_name]._description_selection(self.env)).get(self[field_name], '')

    def _fmt_date(self, value):
        return format_date(self.env, value) if value else ''

    # PDF helpers: every printed text goes through bidi_html (see tools/bidi.py)
    def _bidi(self, text):
        return bidi_html(text)

    def _bidi_amount(self, value, currency):
        return Markup('<span class="ppp-bn">%s</span>') % bidi_html(
            formatLang(self.env, value or 0.0, currency_obj=currency))

    def _bidi_date(self, value):
        if not value:
            return Markup('')
        return Markup('<span class="ppp-bn">%s</span>') % bidi_html(self._fmt_date(value))

    def _get_filter_summary(self):
        """List of (label, value) pairs describing the active filters, translated."""
        self.ensure_one()
        summary = [(_('Vendors'), ', '.join(self.partner_ids.mapped('display_name')) or _('All'))]
        if self.purchase_order_ids:
            summary.append((_('Purchase Orders'), ', '.join(self.purchase_order_ids.mapped('name'))))
        if self.date_from or self.date_to:
            period = '%s → %s' % (self._fmt_date(self.date_from) or '…', self._fmt_date(self.date_to) or '…')
            summary.append((self._selection_label('date_type'), period))
        if self.due_filter != 'all':
            label = self._selection_label('due_filter')
            if self.due_filter == 'due_within':
                label = _('Due within %s days') % self.due_days
            elif self.due_filter == 'due_range':
                label = '%s: %s → %s' % (label, self._fmt_date(self.due_date_from) or '…',
                                         self._fmt_date(self.due_date_to) or '…')
            summary.append((_('Due Date Threshold'), label))
        summary.append((_('Pending Amounts'), self._selection_label('pending_scope')))
        summary.append((_('As of Date'), self._fmt_date(self.as_of_date)))
        return summary

    def _payment_kind_label(self, kind):
        return {
            'payment': _('Payment'),
            'credit_note': _('Credit Note'),
            'bill': _('Vendor Bill'),
            'exchange': _('Exchange Difference'),
            'entry': _('Journal Entry'),
            'unposted': _('Payment not yet in accounting'),
        }.get(kind, kind)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _check_has_data(self, data):
        if not data['rows']:
            raise UserError(_('No pending payments match the selected filters.'))

    def action_view(self):
        self.ensure_one()
        data = self._get_report_data()
        self.line_ids.unlink()
        Line = self.env['po.pending.payment.line']
        for row in data['rows']:
            Line.create(Line._prepare_from_row(self, row))
        action = self.env['ir.actions.actions']._for_xml_id('po_pending_payments.action_po_pending_payment_line')
        action['domain'] = [('wizard_id', '=', self.id)]
        action['context'] = {'search_default_group_vendor': 1, 'po_pp_wizard_id': self.id}
        action['display_name'] = _('Pending Payments (as of %s)') % self._fmt_date(self.as_of_date)
        action['target'] = 'main'  # fresh breadcrumb: the result replaces whatever screen launched the wizard
        return action

    def action_print_pdf(self):
        self.ensure_one()
        self._check_has_data(self._get_report_data())
        return self.env.ref('po_pending_payments.action_report_po_pending_payments').report_action(self)

    def action_export_xlsx(self):
        self.ensure_one()
        self._check_has_data(self._get_report_data())
        return {
            'type': 'ir.actions.act_url',
            'url': '/po_pending_payments/xlsx/%s' % self.id,
            'target': 'self',
        }

    # ------------------------------------------------------------------
    # Excel
    # ------------------------------------------------------------------
    def _xlsx_filename(self):
        return '%s_%s.xlsx' % (_('Pending_Payments'), fields.Date.to_string(self.as_of_date))

    def _generate_xlsx(self):
        """Build the Excel workbook and return it as bytes."""
        self.ensure_one()
        if xlsxwriter is None:
            raise UserError(_('The Python library xlsxwriter is required for the Excel export.'))
        data = self._get_report_data()
        ccy = data['company_currency']
        rtl = data['is_rtl']
        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True, 'strings_to_formulas': False})

        def money_fmt(currency, **extra):
            decimals = max(currency.decimal_places, 0)
            pattern = '#,##0' + ('.' + '0' * decimals if decimals else '')
            return wb.add_format(dict({'num_format': '%s;[Red]-%s;"-"' % (pattern, pattern), 'border': 1,
                                       'border_color': '#D9DEE7'}, **extra))

        base = {'font_name': 'Arial', 'font_size': 10}
        align = 'right' if rtl else 'left'
        f_title = wb.add_format(dict(base, bold=True, font_size=16, font_color='#1F3A5F'))
        f_sub = wb.add_format(dict(base, font_color='#5B6B7F'))
        f_flabel = wb.add_format(dict(base, bold=True, font_color='#1F3A5F'))
        f_head = wb.add_format(dict(base, bold=True, font_color='#FFFFFF', bg_color='#1F3A5F', border=1,
                                    border_color='#1F3A5F', text_wrap=True, valign='vcenter', align='center'))
        f_vendor = wb.add_format(dict(base, bold=True, bg_color='#E8EEF6', font_color='#1F3A5F', border=1,
                                      border_color='#D9DEE7', align=align))
        f_text = wb.add_format(dict(base, border=1, border_color='#D9DEE7', align=align))
        f_date = wb.add_format(dict(base, border=1, border_color='#D9DEE7', num_format='yyyy-mm-dd',
                                    align='center'))
        f_int = wb.add_format(dict(base, border=1, border_color='#D9DEE7', align='center'))
        f_pct = wb.add_format(dict(base, border=1, border_color='#D9DEE7', num_format='0.00%', align='center'))
        f_sub_label = wb.add_format(dict(base, bold=True, bg_color='#F4F6FA', border=1, border_color='#D9DEE7',
                                         align=align))
        f_total_label = wb.add_format(dict(base, bold=True, bg_color='#1F3A5F', font_color='#FFFFFF', border=1,
                                           align=align))
        f_comp = money_fmt(ccy)
        f_comp_sub = money_fmt(ccy, bold=True, bg_color='#F4F6FA')
        f_comp_total = money_fmt(ccy, bold=True, bg_color='#1F3A5F', font_color='#FFFFFF')
        f_comp_due = money_fmt(ccy, bold=True, font_color='#B42318')
        doc_fmts = {}

        def doc_fmt(currency):
            if currency.id not in doc_fmts:
                doc_fmts[currency.id] = money_fmt(currency)
            return doc_fmts[currency.id]

        def header_block(ws, title, ncols):
            if rtl:
                ws.right_to_left()
            ws.write(0, 0, title, f_title)
            ws.write(1, 0, '%s — %s' % (data['company'].name, data['labels']['currency_note']), f_sub)
            row = 2
            for label, value in data['filters']:
                ws.write(row, 0, label, f_flabel)
                ws.merge_range(row, 1, row, max(ncols - 1, 2), value, f_sub)
                row += 1
            return row + 1

        # ---------------- Sheet 1: summary per PO ----------------
        ws = wb.add_worksheet(_('Pending Payments')[:31])
        heads = [
            (_('Vendor'), 28), (_('Purchase Order'), 16), (_('Order Date'), 12), (_('Currency'), 9),
            (_('PO Amount'), 15), (_('Billed'), 15), (_('Paid'), 15), (_('Due'), 15), (_('Not Billed'), 15),
            (_('Billed (%s)') % ccy.name, 16), (_('Paid (%s)') % ccy.name, 16), (_('Due (%s)') % ccy.name, 16),
            (_('Not Billed (%s)') % ccy.name, 16), (_('Outstanding (%s)') % ccy.name, 17),
            (_('Overdue (%s)') % ccy.name, 16), (_('Max Days Overdue'), 10), (_('Next Due Date'), 12),
            (_('Bills'), 30),
        ]
        comp_cols = list(range(9, 15))  # J..O, company-currency columns that get SUBTOTALs
        comp_keys = ['billed_company', 'paid_company', 'due_company', 'unbilled_company',
                     'outstanding_company', 'overdue_company']
        row = header_block(ws, _('Purchase Pending Payments'), len(heads))
        ws.set_row(row, 32)
        for col, (label, width) in enumerate(heads):
            ws.write(row, col, label, f_head)
            ws.set_column(col, col, width)
        header_row = row
        row += 1
        first_data_row = row
        for vendor in data['vendors']:
            ws.merge_range(row, 0, row, len(heads) - 1, vendor['partner'].display_name, f_vendor)
            row += 1
            start = row
            for r in vendor['rows']:
                ws.write(row, 0, r['partner'].display_name, f_text)
                ws.write(row, 1, r['name'], f_text)
                ws.write_datetime(row, 2, datetime.combine(r['date_order'], time.min), f_date)
                ws.write(row, 3, r['currency'].name, f_int)
                for col, key in zip(range(4, 9), ('amount_order', 'billed', 'paid', 'due', 'unbilled')):
                    ws.write_number(row, col, r[key], doc_fmt(r['currency']))
                for col, key in zip(comp_cols, comp_keys):
                    ws.write_number(row, col, r[key], f_comp_due if key == 'outstanding_company' else f_comp)
                ws.write_number(row, 15, r['max_days_overdue'], f_int)
                if r['next_due_date']:
                    ws.write_datetime(row, 16, datetime.combine(r['next_due_date'], time.min), f_date)
                else:
                    ws.write_blank(row, 16, None, f_text)
                ws.write(row, 17, ', '.join(b['name'] for b in r['bills']), f_text)
                row += 1
            end = row - 1
            ws.merge_range(row, 0, row, 8, _('Subtotal %s') % vendor['partner'].display_name, f_sub_label)
            for col, key in zip(comp_cols, comp_keys):
                rng = xl_range(start, col, end, col)
                ws.write_formula(row, col, '=SUBTOTAL(9,%s)' % rng, f_comp_sub, vendor['totals'][key])
            for col in (15, 16, 17):
                ws.write_blank(row, col, None, f_comp_sub)
            row += 1
        last_data_row = row - 1
        ws.merge_range(row, 0, row, 8, _('Grand Total (%s)') % ccy.name, f_total_label)
        for col, key in zip(comp_cols, comp_keys):
            rng = xl_range(first_data_row, col, last_data_row, col)
            ws.write_formula(row, col, '=SUBTOTAL(9,%s)' % rng, f_comp_total, data['totals'][key])
        for col in (15, 16, 17):
            ws.write_blank(row, col, None, f_comp_total)
        ws.freeze_panes(header_row + 1, 2)
        ws.autofilter(header_row, 0, last_data_row, len(heads) - 1)
        ws.set_landscape()
        ws.fit_to_pages(1, 0)
        ws.repeat_rows(header_row)

        # ---------------- Sheet 2: bills & payment allocations ----------------
        ws2 = wb.add_worksheet(_('Bills & Payments')[:31])
        heads2 = [
            (_('Vendor'), 26), (_('Purchase Order'), 15), (_('Document'), 18), (_('Type'), 18),
            (_('Date'), 12), (_('Due Date'), 12), (_('Days Overdue'), 9), (_('Currency'), 9),
            (_('Allocation'), 10), (_('Amount'), 15), (_('Paid'), 15), (_('Residual'), 15),
            (_('Amount (%s)') % ccy.name, 16), (_('Paid (%s)') % ccy.name, 16),
            (_('Residual (%s)') % ccy.name, 16), (_('Journal'), 20), (_('Reference'), 22),
        ]
        row = header_block(ws2, _('Vendor Bills & Payment Allocations'), len(heads2))
        ws2.set_row(row, 32)
        for col, (label, width) in enumerate(heads2):
            ws2.write(row, col, label, f_head)
            ws2.set_column(col, col, width)
        header_row2 = row
        row += 1
        first2 = row
        f_pay = wb.add_format(dict(base, italic=True, font_color='#5B6B7F', border=1, border_color='#D9DEE7',
                                   align=align, indent=1))
        for r in data['rows']:
            for b in r['bills']:
                bill_type = _('Credit Note') if b['move_type'] == 'in_refund' else _('Vendor Bill')
                ws2.write(row, 0, r['partner'].display_name, f_text)
                ws2.write(row, 1, r['name'], f_text)
                ws2.write(row, 2, b['name'], f_text)
                ws2.write(row, 3, bill_type, f_text)
                ws2.write_datetime(row, 4, datetime.combine(b['date'], time.min), f_date)
                ws2.write_datetime(row, 5, datetime.combine(b['date_due'], time.min), f_date)
                ws2.write_number(row, 6, b['days_overdue'], f_int)
                ws2.write(row, 7, b['currency'].name, f_int)
                ws2.write_number(row, 8, b['ratio'], f_pct)
                ws2.write_number(row, 9, b['total_currency'], doc_fmt(b['currency']))
                ws2.write_number(row, 10, b['paid_currency'], doc_fmt(b['currency']))
                ws2.write_number(row, 11, b['residual_currency'], doc_fmt(b['currency']))
                ws2.write_number(row, 12, b['total_company_alloc'], f_comp)
                ws2.write_number(row, 13, b['paid_company_alloc'], f_comp)
                ws2.write_number(row, 14, b['residual_company_alloc'], f_comp)
                ws2.write(row, 15, b['bill'].journal_id.display_name, f_text)
                ws2.write(row, 16, b['ref'], f_text)
                row += 1
                for p in b['payments']:
                    ws2.write(row, 0, '', f_pay)
                    ws2.write(row, 1, r['name'], f_pay)
                    ws2.write(row, 2, p['name'], f_pay)
                    ws2.write(row, 3, self._payment_kind_label(p['kind']), f_pay)
                    ws2.write_datetime(row, 4, datetime.combine(p['date'], time.min), f_date)
                    for col in (5, 6, 7, 8, 9, 11, 12, 14):
                        ws2.write_blank(row, col, None, f_pay)
                    ws2.write_number(row, 10, p['amount_currency'], doc_fmt(b['currency']))
                    ws2.write_number(row, 13, p['amount_company'], f_comp)
                    ws2.write(row, 15, p['journal'], f_pay)
                    ws2.write(row, 16, p['ref'], f_pay)
                    row += 1
        ws2.freeze_panes(header_row2 + 1, 3)
        if row > first2:
            ws2.autofilter(header_row2, 0, row - 1, len(heads2) - 1)
        ws2.set_landscape()
        ws2.fit_to_pages(1, 0)

        # ---------------- Sheet 3: currency breakdown ----------------
        ws3 = wb.add_worksheet(_('Currency Summary')[:31])
        heads3 = [
            (_('Currency'), 10), (_('Orders'), 9), (_('PO Amount'), 16), (_('Billed'), 16), (_('Paid'), 16),
            (_('Due'), 16), (_('Not Billed'), 16), (_('Due (%s)') % ccy.name, 17),
            (_('Not Billed (%s)') % ccy.name, 17), (_('Outstanding (%s)') % ccy.name, 18),
        ]
        row = header_block(ws3, _('Currency Breakdown'), len(heads3))
        ws3.set_row(row, 32)
        for col, (label, width) in enumerate(heads3):
            ws3.write(row, col, label, f_head)
            ws3.set_column(col, col, width)
        row += 1
        first3 = row
        for e in data['currencies']:
            ws3.write(row, 0, e['currency'].name, f_int)
            ws3.write_number(row, 1, e['count'], f_int)
            for col, key in zip(range(2, 7), ('amount_order', 'billed', 'paid', 'due', 'unbilled')):
                ws3.write_number(row, col, e[key], doc_fmt(e['currency']))
            for col, key in zip(range(7, 10), ('due_company', 'unbilled_company', 'outstanding_company')):
                ws3.write_number(row, col, e[key], f_comp)
            row += 1
        ws3.write(row, 0, _('Total'), f_total_label)
        ws3.write_formula(row, 1, '=SUM(%s)' % xl_range(first3, 1, row - 1, 1),
                          wb.add_format(dict(base, bold=True, bg_color='#1F3A5F', font_color='#FFFFFF',
                                             align='center', border=1)), data['order_count'])
        for col in range(2, 7):
            ws3.write_blank(row, col, None, f_comp_total)
        for col, key in zip(range(7, 10), ('due_company', 'unbilled_company', 'outstanding_company')):
            ws3.write_formula(row, col, '=SUM(%s)' % xl_range(first3, col, row - 1, col),
                              f_comp_total, data['totals'][key])

        wb.close()
        return output.getvalue()
