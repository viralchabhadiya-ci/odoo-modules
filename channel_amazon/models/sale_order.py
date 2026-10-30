from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    amazon_marketplace_id = fields.Char(string="Amazon marketplace", readonly=True, copy=False)
