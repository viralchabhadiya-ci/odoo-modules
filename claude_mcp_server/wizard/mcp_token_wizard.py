from odoo import fields, models


class ClaudeMcpTokenWizard(models.TransientModel):
    """Shows the new key once. The form is never saved, so the key is not stored in clear."""

    _name = "claude.mcp.token.wizard"
    _description = "Claude MCP Access Key"

    token_id = fields.Many2one("claude.mcp.token", readonly=True)
    url = fields.Char(string="Server address", readonly=True)
    token = fields.Char(string="Access key", readonly=True)
    command = fields.Text(string="Claude Code", readonly=True)
    config = fields.Text(string="Claude Desktop", readonly=True)
