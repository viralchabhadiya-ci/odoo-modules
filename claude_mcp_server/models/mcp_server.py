import datetime
import json
import logging
import time
from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.fields import Domain

from .mcp_token import is_blocked, is_sensitive

_logger = logging.getLogger(__name__)

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_ROWS = 200
DEFAULT_ROWS = 20
MAX_IDS = 100
MAX_TEXT = 100_000
SKIPPED_PREFIXES = ("message_", "activity_", "my_activity_", "website_message_", "access_", "rating_", "__")

INSTRUCTIONS = (
    "This server gives you the data of an Odoo database as the Odoo user the access key belongs to. "
    "Start with odoo_list_models, then odoo_describe_model to learn the fields. Domains are JSON lists such as "
    '[["state", "=", "sale"], ["amount_total", ">", 1000]] (conditions are ANDed; use "|" and "&" in front for OR and AND). '
    "Dates are YYYY-MM-DD, date and times are UTC as YYYY-MM-DD HH:MM:SS. A many-to-one value is [id, name]."
)


class ToolError(Exception):
    """Reported to Claude as the tool's result, so it can correct itself."""


def json_safe(value):
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return value


def to_domain(value):
    """A JSON domain ([["a", "=", 1], "|", ...]) as an Odoo Domain."""
    if value in (None, ""):
        return Domain.TRUE
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ToolError('domain must be a JSON list, for example [["state", "=", "sale"]].') from exc
    if not isinstance(value, list):
        raise ToolError("domain must be a list of conditions.")
    leaves = []
    for item in value:
        if isinstance(item, str) and item in ("&", "|", "!"):
            leaves.append(item)
        elif (
            isinstance(item, (list, tuple)) and len(item) == 3 and isinstance(item[0], str) and isinstance(item[1], str)
        ):
            leaves.append((item[0], item[1], item[2]))
        else:
            raise ToolError(f"A condition is [field, operator, value]; got {json.dumps(item)[:80]}.")
    try:
        return Domain(leaves)
    except Exception as exc:
        raise ToolError(f"The domain is not valid: {exc}") from exc


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


MODEL = {"type": "string", "description": "Technical model name, for example sale.order"}
DOMAIN = {"type": "array", "description": 'Filter, e.g. [["state","=","sale"]]. Empty means all records.', "items": {}}
FIELDS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Field names to return. Omit for the main fields.",
}
IDS = {"type": "array", "items": {"type": "integer"}, "description": f"Record ids, at most {MAX_IDS}."}
VALUES = {
    "type": "object",
    "description": "Field values. A many-to-one is the id; a many-to-many or one-to-many is a list of commands such as [[6,0,[1,2]]].",
}

TOOLS = {
    "odoo_whoami": (
        "Who Claude is in Odoo: the user, the company and what this access key may do.",
        schema({}),
        "read",
    ),
    "odoo_list_models": (
        "List the models this key can use, with what is allowed on each (read, create, change, delete).",
        schema({}),
        "read",
    ),
    "odoo_describe_model": (
        "Describe the fields of a model: name, label, type, whether it is required or read only, the related model and the choices.",
        schema({"model": MODEL}, ["model"]),
        "read",
    ),
    "odoo_search_read": (
        f"Find records and return their fields. At most {MAX_ROWS} rows; use offset to page. The answer says how many match.",
        schema(
            {
                "model": MODEL,
                "domain": DOMAIN,
                "fields": FIELDS,
                "limit": {
                    "type": "integer",
                    "description": f"Rows to return, default {DEFAULT_ROWS}, at most {MAX_ROWS}.",
                },
                "offset": {"type": "integer"},
                "order": {"type": "string", "description": "For example 'date_order desc, id'."},
            },
            ["model"],
        ),
        "read",
    ),
    "odoo_count": (
        "Count the records that match a filter.",
        schema({"model": MODEL, "domain": DOMAIN}, ["model"]),
        "read",
    ),
    "odoo_read": (
        "Read specific records by id.",
        schema({"model": MODEL, "ids": IDS, "fields": FIELDS}, ["model", "ids"]),
        "read",
    ),
    "odoo_group": (
        "Total a number by a field, for example the sales amount per customer or per month. Returns one row per group.",
        schema(
            {
                "model": MODEL,
                "domain": DOMAIN,
                "group_by": {
                    "type": "string",
                    "description": "Field, optionally with a period for dates: 'date_order:month', 'partner_id'.",
                },
                "measures": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Aggregates such as 'amount_total:sum', 'id:count', 'price_unit:avg'.",
                },
                "limit": {"type": "integer"},
            },
            ["model", "group_by"],
        ),
        "read",
    ),
    "odoo_create": (
        "Create a record. Only for models the key may change.",
        schema({"model": MODEL, "values": VALUES}, ["model", "values"]),
        "create",
    ),
    "odoo_write": (
        "Change records. Only for models the key may change.",
        schema({"model": MODEL, "ids": IDS, "values": VALUES}, ["model", "ids", "values"]),
        "write",
    ),
    "odoo_delete": (
        "Delete records. Only for models where the key may delete.",
        schema({"model": MODEL, "ids": IDS}, ["model", "ids"]),
        "unlink",
    ),
    "odoo_run_action": (
        "Press a button on records, for example action_confirm on sale orders. Only for keys that may run actions.",
        schema(
            {
                "model": MODEL,
                "ids": IDS,
                "method": {"type": "string", "description": "Method name, e.g. action_confirm."},
            },
            ["model", "ids", "method"],
        ),
        "action",
    ),
}


class ClaudeMcpServer(models.AbstractModel):
    """The Model Context Protocol server: JSON-RPC 2.0 messages in, answers out. One key = one `token` record."""

    _name = "claude.mcp.server"
    _description = "Claude MCP Server"

    # -- protocol -------------------------------------------------------------------------------

    @api.model
    def handle(self, token, body):
        """`body` is the parsed JSON of one POST. Returns (http status, JSON-able answer or None)."""
        if isinstance(body, list):
            answers = [a for a in (self._message(token, m) for m in body) if a is not None]
            return (200, answers) if answers else (202, None)
        answer = self._message(token, body)
        return (200, answer) if answer is not None else (202, None)

    def _message(self, token, message):
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
        has_id = "id" in message
        method, params = message["method"], message.get("params") or {}
        handler = {
            "initialize": self._initialize,
            "ping": lambda token, params: {},
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
        }.get(method)
        if not has_id:  # notifications (initialized, cancelled ...) get no answer
            return None
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"Method '{method}' is not supported."},
            }
        if not isinstance(params, dict):
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32602, "message": "params must be an object."},
            }
        return {"jsonrpc": "2.0", "id": message["id"], "result": handler(token, params)}

    def _initialize(self, token, params):
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "odoo-claude-mcp", "version": "1.0.0"},
            "instructions": INSTRUCTIONS,
        }

    def _tools_list(self, token, params):
        tools = []
        for name, (description, input_schema, kind) in TOOLS.items():
            if not self._offered(token, kind):
                continue
            tools.append(
                {
                    "name": name,
                    "description": description,
                    "inputSchema": input_schema,
                    "annotations": {"readOnlyHint": kind == "read", "destructiveHint": kind in ("unlink", "action")},
                }
            )
        return {"tools": tools}

    @staticmethod
    def _offered(token, kind):
        """Hide what the key could never do, so Claude does not try."""
        if kind == "read":
            return True
        if token.read_only:
            return False
        if kind == "action":
            return token.allow_actions and token._can_write_anything()
        return any(
            {"create": line.can_create, "write": line.can_write, "unlink": line.can_delete}[kind]
            for line in token.access_ids
        )

    def _tools_call(self, token, params):
        name, arguments = params.get("name"), params.get("arguments") or {}
        started = time.monotonic()
        model_name, error, result = arguments.get("model") if isinstance(arguments, dict) else None, None, None
        try:
            if name not in TOOLS or not isinstance(arguments, dict):
                raise ToolError(f"Unknown tool '{name}'.")
            if not self._offered(token, TOOLS[name][2]):
                raise ToolError("This access key is not allowed to do that.")
            self._check_quota(token)
            with self.env.cr.savepoint():
                result = getattr(self, f"_tool_{name}")(token, token._user_env(), **self._arguments(name, arguments))
        except ToolError as exc:
            error = str(exc)
        except (AccessError, MissingError, UserError, ValidationError, ValueError) as exc:
            error = str(exc)
        except TypeError as exc:
            error = f"The arguments do not fit this tool: {exc}"
        except Exception as exc:
            _logger.exception("Claude MCP tool %s failed", name)
            error = f"Odoo could not do that ({type(exc).__name__}). Details are in the Odoo log."
        self._log(token, name, model_name, arguments, error, started)
        if error:
            return {"content": [{"type": "text", "text": error}], "isError": True}
        payload = json_safe(result)
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > MAX_TEXT:
            return {
                "content": [
                    {"type": "text", "text": text[:MAX_TEXT] + "… [cut: too long, ask for fewer rows or fields]"}
                ]
            }
        answer = {"content": [{"type": "text", "text": text}]}
        if isinstance(payload, dict):
            answer["structuredContent"] = payload
        return answer

    @staticmethod
    def _arguments(name, arguments):
        allowed = set(TOOLS[name][1]["properties"])
        extra = set(arguments) - allowed
        if extra:
            raise ToolError(
                f"Unknown argument '{sorted(extra)[0]}'. This tool takes: {', '.join(sorted(allowed)) or 'nothing'}."
            )
        return arguments

    def _check_quota(self, token):
        if token.daily_limit and token._count_today() >= token.daily_limit:
            raise ToolError(
                f"The daily limit of {token.daily_limit} calls for this key is used up. It starts again tomorrow."
            )

    def _log(self, token, tool, model_name, arguments, error, started):
        summary = json.dumps(json_safe(arguments), ensure_ascii=False)[:250] if isinstance(arguments, dict) else ""
        self.env["claude.mcp.log"].sudo().create(
            {
                "token_id": token.id,
                "tool": str(tool)[:80],
                "model_name": model_name if isinstance(model_name, str) else False,
                "summary": summary,
                "success": not error,
                "error": (error or "")[:250] or False,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        )
        token._touch()

    # -- helpers -------------------------------------------------------------------------------

    def _model(self, token, env, model_name, operation):
        """The model as the key's user, after the key's own permission and Odoo's access rights."""
        if not isinstance(model_name, str) or model_name not in env:
            raise ToolError(f"There is no model '{model_name}'. Use odoo_list_models to see what is available.")
        if is_blocked(model_name) or not token._may(model_name, operation):
            hint = {
                "read": "read",
                "create": "create records in",
                "write": "change",
                "unlink": "delete from",
                "action": "run actions on",
            }[operation]
            raise ToolError(
                f"This access key may not {hint} {model_name}. The Odoo administrator can allow it on the key."
            )
        Model = env[model_name]
        Model.check_access("unlink" if operation == "unlink" else "write" if operation == "action" else operation)
        return Model

    def _line_domain(self, token, env, model_name):
        line = token._line(model_name)
        return line._domain(env) if line else Domain.TRUE

    def _allowed_fields(self, Model):
        return {
            name: info
            for name, info in Model.fields_get(
                attributes=["type", "string", "required", "readonly", "relation", "selection", "help"]
            ).items()
            if info["type"] not in ("binary", "properties", "json") and not is_sensitive(name)
        }

    def _field_names(self, Model, requested):
        allowed = self._allowed_fields(Model)
        if requested:
            if not isinstance(requested, list):
                raise ToolError("fields must be a list of field names.")
            unknown = [f for f in requested if f not in allowed]
            if unknown:
                raise ToolError(
                    f"The model {Model._name} has no readable field '{unknown[0]}'. Use odoo_describe_model."
                )
            return list(dict.fromkeys(["id", *requested]))
        defaults = [
            name
            for name, info in allowed.items()
            if Model._fields[name].store
            and info["type"] not in ("one2many", "many2many", "html", "text")
            and not name.startswith(SKIPPED_PREFIXES)
            and name not in ("create_uid", "write_uid", "write_date")
        ]
        return ["id", *[n for n in defaults if n != "id"][:40]]

    def _id_list(self, value):
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in value)
        ):
            raise ToolError("ids must be a non-empty list of record ids.")
        if len(value) > MAX_IDS:
            raise ToolError(f"At most {MAX_IDS} records at a time.")
        return value

    def _records(self, token, env, Model, ids):
        """The records, which must all exist, be visible to the key's user and match the key's filter."""
        domain = Domain("id", "in", ids) & self._line_domain(token, env, Model._name)
        records = Model.search(domain)
        missing = sorted(set(ids) - set(records.ids))
        if missing:
            raise ToolError(f"Record {missing[0]} of {Model._name} does not exist or this key cannot reach it.")
        return records

    def _values(self, Model, values):
        if not isinstance(values, dict) or not values:
            raise ToolError("values must be an object of field values.")
        allowed = self._allowed_fields(Model)
        for name in values:
            if name not in allowed:
                raise ToolError(
                    f"The model {Model._name} has no field '{name}' this key may set. Use odoo_describe_model."
                )
            if allowed[name].get("readonly") and not Model._fields[name].store:
                raise ToolError(f"The field '{name}' is read only.")
        return values

    # -- tools -----------------------------------------------------------------------------------

    def _tool_odoo_whoami(self, token, env):
        user = env.user
        return {
            "user": user.name,
            "login": user.login,
            "company": user.company_id.name,
            "companies": user.company_ids.mapped("name"),
            "key": token.name,
            "read_only": token.read_only,
            "may_run_actions": token.allow_actions and not token.read_only,
            "calls_today": token._count_today(),
            "daily_limit": token.daily_limit or None,
            "valid_until": token.token_expiry and token.token_expiry.isoformat(),
        }

    def _tool_odoo_list_models(self, token, env):
        rows = []
        if token.access_all_models:
            names = [
                name for name in env.registry if not is_blocked(name) and not env[name]._transient and env[name]._auto
            ]
        else:
            names = token.access_ids.mapped("model_name")
        for name in sorted(set(names)):
            if name not in env or not env[name].has_access("read"):
                continue
            rows.append(
                {
                    "model": name,
                    "name": env[name]._description,
                    "create": token._may(name, "create") and env[name].has_access("create"),
                    "write": token._may(name, "write") and env[name].has_access("write"),
                    "delete": token._may(name, "unlink") and env[name].has_access("unlink"),
                }
            )
        return {"models": rows, "count": len(rows)}

    def _tool_odoo_describe_model(self, token, env, model):
        Model = self._model(token, env, model, "read")
        fields_ = []
        for name, info in self._allowed_fields(Model).items():
            row = {"name": name, "label": info["string"], "type": info["type"]}
            if info.get("required"):
                row["required"] = True
            if info.get("readonly"):
                row["readonly"] = True
            if info.get("relation"):
                row["relation"] = info["relation"]
            if info.get("selection"):
                row["choices"] = [{"value": value, "label": label} for value, label in info["selection"]]
            if not Model._fields[name].store:
                row["computed_not_searchable"] = True
            fields_.append(row)
        return {"model": model, "name": Model._description, "fields": fields_}

    def _tool_odoo_search_read(
        self, token, env, model, domain=None, fields=None, limit=DEFAULT_ROWS, offset=0, order=None
    ):
        Model = self._model(token, env, model, "read")
        full = to_domain(domain) & self._line_domain(token, env, model)
        limit = self._bounded(limit, DEFAULT_ROWS, MAX_ROWS, "limit")
        names = self._field_names(Model, fields)
        try:
            records = Model.search(
                full, limit=limit, offset=self._bounded(offset, 0, 10**9, "offset"), order=order or None
            )
            rows = records.read(names)
            total = Model.search_count(full)
        except (ValueError, KeyError) as exc:
            raise ToolError(f"The search is not valid: {exc}") from exc
        return {"count": total, "returned": len(rows), "offset": offset, "records": rows}

    def _tool_odoo_count(self, token, env, model, domain=None):
        Model = self._model(token, env, model, "read")
        return {"count": Model.search_count(to_domain(domain) & self._line_domain(token, env, model))}

    def _tool_odoo_read(self, token, env, model, ids, fields=None):
        Model = self._model(token, env, model, "read")
        records = self._records(token, env, Model, self._id_list(ids))
        return {"records": records.read(self._field_names(Model, fields))}

    def _tool_odoo_group(self, token, env, model, group_by, domain=None, measures=None, limit=50):
        Model = self._model(token, env, model, "read")
        full = to_domain(domain) & self._line_domain(token, env, model)
        measures = measures or ["__count"]
        if not isinstance(measures, list) or not isinstance(group_by, str):
            raise ToolError("group_by is a field name and measures a list such as ['amount_total:sum'].")
        for spec in [group_by, *[m for m in measures if m != "__count"]]:
            if spec.split(":")[0] not in self._allowed_fields(Model) and spec != "id:count":
                raise ToolError(f"There is no readable field '{spec.split(':')[0]}' on {model}.")
        try:
            rows = Model._read_group(
                full,
                groupby=[group_by],
                aggregates=["__count" if m == "id:count" else m for m in measures],
                limit=self._bounded(limit, 50, 500, "limit"),
            )
        except (ValueError, KeyError, AttributeError) as exc:
            raise ToolError(f"The grouping is not valid: {exc}") from exc
        labels = ["group", *measures]
        out = []
        for row in rows:
            first = row[0]
            if isinstance(first, models.BaseModel):
                first = {"id": first.id, "name": first.display_name} if first else None
            out.append(dict(zip(labels, [first, *row[1:]], strict=False)))
        return {"rows": out}

    def _tool_odoo_create(self, token, env, model, values):
        Model = self._model(token, env, model, "create")
        record = Model.create(self._values(Model, values))
        return {"id": record.id, "display_name": record.display_name}

    def _tool_odoo_write(self, token, env, model, ids, values):
        Model = self._model(token, env, model, "write")
        records = self._records(token, env, Model, self._id_list(ids))
        records.write(self._values(Model, values))
        return {"changed": records.ids}

    def _tool_odoo_delete(self, token, env, model, ids):
        Model = self._model(token, env, model, "unlink")
        records = self._records(token, env, Model, self._id_list(ids))
        deleted = records.ids
        records.unlink()
        return {"deleted": deleted}

    def _tool_odoo_run_action(self, token, env, model, ids, method):
        Model = self._model(token, env, model, "action")
        if (
            not isinstance(method, str)
            or method.startswith("_")
            or hasattr(models.BaseModel, method)
            or not callable(getattr(Model, method, None))
        ):
            raise ToolError(f"'{method}' is not a method Claude may run. Use a button method such as action_confirm.")
        records = self._records(token, env, Model, self._id_list(ids))
        result = getattr(records, method)()
        if isinstance(result, models.BaseModel):
            return {"records": result.ids}
        return {"done": True, "result": result if isinstance(result, (dict, list, str, int, float, bool)) else None}

    @staticmethod
    def _bounded(value, default, maximum, name):
        if value is None:
            return default
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ToolError(f"{name} must be a whole number, zero or more.")
        return min(value, maximum)

    @api.model
    def _today(self):
        return fields.Date.today()
