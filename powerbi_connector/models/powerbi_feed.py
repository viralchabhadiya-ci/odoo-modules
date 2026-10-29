import base64
import hashlib
import hmac
import json
import secrets
from urllib.parse import urlencode
from xml.sax.saxutils import quoteattr

from odoo import api, fields, models
from odoo.exceptions import UserError

from .odata_filter import ODataError, parse_filter

DEFAULT_PAGE = 5000
TOUCH_EVERY = 300  # seconds between "last used" writes

PRESETS = {
    "sales": [
        (
            "res.partner",
            "customers",
            [("customer_rank", ">", 0)],
            "name email phone city country_id company_id customer_rank create_date",
        ),
        (
            "product.product",
            "products",
            [],
            "name default_code barcode categ_id list_price standard_price type active create_date",
        ),
        (
            "sale.order",
            "sales_orders",
            [],
            "name date_order partner_id user_id team_id company_id state invoice_status currency_id amount_untaxed amount_tax amount_total create_date",
        ),
        (
            "sale.order.line",
            "sales_order_lines",
            [],
            "order_id order_partner_id product_id name product_uom_qty qty_delivered qty_invoiced price_unit discount price_subtotal price_total state salesman_id create_date",
        ),
    ],
    "accounting": [
        (
            "account.move",
            "invoices",
            [("move_type", "in", ["out_invoice", "out_refund", "in_invoice", "in_refund"])],
            "name move_type invoice_date invoice_date_due partner_id company_id state payment_state currency_id amount_untaxed amount_tax amount_total amount_residual invoice_user_id create_date",
        ),
        (
            "account.move.line",
            "invoice_lines",
            [("display_type", "=", "product")],
            "move_id partner_id product_id name quantity price_unit discount price_subtotal price_total date company_id parent_state",
        ),
        (
            "account.payment",
            "payments",
            [],
            "name date partner_id partner_type payment_type amount currency_id journal_id state company_id",
        ),
    ],
    "inventory": [
        (
            "stock.quant",
            "stock_levels",
            [("location_id.usage", "=", "internal")],
            "product_id location_id lot_id quantity reserved_quantity company_id in_date",
        ),
        (
            "stock.move",
            "stock_moves",
            [("state", "=", "done")],
            "product_id product_uom_qty quantity date location_id location_dest_id picking_id company_id",
        ),
        ("stock.warehouse", "warehouses", [], "name code company_id"),
    ],
    "purchases": [
        (
            "purchase.order",
            "purchase_orders",
            [],
            "name date_order partner_id user_id company_id state invoice_status currency_id amount_untaxed amount_tax amount_total create_date",
        ),
        (
            "purchase.order.line",
            "purchase_order_lines",
            [],
            "order_id partner_id product_id name product_qty qty_received qty_invoiced price_unit price_subtotal price_total date_planned state",
        ),
    ],
}


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def token_from_header(authorization):
    """Power BI sends Basic credentials (any user name, the access key as password); scripts may send Bearer."""
    scheme, _sep, value = (authorization or "").partition(" ")
    value = value.strip()
    if scheme.lower() == "bearer":
        return value
    if scheme.lower() == "basic":
        try:
            user, _colon, password = base64.b64decode(value).decode().partition(":")
        except ValueError:
            return ""
        return password or user
    return ""


class PowerBIFeed(models.Model):
    """A read-only OData feed that Power BI reads straight from Odoo, as one Odoo user."""

    _name = "powerbi.feed"
    _description = "Power BI Feed"
    _order = "name"

    name = fields.Char(required=True, default="Power BI")
    active = fields.Boolean(default=True)
    user_id = fields.Many2one(
        "res.users",
        string="Reads data as",
        required=True,
        default=lambda self: self.env.user,
        domain="[('share', '=', False)]",
        help="Power BI sees exactly what this user may read: access rights and record rules apply.",
    )
    token_hash = fields.Char(groups="base.group_system", copy=False)
    token_hint = fields.Char(string="Access key", readonly=True, copy=False, help="The end of the current access key.")
    token_expiry = fields.Date(string="Key valid until", help="Leave empty for a key that does not expire.")
    page_size = fields.Integer(default=DEFAULT_PAGE, help="Rows per answer. Power BI asks for the next page by itself.")
    entity_ids = fields.One2many("powerbi.entity", "feed_id", string="Tables")
    entity_count = fields.Integer(compute="_compute_entity_count")
    last_used = fields.Datetime(readonly=True, copy=False)
    url = fields.Char(compute="_compute_url", string="Address for Power BI")

    @api.depends("entity_ids")
    def _compute_entity_count(self):
        for feed in self:
            feed.entity_count = len(feed.entity_ids)

    @api.depends("name")
    def _compute_url(self):
        for feed in self:
            feed.url = f"{feed.get_base_url()}/powerbi/{self.env.cr.dbname}/odata/{feed.id}" if feed.id else False

    # -- access key ----------------------------------------------------------------------------

    def action_new_token(self):
        """A new key replaces the old one at once. It is shown once and only its hash is kept."""
        self.ensure_one()
        token = secrets.token_urlsafe(32)
        self.sudo().write({"token_hash": hash_token(token), "token_hint": f"…{token[-4:]}"})
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Connect Power BI"),
            "res_model": "powerbi.token.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_feed_id": self.id, "default_url": self.url, "default_token": token},
        }

    def action_revoke_token(self):
        self.sudo().write({"token_hash": False, "token_hint": False})

    @api.model
    def _authenticate(self, feed_id, authorization):
        """The feed if the key is right and still valid, else an empty recordset."""
        feed = self.sudo().browse(feed_id).exists()
        token = token_from_header(authorization)
        if not (feed and feed.active and feed.token_hash and token):
            return self.browse()
        if feed.token_expiry and feed.token_expiry < fields.Date.today():
            return self.browse()
        if not hmac.compare_digest(feed.token_hash, hash_token(token)):
            return self.browse()
        if not feed.user_id.active or feed.user_id.share:
            return self.browse()
        return feed

    def _touch(self):
        now = fields.Datetime.now()
        if not self.last_used or (now - self.last_used).total_seconds() > TOUCH_EVERY:
            self.last_used = now

    # -- presets -----------------------------------------------------------------------------------

    def _add_preset(self, key):
        self.ensure_one()
        Entity, Fields, IrModel = self.env["powerbi.entity"], self.env["ir.model.fields"], self.env["ir.model"]
        have = set(self.entity_ids.mapped("name"))
        added = 0
        for model, name, domain, field_names in PRESETS[key]:
            ir_model = IrModel.search([("model", "=", model)], limit=1)
            if not ir_model or name in have or model not in self.env:
                continue
            wanted = field_names.split()
            ir_fields = Fields.search([("model_id", "=", ir_model.id), ("name", "in", wanted), ("store", "=", True)])
            try:
                Entity.create(
                    {
                        "feed_id": self.id,
                        "model_id": ir_model.id,
                        "name": name,
                        "field_ids": [(6, 0, ir_fields.ids)],
                        "domain": repr(domain) if self._domain_fits(model, domain) else "[]",
                    }
                )
            except Exception:  # a preset must never stop the others (a customised model without a field, say)
                continue
            added += 1
        if not added:
            raise UserError(
                self.env._("Nothing to add: those tables are already in the feed, or the app is not installed.")
            )

    def _domain_fits(self, model, domain):
        try:
            self.env[model].search_count(domain, limit=1)
        except Exception:
            return False
        return True

    def action_add_sales(self):
        self._add_preset("sales")

    def action_add_accounting(self):
        self._add_preset("accounting")

    def action_add_inventory(self):
        self._add_preset("inventory")

    def action_add_purchases(self):
        self._add_preset("purchases")

    # -- OData ---------------------------------------------------------------------------------------

    def _user_env(self):
        user = self.user_id
        return self.env(
            user=user.id, context={"allowed_company_ids": user.company_ids.ids, "lang": user.lang, "tz": user.tz}
        )

    def _visible_entities(self, env):
        return {
            entity.name: entity
            for entity in self.entity_ids
            if entity.model_name in env and env[entity.model_name].has_access("read")
        }

    def _odata(self, path, args, base):
        """Answer one request: (status, content type, body). `args` are the query options, `base` this feed's URL."""
        try:
            return self._answer(path.strip("/"), args, base)
        except ODataError as exc:
            body = {"error": {"code": str(exc.status), "message": str(exc)}}
            return exc.status, "application/json;odata.metadata=minimal", json.dumps(body)

    def _answer(self, path, args, base):
        env = self._user_env()
        entities = self._visible_entities(env)
        if path == "":
            value = [{"name": name, "kind": "EntitySet", "url": name} for name in entities]
            return self._json({"@odata.context": f"{base}/$metadata", "value": value})
        if path == "$metadata":
            return 200, "application/xml", self._metadata(entities, env)
        name, _sep, rest = path.partition("/")
        entity = entities.get(name)
        if not entity:
            raise ODataError(f"There is no table '{name}' in this feed.", 404)
        if rest == "$count":
            columns = entity._columns(env)
            domain = entity._base_domain(env)
            if args.get("$filter"):
                domain &= parse_filter(args["$filter"], entity._resolver(env, columns))
            return 200, "text/plain", str(env[entity.model_name].search_count(domain))
        if rest:
            raise ODataError(f"'{rest}' is not supported.", 404)
        return self._json(self._collection(entity, env, args, base))

    @staticmethod
    def _json(body):
        return 200, "application/json;odata.metadata=minimal;charset=utf-8", json.dumps(body)

    @staticmethod
    def _property(column):
        required = "" if column.get("nullable", True) else ' Nullable="false"'
        return f"<Property Name={quoteattr(column['name'])} Type={quoteattr(column['edm'])}{required}/>"

    def _metadata(self, entities, env):
        types, sets = [], []
        for name, entity in entities.items():
            props = "".join(self._property(column) for column in entity._columns(env))
            types.append(f'<EntityType Name={quoteattr(name)}><Key><PropertyRef Name="id"/></Key>{props}</EntityType>')
            sets.append(f"<EntitySet Name={quoteattr(name)} EntityType={quoteattr('Odoo.' + name)}/>")
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>'
            f'<Schema Namespace="Odoo" xmlns="http://docs.oasis-open.org/odata/ns/edm">{"".join(types)}'
            f'<EntityContainer Name="Container">{"".join(sets)}</EntityContainer></Schema>'
            "</edmx:DataServices></edmx:Edmx>"
        )

    def _collection(self, entity, env, args, base):
        columns = entity._columns(env)
        resolve = entity._resolver(env, columns)
        domain = entity._base_domain(env)
        if args.get("$filter"):
            domain &= parse_filter(args["$filter"], resolve)
        shown = columns
        if args.get("$select"):
            wanted = {part.strip() for part in args["$select"].split(",") if part.strip()}
            unknown = wanted - {c["name"] for c in columns}
            if unknown:
                raise ODataError(f"There is no column '{sorted(unknown)[0]}' in table {entity.name}.")
            shown = [c for c in columns if c["name"] in wanted or c["name"] == "id"]
        order = self._order_clause(args.get("$orderby"), resolve)
        skip, top = self._number(args, "$skip") or 0, self._number(args, "$top")
        page = self.page_size or DEFAULT_PAGE
        take = page if top is None else min(top, page)
        rows = entity._read_rows(env, shown, domain, order, skip, take + 1) if take else []
        more = len(rows) > take
        body = {"@odata.context": f"{base}/$metadata#{entity.name}", "value": rows[:take]}
        if args.get("$count", "").lower() == "true":
            body["@odata.count"] = env[entity.model_name].search_count(domain)
        if more and (top is None or top > page):
            follow = dict(args, **{"$skip": skip + take})
            if top is None:
                follow.pop("$top", None)
            else:
                follow["$top"] = top - take
            body["@odata.nextLink"] = f"{base}/{entity.name}?{urlencode(follow, safe='$,')}"
        return body

    @staticmethod
    def _number(args, key):
        if key not in args:
            return None
        try:
            value = int(args[key])
        except ValueError:
            value = -1
        if value < 0:
            raise ODataError(f"{key} must be a whole number, zero or more.")
        return value

    @staticmethod
    def _order_clause(text, resolve):
        parts = []
        for chunk in (text or "").split(","):
            pieces = chunk.split()
            if not pieces:
                continue
            if len(pieces) > 2 or (len(pieces) == 2 and pieces[1].lower() not in ("asc", "desc")):
                raise ODataError("$orderby is a column, optionally followed by asc or desc.")
            field = resolve(pieces[0])[0].split(".")[0]
            parts.append(f"{field} {pieces[1].lower() if len(pieces) == 2 else 'asc'}")
        return ", ".join(parts + ["id"])
