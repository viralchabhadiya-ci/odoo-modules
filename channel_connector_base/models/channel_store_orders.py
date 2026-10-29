import logging
from datetime import datetime, timedelta, timezone

from odoo import Command, _, fields, models
from odoo.tools import float_is_zero

from .channel_error import ChannelError

_logger = logging.getLogger(__name__)


def parse_datetime(value):
    """ISO 8601 string (with or without offset) to a naive UTC datetime."""
    if not value:
        return fields.Datetime.now()
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class ChannelStore(models.Model):
    _inherit = "channel.store"

    # -- fetching ------------------------------------------------------------------

    def _import_orders(self):
        """Queue one job per order changed since the last run. Returns how many."""
        self.ensure_one()
        started = fields.Datetime.now()
        since = self.last_order_sync or self.import_orders_from
        count = 0
        for data in self._fetch_orders(since):
            job = self._queue_job(
                "import_order",
                _("Import order %s", data.get("name") or data["external_id"]),
                {"order": data},
                dedupe_key=f"order:{data['external_id']}:{data.get('updated_at')}",
                external_ref=data.get("name"),
            )
            count += bool(job)
        # five minutes of overlap so an order saved while we were fetching is not missed
        self.last_order_sync = started - timedelta(minutes=5)
        return count

    def _job_import_order(self, job):
        payload = job.get_payload()
        data = payload.get("order")
        if not data:
            data = self._fetch_order(payload["external_id"])
        if not data:
            return None
        return self._process_order_data(data)

    # -- order creation ---------------------------------------------------------------

    def _process_order_data(self, data):
        self.ensure_one()
        order = self.env["sale.order"].search(
            [
                ("channel_store_id", "=", self.id),
                ("channel_order_id", "=", str(data["external_id"])),
            ],
            limit=1,
        )
        if order:
            self._update_existing_order(order, data)
            return order
        if data.get("cancelled"):
            return None
        if data.get("date") and parse_datetime(data["date"]) < (
            self.import_orders_from or datetime.min
        ):
            return None
        order = self._create_order(data)
        self._update_existing_order(order, data, new=True)
        return order

    def _create_order(self, data):
        partner, invoice_partner, shipping_partner = self._get_order_partners(data)
        lines = self._prepare_order_lines(data)
        currency = self.env["res.currency"].search(
            [("name", "=", data.get("currency"))], limit=1
        ) or self.company_id.currency_id
        pricelist = self.pricelist_id
        if not pricelist or pricelist.currency_id != currency:
            pricelist = self.env["product.pricelist"].search(
                [
                    ("currency_id", "=", currency.id),
                    ("company_id", "in", (False, self.company_id.id)),
                ],
                limit=1,
            ) or pricelist
        vals = {
            "partner_id": partner.id,
            "partner_invoice_id": invoice_partner.id,
            "partner_shipping_id": shipping_partner.id,
            "channel_store_id": self.id,
            "channel_order_id": str(data["external_id"]),
            "channel_order_name": data.get("name"),
            "client_order_ref": data.get("name"),
            "date_order": parse_datetime(data.get("date")),
            "company_id": self.company_id.id,
            "warehouse_id": (
                (self.fulfilled_warehouse_id or self.warehouse_id)
                if data.get("channel_fulfilled")
                else self.warehouse_id
            ).id,
            "team_id": self.team_id.id,
            "note": data.get("note") or False,
            "order_line": [Command.create(line) for line in lines],
        }
        if pricelist:
            vals["pricelist_id"] = pricelist.id
        vals.update(self._order_extra_vals(data))
        order = self.env["sale.order"].with_company(self.company_id).create(vals)
        if data.get("note"):
            order.message_post(body=_("Customer note: %s", data["note"]))
        return order

    def _update_existing_order(self, order, data, new=False):
        """Apply status changes coming from the channel to an Odoo order."""
        self.ensure_one()
        if new and data.get("channel_fulfilled") and not data.get("cancelled"):
            self._deliver_channel_fulfilled(order)
        if new and not data.get("cancelled"):
            workflow = self._find_workflow(data.get("gateways"))
            if workflow:
                try:
                    with self.env.cr.savepoint():
                        workflow._apply(order, data)
                except Exception as exc:  # noqa: BLE001 - the order itself is kept
                    _logger.warning("Workflow failed for %s", order.name, exc_info=True)
                    order.message_post(
                        body=_(
                            "The order was imported but the automatic workflow '%(wf)s' failed: %(error)s",
                            wf=workflow.name,
                            error=exc,
                        )
                    )
        if data.get("cancelled") and order.state not in ("cancel", "done"):
            self._cancel_order(order)
        for refund in data.get("refunds") or []:
            self._process_refund(order, refund)

    def _deliver_channel_fulfilled(self, order):
        """The channel already shipped this order: confirm it and book the delivery, no shipment is sent back."""
        if order.state in ("draft", "sent"):
            order.action_confirm()
        self.env["channel.workflow"]._validate_pickings(order, skip_fulfillment_push=True)

    def _cancel_order(self, order):
        done_pickings = order.picking_ids.filtered(lambda p: p.state == "done")
        if done_pickings:
            order.message_post(
                body=_("Cancelled on the channel, but some deliveries are already done. Review manually.")
            )
            return
        order.invoice_ids.filtered(lambda m: m.state == "draft").button_cancel()
        if order.invoice_ids.filtered(lambda m: m.state == "posted"):
            order.message_post(
                body=_("Cancelled on the channel, but an invoice is already posted. Create a credit note manually.")
            )
            return
        order._action_cancel()
        order.message_post(body=_("Order cancelled on the channel."))

    # -- partners ---------------------------------------------------------------------

    def _get_order_partners(self, data):
        Partner = self.env["res.partner"].with_company(self.company_id)
        billing = data.get("billing") or {}
        shipping = data.get("shipping") or billing
        email = (data.get("email") or "").strip().lower()
        customer_id = data.get("customer_id")
        partner = self.env["res.partner"]
        if customer_id:
            partner = self._get_mapped("res.partner", customer_id)
        if not partner and email:
            partner = Partner.search(
                [("email", "=ilike", email), ("parent_id", "=", False)], limit=1
            )
        if not partner:
            name = (
                billing.get("name")
                or shipping.get("name")
                or email
                or _("Guest %s", data.get("name") or "")
            )
            partner = Partner.create(
                {
                    "name": billing.get("company") or name,
                    "is_company": bool(billing.get("company")),
                    "email": email or False,
                    "phone": data.get("phone") or billing.get("phone") or False,
                    "company_id": self.company_id.id,
                    "customer_rank": 1,
                    **self._address_vals(billing),
                }
            )
        if customer_id:
            self._set_mapping(partner, customer_id)
        invoice_partner = partner
        shipping_partner = partner
        if shipping and self._address_differs(partner, shipping):
            shipping_partner = self._find_or_create_child(partner, shipping, "delivery")
        return partner, invoice_partner, shipping_partner

    def _address_vals(self, address):
        if not address:
            return {}
        country = self.env["res.country"].search(
            [("code", "=", (address.get("country_code") or "").upper())], limit=1
        )
        state = self.env["res.country.state"]
        if country and address.get("state_code"):
            state = state.search(
                [("country_id", "=", country.id), ("code", "=ilike", address["state_code"])],
                limit=1,
            ) or state.search(
                [("country_id", "=", country.id), ("name", "=ilike", address["state_code"])],
                limit=1,
            )
        return {
            "street": address.get("street") or False,
            "street2": address.get("street2") or False,
            "city": address.get("city") or False,
            "zip": address.get("zip") or False,
            "country_id": country.id or False,
            "state_id": state.id or False,
        }

    @staticmethod
    def _norm(value):
        return (value or "").strip().lower()

    def _address_differs(self, partner, address):
        return any(
            self._norm(partner[field]) != self._norm(address.get(key))
            for field, key in (("street", "street"), ("zip", "zip"), ("city", "city"))
        )

    def _find_or_create_child(self, partner, address, address_type):
        for child in partner.child_ids.filtered(lambda c: c.type == address_type):
            if not self._address_differs(child, address):
                return child
        vals = {
            "parent_id": partner.id,
            "type": address_type,
            "name": address.get("name") or partner.name,
            "phone": address.get("phone") or False,
            "company_id": self.company_id.id,
            **self._address_vals(address),
        }
        return self.env["res.partner"].with_company(self.company_id).create(vals)

    # -- lines --------------------------------------------------------------------------

    def _prepare_order_lines(self, data):
        taxes_included = bool(data.get("taxes_included"))
        lines = []
        for line in data.get("lines") or []:
            product = self._find_product(line)
            if not product:
                if not self.create_missing_products:
                    raise ChannelError(
                        _(
                            "Product '%(name)s' (SKU %(sku)s) does not exist in Odoo. "
                            "Import products first, or switch on 'Create missing products' in the store settings.",
                            name=line.get("name"),
                            sku=line.get("sku") or "-",
                        )
                    )
                product = self._create_product_from_line(line)
            lines.append(self._line_vals(product, line, taxes_included))
        for shipping in data.get("shipping_lines") or []:
            if not self.shipping_product_id:
                raise ChannelError(_("Set a shipping product in the store settings."))
            lines.append(
                self._line_vals(
                    self.shipping_product_id,
                    {
                        "name": shipping.get("title") or _("Shipping"),
                        "qty": 1,
                        "price": shipping.get("price", 0.0),
                        "taxes": shipping.get("taxes"),
                    },
                    taxes_included,
                )
            )
        if not lines:
            raise ChannelError(_("Order %s has no lines.", data.get("name")))
        return lines

    def _line_vals(self, product, line, taxes_included):
        taxes = self._find_taxes(line.get("taxes") or [])
        price = float(line.get("price") or 0.0)
        if taxes:
            tax_included_in_odoo = all(t.price_include for t in taxes)
            rate = sum(t.amount for t in taxes if t.amount_type == "percent") / 100
            if taxes_included and not tax_included_in_odoo:
                price = price / (1 + rate)
            elif not taxes_included and tax_included_in_odoo:
                price = price * (1 + rate)
        discount = 0.0
        if line.get("discounted_price") is not None and price:
            full = float(line.get("price") or 0.0)
            if full:
                discount = max(0.0, (full - float(line["discounted_price"])) / full * 100)
        vals = {
            "product_id": product.id,
            "name": line.get("name") or product.display_name,
            "product_uom_qty": float(line.get("qty") or 1),
            "price_unit": price,
            "discount": discount,
            "tax_ids": [Command.set(taxes.ids)],
            "channel_line_id": line.get("external_id") and str(line["external_id"]),
        }
        return vals

    def _find_taxes(self, tax_lines):
        taxes = self.env["account.tax"]
        for tax_line in tax_lines:
            rate = float(tax_line.get("rate") or 0.0)
            if float_is_zero(rate, precision_digits=4):
                continue
            candidates = self.env["account.tax"].search(
                [
                    ("type_tax_use", "=", "sale"),
                    ("amount_type", "=", "percent"),
                    ("amount", "=", rate),
                    ("company_id", "=", self.company_id.id),
                ]
            )
            # a tax-excluded twin wins over a tax-included one
            tax = candidates.sorted(lambda t: (t.price_include, t.sequence, t.id))[:1]
            taxes |= tax
        return taxes

    # -- credit notes ----------------------------------------------------------------------

    def _process_refund(self, order, refund):
        refund_id = str(refund["external_id"])
        Move = self.env["account.move"]
        if Move.search_count([("channel_refund_id", "=", refund_id), ("channel_store_id", "=", self.id)]):
            return
        invoice = order.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted"
        )[:1]
        if not invoice:
            order.message_post(
                body=_(
                    "Refund %(id)s of %(amount)s was issued on the channel but the order has no posted invoice. "
                    "Create the credit note manually once the order is invoiced.",
                    id=refund_id,
                    amount=refund.get("amount"),
                )
            )
            return
        credit_note = invoice._reverse_moves(
            [
                {
                    "ref": _("Refund %(id)s for %(order)s", id=refund_id, order=order.channel_order_name),
                    "date": fields.Date.context_today(self),
                    "channel_refund_id": refund_id,
                    "channel_store_id": self.id,
                }
            ]
        )
        self._shape_credit_note(credit_note, invoice, refund)
        if self.refund_post:
            credit_note.action_post()
        order.message_post(body=_("Credit note %s created from a channel refund.", credit_note.display_name))

    def _shape_credit_note(self, credit_note, invoice, refund):
        """Keep only the refunded quantities; fall back to one adjustment line for amount-only refunds."""
        refund_lines = refund.get("lines") or []
        product_lines = credit_note.invoice_line_ids.filtered(lambda l: l.display_type == "product")
        if refund_lines:
            kept = self.env["account.move.line"]
            for ref_line in refund_lines:
                for line in product_lines - kept:
                    by_line = ref_line.get("line_id") and str(ref_line["line_id"]) in line.sale_line_ids.mapped("channel_line_id")
                    by_sku = (
                        not ref_line.get("line_id")
                        and ref_line.get("sku")
                        and line.product_id.default_code == ref_line["sku"]
                    )
                    if by_line or by_sku:
                        line.quantity = float(ref_line.get("qty") or 0)
                        kept |= line
                        break
            (product_lines - kept).unlink()
            if kept:
                return
        if not self.adjustment_product_id:
            return
        product_lines.unlink()
        credit_note.write(
            {
                "invoice_line_ids": [
                    Command.create(
                        {
                            "product_id": self.adjustment_product_id.id,
                            "name": _("Refund from sales channel"),
                            "quantity": 1,
                            "price_unit": float(refund.get("amount") or 0.0),
                            "tax_ids": [Command.clear()],
                        }
                    )
                ]
            }
        )
