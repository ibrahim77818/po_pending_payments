# -*- coding: utf-8 -*-
{
    'name': 'Purchase Pending Payments Report',
    'version': '16.0.1.0.0',
    'summary': 'Vendor pending payments per Purchase Order: on-screen, PDF and Excel, '
               'with bills, payment allocations and multi-currency totals',
    'description': """
Purchase Pending Payments Report
================================
A reporting wizard under Purchase > Reporting that shows, per vendor and per
Purchase Order, what has been billed, what has been paid, what is still due on
posted vendor bills, and what is not yet billed.

* Filters: vendors, purchase orders, order-date or bill-date period,
  due-date thresholds (overdue, not yet due, due within N days, due-date range)
* Outputs: interactive list view, styled PDF (Arabic RTL aware), Excel workbook
  with live SUBTOTAL formulas
* Traceability: every bill, its allocation to the PO and every payment,
  credit note or exchange difference reconciled against it
* Multi-currency: document-currency amounts plus company-currency amounts taken
  from the posted journal items (historical rates, partial reconciliations)
* Does not override or modify any standard Odoo report
""",
    'author': 'Ibrahim Yahia',
    'website': 'https://www.linkedin.com/in/ibrahim-yahia',
    'category': 'Inventory/Purchase',
    'license': 'LGPL-3',
    'depends': ['purchase'],
    'data': [
        'security/ir.model.access.csv',
        'report/report_paperformat.xml',
        'report/report_pending_payments.xml',
        'wizard/po_pending_payment_wizard_views.xml',
        'views/po_pending_payment_line_views.xml',
        'views/menus.xml',
    ],
    'assets': {
        'web.report_assets_common': [
            'po_pending_payments/static/src/css/report_pending_payments.css',
        ],
    },
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
