from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    channel_store_id = fields.Many2one("channel.store", copy=False, index="btree_not_null")
    channel_refund_id = fields.Char(copy=False, index="btree_not_null")
