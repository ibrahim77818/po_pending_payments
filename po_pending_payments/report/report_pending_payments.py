# -*- coding: utf-8 -*-
from odoo import api, models


class ReportPoPendingPayments(models.AbstractModel):
    _name = 'report.po_pending_payments.report_pending_payments'
    _description = 'Purchase Pending Payments PDF Report'

    @api.model
    def _get_report_values(self, docids, data=None):
        wizards = self.env['po.pending.payment.wizard'].browse(docids).exists()
        return {
            'doc_ids': wizards.ids,
            'doc_model': 'po.pending.payment.wizard',
            'docs': wizards,
            'report_data': {wizard.id: wizard._get_report_data() for wizard in wizards},
        }
