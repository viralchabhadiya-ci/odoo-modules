from odoo import fields, models


class AccountTax(models.Model):
    _inherit = "account.tax"

    quickbooks_taxcode_id = fields.Many2one(
        "quickbooks.ref",
        string="QuickBooks tax code",
        domain="[('kind', '=', 'taxcode')]",
        help="Sent on invoice lines with this tax. Matched by name or rate when you press Refresh lists on the connection. "
        "Not needed for US companies, where QuickBooks works the tax out itself.",
    )


class AccountAccount(models.Model):
    _inherit = "account.account"

    quickbooks_ref_id = fields.Many2one(
        "quickbooks.ref",
        string="QuickBooks account",
        company_dependent=True,
        domain="[('kind', '=', 'account')]",
        help="Where vendor bill lines and product income go in QuickBooks. Matched by number or name when you press Refresh lists.",
    )
