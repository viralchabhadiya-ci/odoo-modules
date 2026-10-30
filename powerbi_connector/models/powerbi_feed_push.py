import json
import logging
from datetime import timedelta

from odoo import api, fields, models

from .powerbi_client import PowerBIClient, PowerBIError

_logger = logging.getLogger(__name__)

DATA_TYPES = {
    "Edm.Int64": "Int64",
    "Edm.Double": "Double",
    "Edm.Boolean": "Boolean",
    "Edm.String": "String",
    "Edm.Date": "DateTime",
    "Edm.DateTimeOffset": "DateTime",
}
ROWS_PER_CALL = 5000  # Power BI accepts 10000; this keeps each call small


class PowerBIFeedPush(models.Model):
    """Optional second way to reach Power BI: Odoo sends a copy of the tables to a Power BI push dataset.
    Use it when Odoo cannot be reached from the internet."""

    _inherit = "powerbi.feed"

    push_enabled = fields.Boolean(string="Also send a copy to the Power BI service")
    pbi_tenant_id = fields.Char(string="Tenant ID")
    pbi_client_id = fields.Char(string="Application (client) ID")
    pbi_client_secret = fields.Char(string="Client secret", groups="base.group_system", copy=False)
    pbi_workspace_id = fields.Char(
        string="Workspace ID", help="The long ID in the workspace address: .../groups/<this>/..."
    )
    pbi_dataset_id = fields.Char(string="Dataset ID", readonly=True, copy=False)
    pbi_schema = fields.Text(readonly=True, copy=False)
    push_interval = fields.Selection(
        [("1", "Every hour"), ("6", "Every 6 hours"), ("24", "Every day"), ("0", "Only when I press the button")],
        string="Send",
        default="24",
        required=True,
    )
    last_push = fields.Datetime(readonly=True, copy=False)
    push_message = fields.Char(readonly=True, copy=False)

    # -- running ---------------------------------------------------------------------------------

    def action_push_now(self):
        self.ensure_one()
        try:
            rows = self._push()
        except PowerBIError as exc:
            self.push_message = str(exc)
            return self._notify(self.env._("Could not send to Power BI"), str(exc), "danger")
        return self._notify(
            self.env._("Sent to Power BI"), self.env._("%s rows are now in the dataset.", rows), "success"
        )

    @staticmethod
    def _notify(title, message, kind):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": kind, "sticky": kind != "success"},
        }

    @api.model
    def _cron_push(self):
        now = fields.Datetime.now()
        for feed in self.search([("push_enabled", "=", True), ("push_interval", "!=", "0")]):
            due = feed.last_push and feed.last_push + timedelta(hours=int(feed.push_interval) - 0.1)
            if due and due > now:
                continue
            try:
                with self.env.cr.savepoint():
                    feed._push()
            except PowerBIError as exc:
                feed.push_message = str(exc)
            except Exception:
                _logger.exception("Power BI push of feed %s failed", feed.id)
                feed.push_message = self.env._("Unexpected problem, see the Odoo log.")

    def _push(self):
        """Replace the dataset's tables with the current rows. Returns the number of rows sent."""
        self.ensure_one()
        client = PowerBIClient(self)
        env = self._user_env()
        entities = self._visible_entities(env)
        columns = {name: entity._columns(env) for name, entity in entities.items()}
        schema = {
            name: [{"name": c["name"], "dataType": DATA_TYPES[c["edm"]]} for c in cols]
            for name, cols in columns.items()
        }
        if not schema:
            raise PowerBIError(self.env._("The feed has no tables the user may read."))
        dataset_id = self._ensure_dataset(client, schema)
        total = 0
        for name, entity in entities.items():
            client.clear_table(dataset_id, name)
            domain, offset = entity._base_domain(env), 0
            dates = {c["name"] for c in columns[name] if c["edm"] == "Edm.Date"}
            while True:
                rows = entity._read_rows(env, columns[name], domain, "id", offset, ROWS_PER_CALL)
                if rows:
                    for row in rows:
                        for key in dates:
                            if row[key]:
                                row[key] += "T00:00:00Z"
                    client.add_rows(dataset_id, name, rows)
                total += len(rows)
                if len(rows) < ROWS_PER_CALL:
                    break
                offset += ROWS_PER_CALL
        self.write({"last_push": fields.Datetime.now(), "push_message": False})
        return total

    def _ensure_dataset(self, client, schema):
        """Create the dataset, or bring its tables up to date without changing its ID (reports stay connected).
        Only a new or removed table needs a new dataset, because Power BI cannot add tables to a push dataset."""
        stored = json.loads(self.pbi_schema or "{}")
        tables = [{"name": name, "columns": cols} for name, cols in schema.items()]
        if self.pbi_dataset_id and set(stored) == set(schema):
            for name, cols in schema.items():
                if stored[name] != cols:
                    client.update_table(self.pbi_dataset_id, {"name": name, "columns": cols})
            self.pbi_schema = json.dumps(schema, sort_keys=True)
            return self.pbi_dataset_id
        if self.pbi_dataset_id:
            try:
                client.delete_dataset(self.pbi_dataset_id)
            except PowerBIError as exc:
                _logger.info("Old Power BI dataset of feed %s was not removed: %s", self.id, exc)
        dataset_id = client.create_dataset(self.name, tables)
        self.write({"pbi_dataset_id": dataset_id, "pbi_schema": json.dumps(schema, sort_keys=True)})
        return dataset_id
