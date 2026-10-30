from odoo import fields, models

from ..models.channel_store import REGIONS


class ChannelSetupWizard(models.TransientModel):
    _inherit = "channel.setup.wizard"

    amazon_region = fields.Selection(REGIONS, string="Amazon region", default="eu")
    amazon_seller_id = fields.Char(string="Seller ID")
    amazon_refresh_token = fields.Char(string="Refresh token")

    def _prepare_store_vals(self):
        vals = super()._prepare_store_vals()
        if self.channel_type == "amazon":
            vals.update(
                amazon_region=self.amazon_region,
                amazon_seller_id=(self.amazon_seller_id or "").strip(),
                amazon_refresh_token=(self.amazon_refresh_token or "").strip(),
            )
        return vals
