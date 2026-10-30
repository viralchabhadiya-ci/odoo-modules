from odoo import api, fields, models


class MarketingCampaign(models.Model):
    """Results of a campaign sent from the provider (read only)."""

    _name = "marketing.campaign"
    _description = "Email Marketing Campaign Result"
    _order = "sent_date desc, id desc"

    account_id = fields.Many2one("marketing.account", required=True, ondelete="cascade", index=True)
    provider = fields.Selection(related="account_id.provider")
    remote_id = fields.Char(required=True, readonly=True)
    name = fields.Char(required=True, readonly=True)
    subject = fields.Char(readonly=True)
    list_name = fields.Char(string="Audience / list", readonly=True)
    sent_date = fields.Datetime(readonly=True)
    emails_sent = fields.Integer(readonly=True)
    opens = fields.Integer(string="Opened", readonly=True)
    clicks = fields.Integer(string="Clicked", readonly=True)
    unsubscribes = fields.Integer(readonly=True)
    bounces = fields.Integer(readonly=True)
    open_rate = fields.Float(compute="_compute_rates", store=True, aggregator="avg")
    click_rate = fields.Float(compute="_compute_rates", store=True, aggregator="avg")

    _unique_remote = models.Constraint(
        "unique (account_id, remote_id)", "This campaign is already known for the account."
    )

    @api.depends("emails_sent", "opens", "clicks")
    def _compute_rates(self):
        for campaign in self:
            sent = campaign.emails_sent or 0
            campaign.open_rate = campaign.opens / sent * 100 if sent else 0
            campaign.click_rate = campaign.clicks / sent * 100 if sent else 0
