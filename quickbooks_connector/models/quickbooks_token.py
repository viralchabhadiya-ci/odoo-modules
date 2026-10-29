from odoo import fields, models


class QuickBooksToken(models.Model):
    """The sign-in of a connection. Kept apart from the connection so that refreshing it never waits for a sync."""

    _name = "quickbooks.token"
    _description = "QuickBooks Sign-in"

    account_id = fields.Many2one("quickbooks.account", required=True, ondelete="cascade", index=True)
    access_token = fields.Char()
    refresh_token = fields.Char()
    expiry = fields.Datetime()

    _unique_account = models.Constraint("unique (account_id)", "A connection has one sign-in.")
