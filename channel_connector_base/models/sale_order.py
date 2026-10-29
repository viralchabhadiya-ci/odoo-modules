from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    channel_store_id = fields.Many2one(
        "channel.store", string="Sales Channel", copy=False, index="btree_not_null", readonly=True
    )
    channel_order_id = fields.Char(string="Channel Order ID", copy=False, index="btree_not_null", readonly=True)
    channel_order_name = fields.Char(string="Channel Order", copy=False, readonly=True)

    _channel_order_unique = models.Constraint(
        "UNIQUE (channel_store_id, channel_order_id)",
        "This channel order was already imported.",
    )


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    channel_line_id = fields.Char(copy=False)
