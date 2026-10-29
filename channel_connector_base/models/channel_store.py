import logging

from odoo import _, api, fields, models

from .channel_error import ChannelAuthError, ChannelError

_logger = logging.getLogger(__name__)


class ChannelStore(models.Model):
    """A connected shop. Connector modules add a `channel_type` and implement the
    `_fetch_*` / `_push_*` hooks at the bottom of this file."""

    _name = "channel.store"
    _description = "Sales Channel Store"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "name"

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    channel_type = fields.Selection([], required=True, readonly=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )
    state = fields.Selection(
        [("draft", "Not connected"), ("connected", "Connected"), ("error", "Connection problem")],
        default="draft",
        required=True,
        tracking=True,
        copy=False,
    )
    connection_message = fields.Char(copy=False, readonly=True)
    color = fields.Integer()

    # Credentials -----------------------------------------------------------
    shop_url = fields.Char(string="Store URL")
    api_key = fields.Char(
        string="Key / Client ID",
        groups="channel_connector_base.group_channel_manager", copy=False
    )
    api_secret = fields.Char(
        string="Secret / Client secret",
        groups="channel_connector_base.group_channel_manager", copy=False
    )
    access_token = fields.Char(
        groups="channel_connector_base.group_channel_manager", copy=False
    )
    token_expiry = fields.Datetime(
        groups="channel_connector_base.group_channel_manager", copy=False
    )
    webhook_secret = fields.Char(
        groups="channel_connector_base.group_channel_manager", copy=False
    )

    # Defaults ----------------------------------------------------------------
    warehouse_id = fields.Many2one(
        "stock.warehouse",
        domain="[('company_id', '=', company_id)]",
        default=lambda self: self.env["stock.warehouse"].search(
            [("company_id", "=", self.env.company.id)], limit=1
        ),
    )
    pricelist_id = fields.Many2one("product.pricelist", string="Pricelist")
    team_id = fields.Many2one("crm.team", string="Sales Team")
    shipping_product_id = fields.Many2one(
        "product.product",
        default=lambda self: self.env.ref(
            "channel_connector_base.product_channel_shipping", raise_if_not_found=False
        ),
    )
    adjustment_product_id = fields.Many2one(
        "product.product",
        default=lambda self: self.env.ref(
            "channel_connector_base.product_channel_adjustment", raise_if_not_found=False
        ),
    )
    create_missing_products = fields.Boolean(
        default=True,
        help="When an order contains a product Odoo does not know, create it "
        "instead of failing the import.",
    )
    refund_post = fields.Boolean(
        string="Validate credit notes",
        help="Post credit notes automatically. Otherwise they stay in draft for review.",
    )

    # Automation ------------------------------------------------------------
    auto_import_orders = fields.Boolean(string="Import orders automatically", default=True)
    auto_push_stock = fields.Boolean(string="Update stock automatically", default=True)
    auto_push_fulfillment = fields.Boolean(
        string="Send shipments & tracking automatically", default=True
    )
    import_orders_from = fields.Datetime(
        default=fields.Datetime.now,
        help="Orders created on the channel before this date are never imported.",
    )
    last_order_sync = fields.Datetime(readonly=True, copy=False)
    last_stock_sync = fields.Datetime(readonly=True, copy=False)
    stock_quantity_type = fields.Selection(
        [
            ("free_qty", "Available (on hand minus reserved)"),
            ("qty_available", "On hand"),
            ("virtual_available", "Forecasted"),
        ],
        default="free_qty",
        required=True,
    )
    stock_safety = fields.Integer(
        string="Safety stock",
        help="Hold back this many units: the channel always sees the Odoo quantity minus this.",
    )

    # Relations --------------------------------------------------------------
    workflow_ids = fields.One2many("channel.workflow", "store_id")
    location_ids = fields.One2many("channel.location", "store_id")
    job_ids = fields.One2many("channel.job", "store_id")
    mapping_ids = fields.One2many("channel.mapping", "store_id")

    # Dashboard ----------------------------------------------------------------
    order_count = fields.Integer(compute="_compute_counts")
    orders_today = fields.Integer(compute="_compute_counts")
    revenue_today = fields.Monetary(compute="_compute_counts", currency_field="currency_id")
    job_pending_count = fields.Integer(compute="_compute_counts")
    job_failed_count = fields.Integer(compute="_compute_counts")
    product_count = fields.Integer(compute="_compute_counts")
    currency_id = fields.Many2one(related="company_id.currency_id")
    webhook_url = fields.Char(compute="_compute_webhook_url")
    webhooks_registered = fields.Boolean(copy=False, readonly=True)

    @api.depends("job_ids.state", "mapping_ids")
    def _compute_counts(self):
        today = fields.Datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        orders = self.env["sale.order"]._read_group(
            [("channel_store_id", "in", self.ids)],
            ["channel_store_id"],
            ["__count"],
        )
        order_counts = {store.id: count for store, count in orders}
        today_orders = self.env["sale.order"]._read_group(
            [("channel_store_id", "in", self.ids), ("date_order", ">=", today)],
            ["channel_store_id"],
            ["__count", "amount_total:sum"],
        )
        today_map = {store.id: (count, total) for store, count, total in today_orders}
        jobs = self.env["channel.job"]._read_group(
            [("store_id", "in", self.ids), ("state", "in", ("pending", "failed"))],
            ["store_id", "state"],
            ["__count"],
        )
        job_map = {(store.id, state): count for store, state, count in jobs}
        products = self.env["channel.mapping"]._read_group(
            [("store_id", "in", self.ids), ("res_model", "=", "product.template")],
            ["store_id"],
            ["__count"],
        )
        product_map = {store.id: count for store, count in products}
        for store in self:
            store.order_count = order_counts.get(store.id, 0)
            store.orders_today, store.revenue_today = today_map.get(store.id, (0, 0.0))
            store.job_pending_count = job_map.get((store.id, "pending"), 0)
            store.job_failed_count = job_map.get((store.id, "failed"), 0)
            store.product_count = product_map.get(store.id, 0)

    @api.depends("channel_type")
    def _compute_webhook_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for store in self:
            store.webhook_url = (
                f"{base}/channel/{store.channel_type}/webhook/{store.id}"
                if store.id and store.channel_type
                else False
            )

    # -- generic helpers --------------------------------------------------------

    def _queue_job(self, job_type, name, payload=None, dedupe_key=None, external_ref=None):
        self.ensure_one()
        return self.env["channel.job"]._enqueue(
            self, job_type, name, payload, dedupe_key, external_ref
        )

    def _get_mapped(self, res_model, external_id):
        self.ensure_one()
        mapping = self.env["channel.mapping"].search(
            [
                ("store_id", "=", self.id),
                ("res_model", "=", res_model),
                ("external_id", "=", str(external_id)),
            ],
            limit=1,
        )
        return self.env[res_model].browse(mapping.res_id).exists() if mapping else self.env[res_model]

    def _set_mapping(self, record, external_id, external_ref=None):
        self.ensure_one()
        mapping = self.env["channel.mapping"].search(
            [
                ("store_id", "=", self.id),
                ("res_model", "=", record._name),
                ("external_id", "=", str(external_id)),
            ],
            limit=1,
        )
        values = {"res_id": record.id}
        if external_ref is not None:
            values["external_ref"] = str(external_ref)
        if mapping:
            mapping.write(values)
            return mapping
        return self.env["channel.mapping"].create(
            {
                "store_id": self.id,
                "res_model": record._name,
                "external_id": str(external_id),
                **values,
            }
        )

    def _find_workflow(self, gateways):
        self.ensure_one()
        for workflow in self.workflow_ids.sorted("sequence"):
            if workflow._matches(gateways):
                return workflow
        return self.env["channel.workflow"]

    # -- buttons -----------------------------------------------------------------

    def action_test_connection(self):
        self.ensure_one()
        try:
            shop_name = self._test_connection()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            message = exc.args[0] if isinstance(exc, ChannelError) and exc.args else str(exc)
            self.write({"state": "error", "connection_message": message})
            if isinstance(exc, ChannelError):
                return self._notify(_("Connection failed"), message, "danger")
            raise
        self.write(
            {"state": "connected", "connection_message": _("Connected to %s", shop_name)}
        )
        return self._notify(_("Connected"), _("Connected to %s.", shop_name), "success")

    def _notify(self, title, message, kind="info"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": kind, "sticky": kind == "danger"},
        }

    def action_register_webhooks(self):
        self.ensure_one()
        self._register_webhooks()
        self.webhooks_registered = True
        return self._notify(
            _("Webhooks registered"),
            _("New orders will now arrive in Odoo within seconds."),
            "success",
        )

    def action_import_orders(self):
        self.ensure_one()
        count = self._import_orders()
        self.env["channel.job"]._run_due_jobs()
        return self._notify(_("Orders"), _("%s order(s) queued and processed.", count))

    def action_push_stock(self):
        self.ensure_one()
        count = self._push_stock()
        self.env["channel.job"]._run_due_jobs()
        return self._notify(_("Stock"), _("%s stock level(s) sent.", count))

    def action_import_products(self):
        self.ensure_one()
        count = self._import_products()
        self.env["channel.job"]._run_due_jobs()
        return self._notify(_("Products"), _("%s product(s) queued and processed.", count))

    def action_run_jobs(self):
        self.job_ids.filtered(lambda j: j.state == "pending").write(
            {"next_try": fields.Datetime.now()}
        )
        self.env["channel.job"]._run_due_jobs()

    def _action_open(self, name, model, domain, context=None, view_mode="list,form"):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": model,
            "view_mode": view_mode,
            "domain": domain,
            "context": context or {},
        }

    def action_view_orders(self):
        return self._action_open(
            _("Orders"), "sale.order", [("channel_store_id", "=", self.id)]
        )

    def action_view_failed_jobs(self):
        return self._action_open(
            _("Jobs needing attention"),
            "channel.job",
            [("store_id", "=", self.id), ("state", "=", "failed")],
        )

    def action_view_pending_jobs(self):
        return self._action_open(
            _("Waiting jobs"),
            "channel.job",
            [("store_id", "=", self.id), ("state", "=", "pending")],
        )

    def action_view_mappings(self):
        return self._action_open(
            _("Linked products"),
            "channel.mapping",
            [("store_id", "=", self.id), ("res_model", "=", "product.product")],
        )

    def action_open_setup_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Store settings"),
            "res_model": "channel.store",
            "res_id": self.id,
            "view_mode": "form",
        }

    # -- crons -------------------------------------------------------------------

    @api.model
    def _connected_stores(self, flag=None):
        domain = [("state", "=", "connected")]
        if flag:
            domain.append((flag, "=", True))
        return self.search(domain)

    @api.model
    def _cron_import_orders(self):
        for store in self._connected_stores("auto_import_orders"):
            store._safe_run("import_orders", store._import_orders)

    @api.model
    def _cron_push_stock(self):
        for store in self._connected_stores("auto_push_stock"):
            store._safe_run("push_stock", store._push_stock)

    @api.model
    def _cron_run_jobs(self):
        self.env["channel.job"]._run_due_jobs()

    def _safe_run(self, label, func):
        """Run a store level task; a failure becomes one visible job, not a crash.

        The job is reused while the problem lasts and closed once the task works again."""
        self.ensure_one()
        key = f"store:{label}"
        Job = self.env["channel.job"]
        open_job = Job.search(
            [("store_id", "=", self.id), ("dedupe_key", "=", key), ("state", "!=", "done")], limit=1
        )
        try:
            with self.env.cr.savepoint():
                result = func()
        except Exception as exc:  # noqa: BLE001 - recorded as a failed job
            _logger.warning("Store %s: %s failed", self.name, label, exc_info=True)
            job = open_job or Job.create(
                {
                    "name": _("Sync failed: %s", label.replace("_", " ")),
                    "store_id": self.id,
                    "job_type": "other",
                    "dedupe_key": key,
                }
            )
            job._register_failure(exc)
            job.write({"state": "failed", "retry_count": job.max_retries})
            if isinstance(exc, ChannelAuthError) and self.state == "connected":
                self.state = "error"
                self.connection_message = exc.args[0] if exc.args else str(exc)
            return None
        open_job.action_mark_done()
        return result

    # -- job dispatch ------------------------------------------------------------

    def _run_job(self, job):
        self.ensure_one()
        handler = getattr(self, f"_job_{job.job_type}", None)
        if handler is None:
            raise ChannelError(_("This kind of job (%s) is not supported.", job.job_type))
        return handler(job)

    # -- connector hooks ---------------------------------------------------------

    def _not_implemented(self, what):
        raise ChannelError(
            _("The %(type)s connector does not support: %(what)s", type=self.channel_type, what=what)
        )

    def _dispatch(self, hook, *args, **kwargs):
        """Call `_<channel_type>_<hook>`: several connectors can be installed side by side."""
        method = getattr(self, f"_{self.channel_type}_{hook}", None)
        if method is None:
            self._not_implemented(hook.replace("_", " "))
        return method(*args, **kwargs)

    def _test_connection(self):
        """Check credentials, refresh locations and return the shop's name."""
        return self._dispatch("test_connection")

    def _register_webhooks(self):
        return self._dispatch("register_webhooks")

    def _fetch_orders(self, since):
        """Yield normalised order dicts changed since `since` (see the README)."""
        return self._dispatch("fetch_orders", since)

    def _fetch_order(self, external_id):
        return self._dispatch("fetch_order", external_id)

    def _fetch_products(self):
        """Yield normalised product dicts."""
        return self._dispatch("fetch_products")

    def _push_stock_levels(self, items):
        return self._dispatch("push_stock_levels", items)

    def _push_fulfillment(self, picking):
        return self._dispatch("push_fulfillment", picking)

    def _verify_webhook(self, body, headers):
        return self._dispatch("verify_webhook", body, headers) if self._has_hook("verify_webhook") else False

    def _parse_webhook(self, body, headers):
        """Return a list of (job_type, name, payload, dedupe_key) to queue."""
        return self._dispatch("parse_webhook", body, headers) if self._has_hook("parse_webhook") else []

    def _has_hook(self, hook):
        return hasattr(self, f"_{self.channel_type}_{hook}")

    @api.model
    def _credentials_help(self, channel_type):
        """HTML shown under the credential fields of the setup wizard."""
        return False
