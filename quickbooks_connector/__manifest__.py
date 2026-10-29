{
    "name": "QuickBooks Online Connector",
    "version": "19.0.1.0.0",
    "category": "Accounting/Accounting",
    "summary": "Two-way link with QuickBooks Online: invoices, bills, credit notes, payments, customers and vendors, "
    "with one-click sign-in, duplicate-safe contacts and a per-record sync log",
    "author": "Veer",
    "website": "https://github.com/viralchabhadiya-ci/odoo-modules",
    "license": "LGPL-3",
    "depends": ["account"],
    "data": [
        "security/quickbooks_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/quickbooks_account_views.xml",
        "views/account_move_views.xml",
    ],
    "application": True,
    "installable": True,
}
