from datetime import timedelta

from odoo import api, fields, models

KEEP_DAYS = 90


class ClaudeMcpLog(models.Model):
    """What Claude asked Odoo to do, one line per tool call."""

    _name = "claude.mcp.log"
    _description = "Claude MCP Audit Entry"
    _order = "id desc"

    token_id = fields.Many2one("claude.mcp.token", required=True, ondelete="cascade", index=True)
    user_id = fields.Many2one("res.users", related="token_id.user_id", store=True)
    tool = fields.Char(required=True)
    model_name = fields.Char(string="Model")
    summary = fields.Char(help="The request in short form.")
    success = fields.Boolean()
    error = fields.Char()
    duration_ms = fields.Integer(string="Duration (ms)")

    @api.model
    def _cron_cleanup(self):
        self.search([("create_date", "<", fields.Datetime.now() - timedelta(days=KEEP_DAYS))]).unlink()
