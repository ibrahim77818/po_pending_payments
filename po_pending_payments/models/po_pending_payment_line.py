# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.tools.misc import formatLang


class PoPendingPaymentLine(models.TransientModel):
    """One row per purchase order of a pending payments run (on-screen view)."""
    _name = 'po.pending.payment.line'
    _description = 'Purchase Pending Payments Line'
    _order = 'partner_name, date_order, id'

    wizard_id = fields.Many2one('po.pending.payment.wizard', required=True, ondelete='cascade', index=True)
    name = fields.Char(string='Reference', readonly=True)
    company_id = fields.Many2one('res.company', related='wizard_id.company_id')
    company_currency_id = fields.Many2one('res.currency', string='Company Currency', readonly=True)
    order_id = fields.Many2one('purchase.order', string='Purchase Order', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Vendor', readonly=True)
    partner_name = fields.Char(readonly=True)
    date_order = fields.Date(string='Order Date', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    amount_order = fields.Monetary(string='PO Amount', currency_field='currency_id', readonly=True,
                                  group_operator=False)
    amount_billed = fields.Monetary(string='Billed', currency_field='currency_id', readonly=True,
                                  group_operator=False)
    amount_paid = fields.Monetary(string='Paid', currency_field='currency_id', readonly=True,
                                  group_operator=False)
    amount_due = fields.Monetary(string='Due', currency_field='currency_id', readonly=True,
                                  group_operator=False)
    amount_unbilled = fields.Monetary(string='Not Billed', currency_field='currency_id', readonly=True,
                                  group_operator=False)

    amount_order_company = fields.Monetary(
        string='PO Amount (Company)', currency_field='company_currency_id', readonly=True,
        help='Order total converted at the order-date rate.')
    billed_company = fields.Monetary(
        string='Billed (Company)', currency_field='company_currency_id', readonly=True,
        help='From the posted payable journal items of the bills (bill-date rates).')
    paid_company = fields.Monetary(string='Paid (Company)', currency_field='company_currency_id', readonly=True)
    due_company = fields.Monetary(string='Due (Company)', currency_field='company_currency_id', readonly=True)
    unbilled_company = fields.Monetary(
        string='Not Billed (Company)', currency_field='company_currency_id', readonly=True,
        help='Converted at the "As of" date rate.')
    outstanding_company = fields.Monetary(
        string='Outstanding (Company)', currency_field='company_currency_id', readonly=True)
    overdue_company = fields.Monetary(string='Overdue (Company)', currency_field='company_currency_id',
                                      readonly=True)
    max_days_overdue = fields.Integer(string='Max Days Overdue', readonly=True, group_operator='max')
    next_due_date = fields.Date(string='Next Due Date', readonly=True)
    bill_count = fields.Integer(string='Bills', readonly=True)
    bill_ids = fields.Many2many('account.move', 'po_pp_line_bill_rel', 'line_id', 'move_id',
                                string='Vendor Bills', readonly=True)
    bill_line_ids = fields.One2many('po.pending.payment.bill', 'line_id', string='Bills & Payments',
                                    readonly=True)
    status = fields.Selection([
        ('overdue', 'Overdue'),
        ('due', 'Due'),
        ('unbilled', 'Not Billed'),
        ('settled', 'Settled'),
    ], readonly=True)

    @api.model
    def _prepare_from_row(self, wizard, row):
        ccy = wizard.company_id.currency_id
        if not ccy.is_zero(row['overdue_company']):
            status = 'overdue'
        elif not ccy.is_zero(row['due_company']):
            status = 'due'
        elif not ccy.is_zero(row['unbilled_company']):
            status = 'unbilled'
        else:
            status = 'settled'
        return {
            'wizard_id': wizard.id,
            'company_currency_id': ccy.id,
            'order_id': row['order'].id,
            'name': row['name'],
            'partner_id': row['partner'].id,
            'partner_name': row['partner'].display_name,
            'date_order': row['date_order'],
            'currency_id': row['currency'].id,
            'amount_order': row['amount_order'],
            'amount_billed': row['billed'],
            'amount_paid': row['paid'],
            'amount_due': row['due'],
            'amount_unbilled': row['unbilled'],
            'amount_order_company': row['amount_order_company'],
            'billed_company': row['billed_company'],
            'paid_company': row['paid_company'],
            'due_company': row['due_company'],
            'unbilled_company': row['unbilled_company'],
            'outstanding_company': row['outstanding_company'],
            'overdue_company': row['overdue_company'],
            'max_days_overdue': row['max_days_overdue'],
            'next_due_date': row['next_due_date'],
            'bill_count': len(row['bills']),
            'bill_ids': [(6, 0, [b['bill'].id for b in row['bills']])],
            'status': status,
            'bill_line_ids': [(0, 0, {
                'bill_id': b['bill'].id,
                'name': b['name'],
                'move_type': b['move_type'],
                'date': b['date'],
                'date_due': b['date_due'],
                'days_overdue': b['days_overdue'],
                'currency_id': b['currency'].id,
                'company_currency_id': ccy.id,
                'allocation': b['ratio'] * 100.0,
                'amount_total': b['total_currency'],
                'amount_paid': b['paid_currency'],
                'amount_residual': b['residual_currency'],
                'amount_total_company': b['total_company_alloc'],
                'amount_paid_company': b['paid_company_alloc'],
                'amount_residual_company': b['residual_company_alloc'],
                'payment_state': b['payment_state'],
                'payment_summary': '\n'.join(
                    '%s  %s  %s  %s' % (
                        wizard._fmt_date(p['date']), p['name'] or '',
                        wizard._payment_kind_label(p['kind']),
                        formatLang(wizard.env, p['amount_currency'], currency_obj=b['currency']))
                    for p in b['payments']),
            }) for b in row['bills']],
        }

    def action_open_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'res_id': self.order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_open_bills(self):
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id('account.action_move_in_invoice_type')
        action['domain'] = [('id', 'in', self.bill_ids.ids)]
        action['context'] = {'default_move_type': 'in_invoice', 'create': False}
        if len(self.bill_ids) == 1:
            action.update({'view_mode': 'form', 'res_id': self.bill_ids.id,
                           'views': [(self.env.ref('account.view_move_form').id, 'form')]})
        return action

    def _get_wizard(self):
        wizard = self.wizard_id[:1]
        if not wizard and self.env.context.get('po_pp_wizard_id'):
            wizard = self.env['po.pending.payment.wizard'].browse(self.env.context['po_pp_wizard_id'])
        return wizard.exists()

    def action_print_pdf(self):
        return self._get_wizard().action_print_pdf()

    def action_export_xlsx(self):
        return self._get_wizard().action_export_xlsx()


class PoPendingPaymentBill(models.TransientModel):
    """Bill detail of a pending payments line, with its payment allocations."""
    _name = 'po.pending.payment.bill'
    _description = 'Purchase Pending Payments Bill Detail'
    _order = 'date, id'

    line_id = fields.Many2one('po.pending.payment.line', required=True, ondelete='cascade', index=True)
    bill_id = fields.Many2one('account.move', string='Vendor Bill', readonly=True)
    name = fields.Char(string='Number', readonly=True)
    move_type = fields.Selection([('in_invoice', 'Vendor Bill'), ('in_refund', 'Credit Note')],
                                 string='Type', readonly=True)
    date = fields.Date(string='Bill Date', readonly=True)
    date_due = fields.Date(string='Due Date', readonly=True)
    days_overdue = fields.Integer(string='Days Overdue', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    company_currency_id = fields.Many2one('res.currency', readonly=True)
    allocation = fields.Float(string='Allocation %', digits=(16, 2), readonly=True,
                              help='Share of this bill attributed to the purchase order.')
    amount_total = fields.Monetary(string='Total', currency_field='currency_id', readonly=True)
    amount_paid = fields.Monetary(string='Paid', currency_field='currency_id', readonly=True,
                                  group_operator=False)
    amount_residual = fields.Monetary(string='Residual', currency_field='currency_id', readonly=True)
    amount_total_company = fields.Monetary(string='Allocated Total (Company)',
                                           currency_field='company_currency_id', readonly=True)
    amount_paid_company = fields.Monetary(string='Allocated Paid (Company)',
                                          currency_field='company_currency_id', readonly=True)
    amount_residual_company = fields.Monetary(string='Allocated Due (Company)',
                                              currency_field='company_currency_id', readonly=True)
    payment_state = fields.Selection(selection=lambda self: self.env['account.move']._fields[
        'payment_state']._description_selection(self.env), string='Payment Status', readonly=True)
    payment_summary = fields.Text(string='Payments', readonly=True)
