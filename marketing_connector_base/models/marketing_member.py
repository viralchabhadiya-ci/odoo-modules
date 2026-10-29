from odoo import fields, models


class MarketingMember(models.Model):
    """What we last exchanged for one contact on one list, so an unchanged contact is never sent again."""

    _name = "marketing.member"
    _description = "Email Marketing Contact"
    _order = "id desc"

    link_id = fields.Many2one("marketing.list.link", required=True, ondelete="cascade", index=True)
    account_id = fields.Many2one(related="link_id.account_id", store=True, index=True)
    contact_id = fields.Many2one("mailing.contact", required=True, ondelete="cascade", index=True)
    email = fields.Char(readonly=True)
    remote_id = fields.Char(string="Provider ID", readonly=True)
    remote_subscribed = fields.Boolean(
        default=True, readonly=True, help="Subscribed at the provider when we last looked."
    )
    pushed_hash = fields.Char(readonly=True, copy=False)
    sync_error = fields.Char(string="Problem", readonly=True, copy=False)

    _unique_contact = models.Constraint("unique (link_id, contact_id)", "This contact is already linked on this list.")
