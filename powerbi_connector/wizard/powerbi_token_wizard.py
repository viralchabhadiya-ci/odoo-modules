from odoo import fields, models


class PowerBITokenWizard(models.TransientModel):
    """Shows the new access key once. The form is never saved, so the key is not stored anywhere in clear."""

    _name = "powerbi.token.wizard"
    _description = "Power BI Access Key"

    feed_id = fields.Many2one("powerbi.feed", readonly=True)
    url = fields.Char(string="Address", readonly=True)
    token = fields.Char(string="Access key", readonly=True)
