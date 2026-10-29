from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError


class ChannelSetupWizard(models.TransientModel):
    """Two screens: connect the shop, then choose how orders and stock behave."""

    _name = "channel.setup.wizard"
    _description = "Add a Store"

    step = fields.Selection(
        [("connect", "Connect"), ("settings", "Settings")], default="connect", required=True
    )
    channel_type = fields.Selection(
        lambda self: self.env["channel.store"]._fields["channel_type"]._description_selection(self.env),
        required=True,
        string="Platform",
        default=lambda self: next(
            iter(self.env["channel.store"]._fields["channel_type"]._description_selection(self.env)),
            (False,),
        )[0],
    )
    name = fields.Char(string="Store name", required=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    shop_url = fields.Char(string="Store URL")
    api_key = fields.Char()
    api_secret = fields.Char()
    access_token = fields.Char()
    webhook_secret = fields.Char()
    credentials_help = fields.Html(compute="_compute_credentials_help", sanitize=False)
    store_id = fields.Many2one("channel.store", readonly=True)

    warehouse_id = fields.Many2one("stock.warehouse", domain="[('company_id', '=', company_id)]")
    import_products = fields.Boolean(string="Import my products now", default=True)
    import_old_orders = fields.Boolean(
        string="Also import orders from the last 30 days",
        help="Otherwise only orders placed from now on are imported.",
    )
    auto_workflow = fields.Boolean(
        string="Confirm, invoice and register payment for paid orders", default=True
    )
    payment_journal_id = fields.Many2one(
        "account.journal", domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]"
    )

    @api.depends("channel_type")
    def _compute_credentials_help(self):
        for wizard in self:
            wizard.credentials_help = (
                self.env["channel.store"]._credentials_help(wizard.channel_type) or False
            )

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_connect(self):
        self.ensure_one()
        store = self.env["channel.store"].create(
            {
                "name": self.name,
                "channel_type": self.channel_type,
                "company_id": self.company_id.id,
                "shop_url": self.shop_url,
                "api_key": self.api_key,
                "api_secret": self.api_secret,
                "access_token": self.access_token,
                "webhook_secret": self.webhook_secret,
            }
        )
        # an error here rolls the store back, so a typo never leaves a half set up shop behind
        shop_name = store._test_connection()
        store.write({"state": "connected", "connection_message": _("Connected to %s", shop_name)})
        self.write(
            {
                "store_id": store.id,
                "step": "settings",
                "warehouse_id": store.warehouse_id.id,
                "payment_journal_id": self.env["account.journal"]
                .search([("type", "=", "bank"), ("company_id", "=", store.company_id.id)], limit=1)
                .id,
            }
        )
        return self._reopen()

    def action_finish(self):
        self.ensure_one()
        store = self.store_id
        if not store:
            raise UserError(_("Connect the store first."))
        values = {"warehouse_id": self.warehouse_id.id}
        if self.import_old_orders:
            values["import_orders_from"] = fields.Datetime.subtract(fields.Datetime.now(), days=30)
        store.write(values)
        store.location_ids.filtered(lambda l: not l.warehouse_id)[:1].warehouse_id = self.warehouse_id
        if self.auto_workflow:
            store.workflow_ids = [
                Command.create(
                    {
                        "name": _("Paid orders"),
                        "payment_journal_id": self.payment_journal_id.id,
                    }
                )
            ]
        else:
            store.workflow_ids = [
                Command.create(
                    {
                        "name": _("Manual: keep as quotation"),
                        "confirm_order": False,
                        "create_invoice": False,
                        "post_invoice": False,
                        "register_payment": False,
                    }
                )
            ]
        try:
            store._register_webhooks()
            store.webhooks_registered = True
        except UserError as exc:
            store.message_post(
                body=_(
                    "Webhooks could not be registered (%s). Orders will still arrive every 15 minutes.",
                    exc.args[0] if exc.args else exc,
                )
            )
        if self.import_products:
            store._import_products()
        store._import_orders()
        return {
            "type": "ir.actions.act_window",
            "res_model": "channel.store",
            "res_id": store.id,
            "view_mode": "form",
        }
