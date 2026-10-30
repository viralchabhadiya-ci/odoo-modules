{
    "name": "Email Marketing Connector Base",
    "version": "19.0.1.0.0",
    "category": "Marketing/Email Marketing",
    "summary": "Shared engine for Mailchimp, Brevo and other email marketing connectors: "
    "mailing lists, contacts, unsubscribes and campaign results",
    "author": "Veer",
    "website": "https://github.com/viralchabhadiya-ci/odoo-modules",
    "license": "LGPL-3",
    "depends": ["mass_mailing"],
    "data": [
        "security/marketing_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/marketing_account_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
}
