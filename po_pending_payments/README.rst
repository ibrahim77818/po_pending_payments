================================
Purchase Pending Payments Report
================================

Odoo 19 (Community & Enterprise). Depends only on ``purchase``. No standard report or view is
overridden: the module adds its own wizard, result models, QWeb PDF and Excel export.

Usage
=====
*Purchase › Reporting › Pending Payments* (Purchase Managers).

Filters
-------
* **Vendors** – empty means all; contacts of a selected company are included.
* **Purchase Orders** – restricted to confirmed orders of the chosen vendors.
* **Period Based On** – *Purchase Order Date* (orders + amounts not yet billed) or
  *Vendor Bill Date* (only bills dated in the period).
* **Due Date Threshold** – all, overdue, not yet due, due within N days, or a due-date range
  (relative to the *As of Date*).
* **Pending Amounts** – bills due + not yet billed, or bills due only.
* **Include Settled Orders**, **Show Bills & Payments**.

Outputs
-------
* **View** – list grouped by vendor (status colours, company-currency totals), drill-down form
  with every bill, its allocation share and its payments.
* **Print PDF** – landscape A4, company layout, KPI cards, per-vendor tables, bill and payment
  lines, currency breakdown. Right-to-left layout when printed in Arabic.
* **Export Excel** – three sheets (pending payments with live ``SUBTOTAL`` vendor subtotals and
  grand total, bills & payment allocations, currency summary). Right-to-left sheets in Arabic.

How amounts are computed
========================
* Confirmed orders (state *Purchase Order*, locked or not); bills = posted vendor bills and refunds linked
  through ``purchase_line_id``. Draft bills count as not yet billed.
* Billed / paid / due come from each bill's payable journal items (``balance``,
  ``amount_residual`` and their currency counterparts), so company-currency figures carry the
  bill-date rate and every partial reconciliation exactly as booked.
* A bill spanning several orders is split in proportion to the bill-line totals of each order.
* Not billed = order lines' remaining quantity × unit total (tax included), converted to the
  company currency at the *As of Date* rate. PO amount in company currency uses the order-date
  rate.
* Payment allocations list payments, credit notes and exchange-difference entries reconciled
  with each bill.

Arabic / RTL
============
``i18n/ar_001.po`` covers the whole module. The PDF stylesheet handles direction itself and is
excluded from Odoo's ``rtlcss`` pass. ``tools/bidi.py`` isolates Arabic and Latin/number runs so
wkhtmltopdf prints mixed text (e.g. ``فاتور/2026/08/0001``, ``1,250.00 ر.س``) without overlapping
glyphs.

Tests
=====
::

    odoo-bin -d <db> -i po_pending_payments --test-tags /po_pending_payments --stop-after-init
