import json

from odoo import api, fields, models


class ChannelMapping(models.Model):
    """Links an Odoo record to its twin on the sales channel."""

    _name = "channel.mapping"
    _description = "Sales Channel Mapping"
    _order = "id desc"

    store_id = fields.Many2one(
        "channel.store", required=True, ondelete="cascade", index=True
    )
    res_model = fields.Selection(
        [
            ("product.template", "Product"),
            ("product.product", "Variant"),
            ("res.partner", "Customer"),
        ],
        required=True,
    )
    res_id = fields.Many2oneReference(model_field="res_model", required=True)
    external_id = fields.Char(required=True, index=True)
    external_ref = fields.Char(
        help="Second identifier needed by the channel, e.g. the inventory item of a variant.",
    )
    push_stock = fields.Boolean(
        string="Push Stock",
        default=True,
        help="Untick to stop sending the stock of this variant to the channel.",
    )
    last_stock = fields.Text(
        help="JSON {location external id: quantity} last sent to the channel.",
        copy=False,
    )
    record_name = fields.Char(compute="_compute_record_name")

    _store_model_external_unique = models.Constraint(
        "UNIQUE (store_id, res_model, external_id)",
        "This record is already linked to the store.",
    )

    @api.depends("res_model", "res_id")
    def _compute_record_name(self):
        for mapping in self:
            record = self.env[mapping.res_model].browse(mapping.res_id).exists()
            mapping.record_name = record.display_name if record else False

    def get_last_stock(self):
        self.ensure_one()
        try:
            return json.loads(self.last_stock or "{}")
        except ValueError:
            return {}
