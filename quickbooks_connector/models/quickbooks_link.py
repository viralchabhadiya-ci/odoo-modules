from odoo import api, fields, models

ENTITIES = [
    ("Customer", "Customer"),
    ("Vendor", "Vendor"),
    ("Item", "Item"),
    ("Invoice", "Invoice"),
    ("CreditMemo", "Credit memo"),
    ("Bill", "Bill"),
    ("VendorCredit", "Vendor credit"),
    ("Payment", "Payment"),
    ("BillPayment", "Bill payment"),
]
MAX_ATTEMPTS = 3


class QuickBooksLink(models.Model):
    """One Odoo record and its twin in QuickBooks, with what was last sent and why a send failed."""

    _name = "quickbooks.link"
    _description = "QuickBooks Link"
    _order = "write_date desc, id desc"

    account_id = fields.Many2one("quickbooks.account", required=True, ondelete="cascade", index=True)
    entity = fields.Selection(ENTITIES, required=True)
    res_model = fields.Char(required=True)
    res_id = fields.Integer(required=True)
    record_name = fields.Char(string="Odoo record", compute="_compute_record_name")
    remote_id = fields.Char(string="QuickBooks id", index=True)
    sync_token = fields.Char(help="QuickBooks' version number of the record; needed to update it.")
    payload_hash = fields.Char(help="Fingerprint of what Odoo last sent or received, so unchanged records are skipped.")
    last_sync = fields.Datetime()
    error = fields.Text()
    attempts = fields.Integer(
        default=0, help="Failed attempts in a row. After a few the record waits for a change or for Retry."
    )
    note = fields.Char(help="A warning, e.g. when QuickBooks computed a different total.")

    _unique_record = models.Constraint(
        "unique (account_id, entity, res_model, res_id)", "This record already has a QuickBooks link."
    )

    @api.depends("res_model", "res_id")
    def _compute_record_name(self):
        for link in self:
            record = link.env[link.res_model].browse(link.res_id) if link.res_model in link.env else None
            link.record_name = record.exists().display_name if record else False

    def action_open_record(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": self.res_model,
            "res_id": self.res_id,
            "view_mode": "form",
        }

    def action_retry(self):
        self.write({"attempts": 0})
