from odoo import fields, models


class ChannelSetupWizard(models.TransientModel):
    _inherit = "channel.setup.wizard"

    ebay_refresh_token = fields.Char(string="Refresh token")
    ebay_sandbox = fields.Boolean(string="Sandbox (testing)")

    def _prepare_store_vals(self):
        vals = super()._prepare_store_vals()
        if self.channel_type == "ebay":
            vals.update(
                ebay_refresh_token=(self.ebay_refresh_token or "").strip(),
                ebay_sandbox=self.ebay_sandbox,
            )
        return vals
