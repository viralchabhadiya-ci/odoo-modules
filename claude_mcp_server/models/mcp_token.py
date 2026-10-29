import hashlib
import hmac
import json
import secrets

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.fields import Domain
from odoo.tools.safe_eval import safe_eval

# Never offered to Claude, whatever the key allows: credentials, configuration and the access keys themselves.
BLOCKED_PREFIXES = ("ir.", "base.", "bus.", "auth", "res.users.", "claude.mcp.", "powerbi.", "mail.mail", "fetchmail.")
BLOCKED_MODELS = {"res.users", "res.groups", "res.config.settings", "payment.provider", "payment.token"}
SENSITIVE_PARTS = ("password", "token", "secret", "api_key", "apikey", "private_key")


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def token_from_header(authorization):
    scheme, _sep, value = (authorization or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def is_blocked(model_name):
    return model_name in BLOCKED_MODELS or model_name.startswith(BLOCKED_PREFIXES)


def is_sensitive(field_name):
    return any(part in field_name.lower() for part in SENSITIVE_PARTS)


class ClaudeMcpToken(models.Model):
    """One access key for Claude: acts as an Odoo user, sees only the models listed, read only unless allowed."""

    _name = "claude.mcp.token"
    _description = "Claude MCP Access Key"
    _order = "name"

    name = fields.Char(required=True, help="Who or what uses this key, e.g. 'Claude Desktop of Maria'.")
    active = fields.Boolean(default=True)
    user_id = fields.Many2one(
        "res.users",
        string="Acts as",
        required=True,
        default=lambda self: self.env.user,
        domain="[('share', '=', False)]",
        help="Claude sees and changes exactly what this user may: access rights and record rules apply.",
    )
    token_hash = fields.Char(groups="base.group_system", copy=False)
    token_hint = fields.Char(string="Key", readonly=True, copy=False)
    token_expiry = fields.Date(string="Valid until", help="Leave empty for a key that does not expire.")
    read_only = fields.Boolean(
        string="Read only",
        default=True,
        help="Claude can look but not change anything, whatever the tables below say.",
    )
    allow_actions = fields.Boolean(
        string="May run actions",
        help="Lets Claude press buttons such as Confirm on records it may change (needs 'Change' on that model).",
    )
    access_all_models = fields.Boolean(
        string="May read any model the user can read",
        help="Writing, creating, deleting and actions still need the model to be listed below.",
    )
    access_ids = fields.One2many("claude.mcp.access", "token_id", string="Models")
    daily_limit = fields.Integer(string="Calls per day", default=2000, help="0 means no limit.")
    last_used = fields.Datetime(readonly=True, copy=False)
    log_ids = fields.One2many("claude.mcp.log", "token_id")
    calls_today = fields.Integer(compute="_compute_calls_today")
    url = fields.Char(string="Server address", compute="_compute_url")

    def _count_today(self):
        """Calls since midnight (UTC), counted afresh: a batch of calls must see its own earlier calls."""
        today = fields.Datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            self.env["claude.mcp.log"].sudo().search_count([("token_id", "=", self.id), ("create_date", ">=", today)])
        )

    def _compute_calls_today(self):
        for token in self:
            token.calls_today = token._count_today()

    @api.depends("name")
    def _compute_url(self):
        for token in self:
            token.url = f"{token.get_base_url()}/mcp/{self.env.cr.dbname}"

    # -- key -----------------------------------------------------------------------------------

    def action_new_token(self):
        """A new key replaces the old one at once. It is shown once; only its fingerprint is kept."""
        self.ensure_one()
        token = secrets.token_urlsafe(32)
        self.sudo().write({"token_hash": hash_token(token), "token_hint": f"…{token[-4:]}"})
        url = self.url
        header = f"Authorization: Bearer {token}"
        config = {
            "mcpServers": {
                self.name or "odoo": {"command": "npx", "args": ["-y", "mcp-remote", url, "--header", header]}
            }
        }
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Connect Claude"),
            "res_model": "claude.mcp.token.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_token_id": self.id,
                "default_url": url,
                "default_token": token,
                "default_command": f'claude mcp add --transport http odoo {url} --header "{header}"',
                "default_config": json.dumps(config, indent=2),
            },
        }

    def action_revoke_token(self):
        self.sudo().write({"token_hash": False, "token_hint": False})

    @api.model
    def _authenticate(self, authorization):
        """The key's record if the key is right, valid and its user usable, else an empty recordset."""
        token = token_from_header(authorization)
        if not token:
            return self.browse()
        found = self.sudo().search([("token_hash", "=", hash_token(token))], limit=1)
        if not found or not found.token_hash or not hmac.compare_digest(found.token_hash, hash_token(token)):
            return self.browse()
        if found.token_expiry and found.token_expiry < fields.Date.today():
            return self.browse()
        if not found.user_id.active or found.user_id.share:
            return self.browse()
        return found

    # -- what the key may do -------------------------------------------------------------------------

    def _user_env(self):
        user = self.user_id
        return self.env(
            user=user.id, context={"allowed_company_ids": user.company_ids.ids, "lang": user.lang, "tz": user.tz}
        )

    def _line(self, model_name):
        return self.access_ids.filtered(lambda line: line.model_name == model_name)[:1]

    def _may(self, model_name, operation):
        """Does the key (not Odoo's own rights) allow this? operation: read, create, write, unlink, action."""
        if is_blocked(model_name):
            return False
        line = self._line(model_name)
        if operation == "read":
            return bool(line) or self.access_all_models
        if self.read_only or not line:
            return False
        if operation == "action":
            return self.allow_actions and line.can_write
        return {"create": line.can_create, "write": line.can_write, "unlink": line.can_delete}[operation]

    def _can_write_anything(self):
        return not self.read_only and any(
            line.can_create or line.can_write or line.can_delete for line in self.access_ids
        )

    def _touch(self):
        now = fields.Datetime.now()
        if not self.last_used or (now - self.last_used).total_seconds() > 300:
            self.last_used = now


class ClaudeMcpAccess(models.Model):
    """A model Claude may use through a key, and how."""

    _name = "claude.mcp.access"
    _description = "Claude MCP Model Access"
    _order = "token_id, model_id"

    token_id = fields.Many2one("claude.mcp.token", required=True, ondelete="cascade", index=True)
    model_id = fields.Many2one(
        "ir.model", string="Model", required=True, ondelete="cascade", domain="[('transient', '=', False)]"
    )
    model_name = fields.Char(related="model_id.model", store=True)
    can_create = fields.Boolean(string="Create")
    can_write = fields.Boolean(string="Change")
    can_delete = fields.Boolean(string="Delete")
    domain = fields.Char(
        string="Only records matching",
        default="[]",
        help="Narrows what Claude can see and change on top of the user's own rights.",
    )

    _unique_model = models.Constraint("unique (token_id, model_id)", "A model can be listed only once per key.")

    @api.constrains("model_id")
    def _check_model(self):
        for line in self:
            if is_blocked(line.model_name):
                raise ValidationError(
                    self.env._("%s holds credentials or settings and can never be opened to Claude.", line.model_name)
                )

    @api.constrains("domain")
    def _check_domain(self):
        for line in self:
            try:
                Domain(safe_eval(line.domain or "[]"))
            except Exception as exc:
                raise ValidationError(
                    self.env._("The filter of %(model)s is not valid: %(error)s", model=line.model_name, error=exc)
                ) from exc

    def _domain(self, env):
        return Domain(safe_eval(self.domain or "[]", {"uid": env.uid}))
