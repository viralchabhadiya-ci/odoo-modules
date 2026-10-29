from odoo import _, fields, models
from odoo.tools import float_compare


class ChannelWorkflow(models.Model):
    """What Odoo does automatically with an imported order, per payment method."""

    _name = "channel.workflow"
    _description = "Sales Channel Order Workflow"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    store_id = fields.Many2one(
        "channel.store", required=True, ondelete="cascade", index=True
    )
    gateway_names = fields.Char(
        string="Payment Methods",
        help="Comma separated payment method names this workflow applies to "
        "(case insensitive, e.g. 'paypal, stripe'). Leave empty for a catch-all.",
    )
    only_if_paid = fields.Boolean(
        string="Only if paid",
        default=True,
        help="Skip the automatic steps when the channel says the order is not paid yet "
        "(cash on delivery, bank transfer...).",
    )
    confirm_order = fields.Boolean(default=True)
    create_invoice = fields.Boolean(default=True)
    post_invoice = fields.Boolean(string="Validate Invoice", default=True)
    register_payment = fields.Boolean(default=True)
    payment_journal_id = fields.Many2one(
        "account.journal",
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
    )
    validate_delivery = fields.Boolean(
        help="Mark the delivery as done in Odoo right away. Leave off when your "
        "warehouse ships the order and Odoo should push the tracking back.",
    )
    company_id = fields.Many2one(related="store_id.company_id")

    def _matches(self, gateways):
        self.ensure_one()
        wanted = {
            g.strip().lower() for g in (self.gateway_names or "").split(",") if g.strip()
        }
        if not wanted:
            return True
        return bool(wanted & {(g or "").strip().lower() for g in gateways or []})

    def _apply(self, order, data):
        """Run the automatic steps on a freshly imported order."""
        self.ensure_one()
        paid = bool(data.get("paid"))
        if self.only_if_paid and not paid:
            order.message_post(
                body=_(
                    "Workflow '%s' skipped: the order is not paid on the channel yet.",
                    self.name,
                )
            )
            return
        if self.confirm_order and order.state in ("draft", "sent"):
            order.action_confirm()
        if self.validate_delivery:
            self._validate_pickings(order, skip_fulfillment_push=bool(data.get("fulfilled")))
        if self.create_invoice and order.state == "sale":
            invoices = order._create_invoices()
            if self.post_invoice:
                invoices.action_post()
                if self.register_payment and paid and self.payment_journal_id:
                    self._register_payments(invoices)

    def _validate_pickings(self, order, skip_fulfillment_push=False):
        pickings = order.picking_ids.filtered(
            lambda p: p.state not in ("done", "cancel")
        )
        pickings.action_assign()
        for move in pickings.move_ids:
            if float_compare(
                move.quantity, move.product_uom_qty, precision_rounding=move.product_uom.rounding
            ) < 0:
                move.quantity = move.product_uom_qty
        pickings.with_context(
            skip_sms=True,
            skip_backorder=True,
            channel_skip_fulfillment_push=skip_fulfillment_push,
        ).button_validate()

    def _register_payments(self, invoices):
        for invoice in invoices.filtered(lambda m: m.amount_residual):
            self.env["account.payment.register"].with_context(
                active_model="account.move", active_ids=invoice.ids
            ).create(
                {
                    "journal_id": self.payment_journal_id.id,
                    "payment_date": fields.Date.context_today(self),
                }
            )._create_payments()

    def _default_vals_for_store(self, store):
        """Values of the workflow created for a new store."""
        return {
            "name": _("Paid orders"),
            "store_id": store.id,
            "payment_journal_id": self.env["account.journal"]
            .search(
                [("type", "=", "bank"), ("company_id", "=", store.company_id.id)], limit=1
            )
            .id,
        }

