{
    "name": "Power BI Connector",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Let Power BI read Odoo sales, invoices, stock and purchases directly: a ready OData feed with "
    "an access key, no Azure app and no extra Python package",
    "author": "Veer",
    "website": "https://github.com/viralchabhadiya-ci/odoo-modules",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [
        "security/powerbi_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/powerbi_feed_views.xml",
    ],
    "application": True,
    "installable": True,
}
