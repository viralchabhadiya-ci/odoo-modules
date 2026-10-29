from odoo import fields, models


class ChannelLocation(models.Model):
    """A stock location of the channel (e.g. a Shopify location), tied to a warehouse."""

    _name = "channel.location"
    _description = "Sales Channel Location"
    _order = "name"

    store_id = fields.Many2one(
        "channel.store", required=True, ondelete="cascade", index=True
    )
    name = fields.Char(required=True)
    external_id = fields.Char(required=True)
    warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Odoo Warehouse",
        domain="[('company_id', '=', company_id)]",
        help="Stock of this warehouse is sent to this channel location. "
        "Leave empty to leave the location untouched.",
    )
    company_id = fields.Many2one(related="store_id.company_id")

    _store_external_unique = models.Constraint(
        "UNIQUE (store_id, external_id)", "This location already exists for the store."
    )
