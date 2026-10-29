from odoo import api, fields, models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    quickbooks_state = fields.Selection(
        [("none", "Not in QuickBooks"), ("sent", "In QuickBooks"), ("error", "QuickBooks error")],
        string="QuickBooks",
        compute="_compute_quickbooks_state",
    )

    @api.depends("state", "write_date")
    def _compute_quickbooks_state(self):
        links = {
            link.res_id: link
            for link in self.env["quickbooks.link"].search(
                [("res_model", "=", "account.move"), ("res_id", "in", self.ids)]
            )
        }
        for move in self:
            link = links.get(move.id)
            move.quickbooks_state = (
                "none" if not link else "error" if link.error else "sent" if link.remote_id else "none"
            )

    def action_send_to_quickbooks(self):
        """Send these documents now instead of waiting for the next round (ignores the 'dated from' limit)."""
        for company, moves in self.grouped("company_id").items():
            account = self.env["quickbooks.account"].search(
                [("company_id", "=", company.id), ("state", "=", "connected")], limit=1
            )
            if not account:
                raise UserError(self.env._("%s is not connected to QuickBooks.", company.name))
            account._send_now(moves)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("QuickBooks"),
                "message": self.env._("Sent to QuickBooks."),
                "type": "success",
            },
        }
