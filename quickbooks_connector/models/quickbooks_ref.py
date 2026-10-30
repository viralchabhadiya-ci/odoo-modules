from odoo import fields, models


class QuickBooksRef(models.Model):
    """A QuickBooks list entry Odoo needs to point at: an account or a tax code. Filled by 'Refresh lists'."""

    _name = "quickbooks.ref"
    _description = "QuickBooks List Entry"
    _order = "kind, name"

    account_id = fields.Many2one("quickbooks.account", required=True, ondelete="cascade", index=True)
    kind = fields.Selection([("account", "Account"), ("taxcode", "Tax code")], required=True)
    remote_id = fields.Char(required=True)
    name = fields.Char(required=True)
    number = fields.Char(help="The account number, if the QuickBooks company uses them.")
    account_type = fields.Char(string="Type")
    rate = fields.Float(digits=(16, 4), help="Percentage of a tax code, when it has a single rate.")
    active = fields.Boolean(default=True)

    _unique_remote = models.Constraint("unique (account_id, kind, remote_id)", "This entry is already listed.")

    def _compute_display_name(self):
        for ref in self:
            ref.display_name = f"{ref.number} {ref.name}" if ref.number else ref.name
