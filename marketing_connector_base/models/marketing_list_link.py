from odoo import api, fields, models


class MarketingListLink(models.Model):
    """One audience / list on the provider, optionally kept in step with an Odoo mailing list."""

    _name = "marketing.list.link"
    _description = "Email Marketing List"
    _order = "account_id, name"

    account_id = fields.Many2one("marketing.account", required=True, ondelete="cascade", index=True)
    provider = fields.Selection(related="account_id.provider")
    company_id = fields.Many2one(related="account_id.company_id", store=True)
    remote_id = fields.Char(string="Provider ID", required=True, readonly=True)
    name = fields.Char(string="Audience / list", required=True, readonly=True)
    remote_count = fields.Integer(string="Contacts there", readonly=True)
    missing = fields.Boolean(string="Deleted at provider", readonly=True)
    sync = fields.Boolean(
        string="Keep in sync",
        help="Contacts and unsubscribes are exchanged with the Odoo mailing list below every 15 minutes.",
    )
    list_id = fields.Many2one(
        "mailing.list",
        string="Odoo mailing list",
        help="Leave empty to use the Odoo list of the same name, or to create it.",
    )
    last_pull = fields.Datetime(readonly=True, copy=False)
    last_push = fields.Datetime(readonly=True, copy=False)
    webhook_ref = fields.Char(readonly=True, copy=False)
    error = fields.Char(string="Last problem", readonly=True, copy=False)
    member_ids = fields.One2many("marketing.member", "link_id")
    odoo_count = fields.Integer(string="Contacts in Odoo", compute="_compute_odoo_count")
    problem_count = fields.Integer(compute="_compute_odoo_count")

    _unique_remote = models.Constraint("unique (account_id, remote_id)", "This list is already known for the account.")

    @api.depends("list_id.contact_count", "member_ids.sync_error")
    def _compute_odoo_count(self):
        for link in self:
            link.odoo_count = link.list_id.contact_count
            link.problem_count = len(link.member_ids.filtered("sync_error"))

    def _ensure_list(self):
        """Use the chosen Odoo list, else one with the same name, else create it."""
        for link in self.filtered(lambda link: not link.list_id):
            MailingList = self.env["mailing.list"]
            link.list_id = MailingList.search([("name", "=ilike", link.name)], limit=1) or MailingList.create(
                {"name": link.name}
            )

    def action_sync_now(self):
        for link in self:
            link.account_id._sync_link(link)
        failed = self.filtered("error")
        if failed:
            return self.account_id[:1]._notify(self.env._("Sync stopped"), "; ".join(failed.mapped("error")), "danger")
        return self.account_id[:1]._notify(
            self.env._("Synced"), self.env._("Contacts and unsubscribes are up to date.")
        )

    def action_view_problems(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Contacts that could not be synced"),
            "res_model": "marketing.member",
            "view_mode": "list,form",
            "domain": [("link_id", "=", self.id), ("sync_error", "!=", False)],
        }
