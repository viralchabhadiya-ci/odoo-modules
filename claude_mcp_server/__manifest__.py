{
    "name": "Claude MCP Server",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Let Claude read and, if you allow it, update Odoo through the Model Context Protocol: "
    "per-key model permissions, record filters, audit log and daily limits",
    "author": "Veer",
    "website": "https://github.com/viralchabhadiya-ci/odoo-modules",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [
        "security/mcp_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/mcp_token_views.xml",
    ],
    "application": True,
    "installable": True,
}
