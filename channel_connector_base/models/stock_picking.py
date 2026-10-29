from odoo import _, models


class StockPicking(models.Model):
    _inherit = "stock.picking"

    def _action_done(self):
        res = super()._action_done()
        if self.env.context.get("channel_skip_fulfillment_push"):
            return res
        for picking in self:
            order = picking.sale_id
            store = order.channel_store_id
            if (
                picking.state == "done"
                and picking.picking_type_code == "outgoing"
                and store
                and store.auto_push_fulfillment
            ):
                store._queue_job(
                    "push_fulfillment",
                    _("Send shipment %(picking)s of %(order)s", picking=picking.name, order=order.channel_order_name),
                    {"picking_id": picking.id},
                    dedupe_key=f"fulfil:{picking.id}",
                    external_ref=order.channel_order_name,
                )
        return res
