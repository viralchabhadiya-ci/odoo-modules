import re
from datetime import datetime, timezone

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.fields import Domain
from odoo.tools.safe_eval import safe_eval

from .odata_filter import ODataError

EDM = {
    "integer": "Edm.Int64",
    "float": "Edm.Double",
    "monetary": "Edm.Double",
    "boolean": "Edm.Boolean",
    "date": "Edm.Date",
    "datetime": "Edm.DateTimeOffset",
}
TEXT = ("char", "text", "html")
SKIPPED_PREFIXES = ("message_", "activity_", "my_activity_", "website_message_", "access_", "rating_", "__")


def to_utc_text(value):
    """'2026-10-29T12:00:00+02:00' -> '2026-10-29 10:00:00', the form the ORM compares datetimes in."""
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def converter(kind):
    """Turn a filter literal into the value the ORM expects; None stays None."""
    convert = {
        "integer": int,
        "float": float,
        "boolean": bool,
        "datetime": to_utc_text,
    }.get(kind, lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v))
    return lambda value: None if value is None else convert(value)


def cell(column, value):
    """One Odoo value as it goes into the JSON feed."""
    kind = column["kind"]
    if kind == "m2o_id":
        return value[0] if value else None
    if kind == "m2o_name":
        return value[1] if value else None
    if kind == "label":
        return column["labels"].get(value) if value else None
    if kind == "date":
        return value.isoformat() if value else None
    if kind == "datetime":
        return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None
    if kind in ("integer", "id"):
        return int(value) if value is not None and value is not False else None
    if kind in ("float", "monetary"):
        return float(value) if value is not None and value is not False else None
    if kind == "boolean":
        return bool(value)
    return value or None


class PowerBIEntity(models.Model):
    """One table of the feed: an Odoo model, the columns to expose and an optional filter."""

    _name = "powerbi.entity"
    _description = "Power BI Table"
    _order = "feed_id, sequence, id"

    feed_id = fields.Many2one("powerbi.feed", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    model_id = fields.Many2one(
        "ir.model", string="Data", required=True, ondelete="cascade", domain="[('transient', '=', False)]"
    )
    model_name = fields.Char(related="model_id.model")
    name = fields.Char(
        string="Table name", required=True, help="The name Power BI shows. Letters, digits and underscores."
    )
    field_ids = fields.Many2many(
        "ir.model.fields",
        string="Columns",
        domain="[('model_id', '=', model_id), ('store', '=', True), "
        "('ttype', 'not in', ['one2many', 'many2many', 'binary', 'json', 'properties', 'reference', 'many2one_reference'])]",
        help="Leave empty for every ordinary column. Relations show the id and the name; choices show the code and the label.",
    )
    domain = fields.Char(string="Only rows matching", default="[]")

    _unique_name = models.Constraint("unique (feed_id, name)", "Two tables of a feed cannot have the same name.")

    @api.onchange("model_id")
    def _onchange_model_id(self):
        if self.model_id and not self.name:
            self.name = self.model_id.model.replace(".", "_")
        self.field_ids = False
        self.domain = "[]"

    @api.constrains("name")
    def _check_name(self):
        for entity in self:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", entity.name or ""):
                raise ValidationError(
                    self.env._(
                        "A table name may only hold letters, digits and underscores, and cannot start with a digit."
                    )
                )

    @api.constrains("domain")
    def _check_domain(self):
        for entity in self:
            try:
                Domain(safe_eval(entity.domain or "[]"))
            except Exception as exc:
                raise ValidationError(
                    self.env._("The filter of table %(name)s is not valid: %(error)s", name=entity.name, error=exc)
                ) from exc

    # -- what is exposed -------------------------------------------------------------------------

    def _columns(self, env):
        """Columns as the feed user may see them: [{name, edm, field, kind, ...}], `id` first."""
        self.ensure_one()
        Model = env[self.model_name]
        allowed = Model.fields_get(attributes=["type", "selection"])
        chosen = self.field_ids.mapped("name")
        names = chosen or [
            name
            for name, field in Model._fields.items()
            if field.store and not name.startswith(SKIPPED_PREFIXES) and name not in ("id", "create_uid", "write_uid")
        ]
        columns = [{"name": "id", "edm": "Edm.Int64", "field": "id", "kind": "id", "nullable": False}]
        for name in names:
            field = Model._fields.get(name)
            if not field or name == "id" or name not in allowed or not field.store:
                continue
            kind = field.type
            if kind == "many2one":
                columns.append({"name": name, "edm": "Edm.Int64", "field": name, "kind": "m2o_id"})
                columns.append({"name": f"{name}_name", "edm": "Edm.String", "field": name, "kind": "m2o_name"})
            elif kind == "selection":
                labels = dict(allowed[name].get("selection") or [])
                columns.append({"name": name, "edm": "Edm.String", "field": name, "kind": "char"})
                columns.append(
                    {"name": f"{name}_label", "edm": "Edm.String", "field": name, "kind": "label", "labels": labels}
                )
            elif kind in EDM:
                columns.append({"name": name, "edm": EDM[kind], "field": name, "kind": kind})
            elif kind in TEXT:
                columns.append({"name": name, "edm": "Edm.String", "field": name, "kind": "char"})
        return columns

    def _resolver(self, env, columns):
        """Map a column of the feed back to the Odoo field a filter or sort must use."""
        by_name = {column["name"]: column for column in columns}
        Model = env[self.model_name]

        def resolve(name):
            column = by_name.get(name)
            if not column:
                raise ODataError(f"There is no column '{name}' in table {self.name}.")
            field, kind = column["field"], column["kind"]
            if kind == "m2o_name":
                return f"{field}.{env[Model._fields[field].comodel_name]._rec_name}", converter("char")
            if kind == "label":
                reverse = {label: code for code, label in column["labels"].items()}
                return field, lambda value: None if value is None else reverse.get(value, value)
            return field, converter("integer" if kind in ("m2o_id", "id") else kind)

        return resolve

    def _base_domain(self, env):
        return Domain(safe_eval(self.domain or "[]", {"uid": env.uid}))

    def _read_rows(self, env, columns, domain, order, offset, limit):
        Model = env[self.model_name]
        records = Model.search(domain, order=order, offset=offset, limit=limit)
        field_names = list(dict.fromkeys(column["field"] for column in columns if column["field"] != "id"))
        data = {row["id"]: row for row in records.read(field_names)} if field_names else {}
        rows = []
        for record in records:
            values = data.get(record.id, {})
            rows.append(
                {c["name"]: cell(c, record.id if c["field"] == "id" else values.get(c["field"])) for c in columns}
            )
        return rows
