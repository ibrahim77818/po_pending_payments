# -*- coding: utf-8 -*-
from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import content_disposition, request


class PoPendingPaymentsController(http.Controller):

    @http.route('/po_pending_payments/xlsx/<int:wizard_id>', type='http', auth='user')
    def download_xlsx(self, wizard_id, **kwargs):
        wizard = request.env['po.pending.payment.wizard'].browse(wizard_id).exists()
        if not wizard:
            raise NotFound()
        # Transient records are only readable by their creator; this raises otherwise.
        wizard.check_access('read')
        content = wizard._generate_xlsx()
        return request.make_response(content, headers=[
            ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Length', len(content)),
            ('Content-Disposition', content_disposition(wizard._xlsx_filename())),
        ])
