import hashlib
import json
import logging
import secrets
from datetime import timedelta

from odoo import Command, api, fields, models, tools
from odoo.exceptions import UserError

from .marketing_error import MarketingAuthError, MarketingError

_logger = logging.getLogger(__name__)

CHUNK = 500  # contacts per call to the provider
MARGIN = timedelta(minutes=2)  # re-read a little before the last sync, clocks differ
FIRST_CAMPAIGN_DAYS = 90
CAMPAIGN_DAYS = 30  # results of older campaigns no longer change


def member_hash(item):
    """Fingerprint of what a contact looks like on the provider, to skip contacts that did not change."""
    key = [item["email"], item.get("first_name") or "", item.get("last_name") or "", bool(item["subscribed"])]
    return hashlib.md5(json.dumps(key).encode()).hexdigest()  # noqa: S324 - not a security use


class MarketingAccount(models.Model):
    """A connected email marketing account. Provider modules add a `provider` and implement the hooks
    `_<provider>_test_connection`, `_fetch_lists`, `_fetch_members`, `_push_members` (and optionally
    `_fetch_member`, `_fetch_campaigns`, `_register_webhooks`, `_parse_webhook`, `_connect_help`).

    Providers speak in neutral dicts. A member is {"email", "first_name", "last_name", "subscribed", "remote_id",
    "updated"}; a list is {"id", "name", "count"}."""

    _name = "marketing.account"
    _description = "Email Marketing Account"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    provider = fields.Selection([], required=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    state = fields.Selection(
        [("draft", "Not connected"), ("connected", "Connected"), ("error", "Connection problem")],
        default="draft",
        required=True,
        tracking=True,
        copy=False,
    )
    connection_message = fields.Char(string="Message", copy=False, readonly=True)
    api_key = fields.Char(
        string="API key", groups="marketing_connector_base.group_marketing_connector_manager", copy=False
    )
    connect_help = fields.Text(compute="_compute_connect_help")
    auto_sync = fields.Boolean(string="Sync automatically", default=True)
    create_contacts = fields.Boolean(
        string="Create Odoo contacts for new subscribers",
        default=True,
        help="Subscribers who signed up at the provider become mailing contacts in Odoo. "
        "Otherwise only people already in Odoo are updated.",
    )
    last_sync = fields.Datetime(readonly=True, copy=False)
    webhook_token = fields.Char(
        groups="marketing_connector_base.group_marketing_connector_manager",
        copy=False,
        default=lambda self: secrets.token_urlsafe(24),
    )
    webhook_url = fields.Char(
        compute="_compute_webhook_url", groups="marketing_connector_base.group_marketing_connector_manager"
    )
    supports_webhooks = fields.Boolean(compute="_compute_webhook_url")
    webhooks_registered = fields.Boolean(string="Instant updates are on", copy=False, readonly=True)

    link_ids = fields.One2many("marketing.list.link", "account_id")
    campaign_ids = fields.One2many("marketing.campaign", "account_id")
    link_count = fields.Integer(compute="_compute_counts")
    synced_count = fields.Integer(compute="_compute_counts")
    problem_count = fields.Integer(compute="_compute_counts")
    campaign_count = fields.Integer(compute="_compute_counts")

    @api.depends("link_ids.sync", "link_ids.problem_count", "campaign_ids")
    def _compute_counts(self):
        for account in self:
            account.link_count = len(account.link_ids)
            account.synced_count = len(account.link_ids.filtered("sync"))
            account.problem_count = sum(account.link_ids.mapped("problem_count"))
            account.campaign_count = len(account.campaign_ids)

    @api.depends("provider")
    def _compute_connect_help(self):
        for account in self:
            hook = getattr(account, f"_{account.provider}_connect_help", None)
            account.connect_help = hook() if hook else False

    @api.depends("provider", "webhook_token")
    def _compute_webhook_url(self):
        for account in self:
            account.supports_webhooks = bool(getattr(account, f"_{account.provider}_register_webhooks", None))
            token = account.sudo().webhook_token
            account.webhook_url = (
                f"{account.get_base_url()}/marketing/{account.provider}/webhook/{account.id}/{token}"
                if account.id and token
                else False
            )

    # -- provider hooks ----------------------------------------------------------------

    def _dispatch(self, hook, *args, optional=False):
        self.ensure_one()
        method = getattr(self, f"_{self.provider}_{hook}", None)
        if method is None:
            if optional:
                return None
            raise MarketingError(
                self.env._("%s cannot do this yet.", dict(self._fields["provider"].selection)[self.provider])
            )
        return method(*args)

    def _notify(self, title, message, kind="success"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": kind, "sticky": kind != "success"},
        }

    def _record_problem(self, exc):
        """Auth problems disconnect the account; anything else (network, rate limit) just leaves a message."""
        vals = {"connection_message": str(exc)}
        if isinstance(exc, MarketingAuthError):
            vals["state"] = "error"
        self.write(vals)

    # -- connect -------------------------------------------------------------------------

    def action_connect(self):
        self.ensure_one()
        try:
            who = self._dispatch("test_connection")
            self._sync_lists()
        except MarketingError as exc:
            self._record_problem(exc)
            return self._notify(self.env._("Could not connect"), str(exc), "danger")
        self.write({"state": "connected", "connection_message": False})
        return self._notify(
            self.env._("Connected"),
            self.env._(
                "%(who)s: %(count)s audiences found. Tick the ones to keep in sync.", who=who, count=len(self.link_ids)
            ),
        )

    def action_sync_now(self):
        self.ensure_one()
        if self._sync():
            return self._notify(
                self.env._("Synced"), self.env._("Contacts, unsubscribes and campaign results are up to date.")
            )
        return self._notify(
            self.env._("Sync stopped"), self.connection_message or self.env._("See the lists for details."), "danger"
        )

    def action_resync_all(self):
        """Read and send everything again; contacts that did not change are skipped by their fingerprint."""
        self.link_ids.write({"last_pull": False, "last_push": False})
        return self.action_sync_now()

    def action_register_webhooks(self):
        self.ensure_one()
        if not self._public_url():
            raise UserError(
                self.env._(
                    "Instant updates need Odoo to be reachable from the internet over https. "
                    "This database is at %s. Until then, changes are picked up every 15 minutes.",
                    self.get_base_url(),
                )
            )
        try:
            self._dispatch("register_webhooks")
        except MarketingError as exc:
            raise UserError(str(exc)) from exc
        return self._notify(self.env._("Instant updates are on"), self.env._("Unsubscribes now arrive within seconds."))

    def _public_url(self):
        url = self.get_base_url()
        return url.startswith("https://") and not any(host in url for host in ("localhost", "127.0.0.1", "0.0.0.0"))

    def action_view_problems(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Contacts that could not be synced"),
            "res_model": "marketing.member",
            "view_mode": "list,form",
            "domain": [("account_id", "=", self.id), ("sync_error", "!=", False)],
        }

    # -- lists -----------------------------------------------------------------------------

    def _sync_lists(self):
        self.ensure_one()
        known = {link.remote_id: link for link in self.link_ids}
        seen = set()
        for item in self._dispatch("fetch_lists"):
            remote_id = str(item["id"])
            seen.add(remote_id)
            vals = {"name": item["name"], "remote_count": item.get("count") or 0, "missing": False}
            if remote_id in known:
                known[remote_id].write(vals)
            else:
                self.env["marketing.list.link"].create({**vals, "account_id": self.id, "remote_id": remote_id})
        for remote_id, link in known.items():
            if remote_id not in seen:
                link.write({"missing": True, "sync": False})

    # -- contacts --------------------------------------------------------------------------

    @staticmethod
    def _local_item(contact, subscription):
        first, last = contact.first_name or "", contact.last_name or ""
        if not (first or last):
            first, _sep, last = (contact.name or "").strip().partition(" ")
        blacklisted = contact.sudo().is_blacklisted
        return {
            "email": contact.email_normalized,
            "first_name": first,
            "last_name": last,
            "subscribed": not (subscription.opt_out or blacklisted),
            "blacklisted": blacklisted,  # do not email at all, not only "left this list"
        }

    def _apply_members(self, link, items):
        """Bring Odoo up to date with what the provider says. Unsubscribes at the provider always win; a
        subscription only comes back when it was changed there after the contact opted out in Odoo."""
        Contact = self.env["mailing.contact"]
        Subscription = self.env["mailing.subscription"]
        items = [dict(i, email=tools.email_normalize(i.get("email") or "")) for i in items]
        items = [i for i in items if i["email"]]
        if not items:
            return
        by_email = {}
        for contact in Contact.search([("email_normalized", "in", [i["email"] for i in items])]):
            by_email.setdefault(contact.email_normalized, []).append(contact)
        members = {m.contact_id.id: m for m in link.member_ids}
        for item in items:
            email, subscribed, updated = item["email"], bool(item["subscribed"]), item.get("updated")
            candidates = by_email.get(email, [])
            contact = next(
                (c for c in candidates if link.list_id in c.list_ids), candidates[0] if candidates else Contact
            )
            name = " ".join(filter(None, [item.get("first_name"), item.get("last_name")]))
            if not contact:
                if not subscribed or not self.create_contacts:
                    continue
                contact = Contact.create(
                    {
                        "name": name or email,
                        "email": email,
                        "first_name": item.get("first_name"),
                        "last_name": item.get("last_name"),
                        "subscription_ids": [Command.create({"list_id": link.list_id.id})],
                    }
                )
                by_email.setdefault(email, []).append(contact)
            elif name and name != contact.name and (not contact.name or (updated and updated > contact.write_date)):
                contact.write({"name": name, "first_name": item.get("first_name"), "last_name": item.get("last_name")})
            subscription = contact.subscription_ids.filtered(lambda s: s.list_id == link.list_id)
            if not subscription:
                subscription = Subscription.create(
                    {"contact_id": contact.id, "list_id": link.list_id.id, "opt_out": not subscribed}
                )
            elif not subscribed and not subscription.opt_out:
                subscription.opt_out = True
            elif (
                subscribed
                and subscription.opt_out
                and updated
                and subscription.opt_out_datetime
                and updated > subscription.opt_out_datetime
            ):
                subscription.opt_out = False
            vals = {
                "email": email,
                "remote_id": item.get("remote_id"),
                "remote_subscribed": subscribed,
                "pushed_hash": member_hash(self._local_item(contact, subscription)),
                "sync_error": False,
            }
            member = members.get(contact.id)
            if member:
                member.write(vals)
            else:
                members[contact.id] = self.env["marketing.member"].create(
                    {"link_id": link.id, "contact_id": contact.id, **vals}
                )

    def _pull_link(self, link):
        link._ensure_list()
        started = fields.Datetime.now()
        since = link.last_pull and link.last_pull - MARGIN
        for chunk in tools.split_every(200, self._dispatch("fetch_members", link, since)):
            self._apply_members(link, list(chunk))
        link.last_pull = started

    def _push_link(self, link):
        link._ensure_list()
        started = fields.Datetime.now()
        since = link.last_push and link.last_push - MARGIN
        domain = [("list_id", "=", link.list_id.id), ("contact_id.email_normalized", "!=", False)]
        if since:
            # a contact who was just blacklisted (or taken off the blacklist) has not been written itself
            changed = self.env["mail.blacklist"].with_context(active_test=False).search([("write_date", ">=", since)])
            domain += [
                "|",
                "|",
                ("write_date", ">=", since),
                ("contact_id.write_date", ">=", since),
                ("contact_id.email_normalized", "in", changed.mapped("email")),
            ]
        members = {m.contact_id.id: m for m in link.member_ids}
        todo = []
        for subscription in self.env["mailing.subscription"].search(domain):
            item = self._local_item(subscription.contact_id, subscription)
            fingerprint = member_hash(item)
            member = members.get(subscription.contact_id.id)
            if member and member.pushed_hash == fingerprint:
                if member.sync_error:
                    member.sync_error = False
            elif not item["subscribed"] and not member:
                continue  # never on the provider, nothing to unsubscribe
            elif item["subscribed"] and member and not member.remote_subscribed:
                message = self.env._(
                    "Unsubscribed at the provider. Only the contact can subscribe again, with the provider's sign-up form."
                )
                if member.sync_error != message:
                    member.sync_error = message
            else:
                todo.append((subscription.contact_id, item, fingerprint))
        for chunk in tools.split_every(CHUNK, todo):
            errors = self._dispatch("push_members", link, [item for _contact, item, _hash in chunk]) or {}
            for contact, item, fingerprint in chunk:
                error = errors.get(item["email"])
                vals = {
                    "email": item["email"],
                    "sync_error": error or False,
                    "pushed_hash": False if error else fingerprint,
                }
                if not error:
                    vals["remote_subscribed"] = item["subscribed"]
                member = members.get(contact.id)
                if member:
                    member.write(vals)
                else:
                    members[contact.id] = self.env["marketing.member"].create(
                        {"link_id": link.id, "contact_id": contact.id, **vals}
                    )
        link.last_push = started

    def _sync_link(self, link):
        """Pull then push one list. A problem with one list is kept on that list and does not stop the others."""
        try:
            with self.env.cr.savepoint():
                self._pull_link(link)
                self._push_link(link)
        except MarketingAuthError:
            raise
        except MarketingError as exc:
            link.error = str(exc)
            return False
        link.error = False
        return True

    # -- campaigns ---------------------------------------------------------------------------

    def _pull_campaigns(self):
        days = CAMPAIGN_DAYS if self.last_sync else FIRST_CAMPAIGN_DAYS
        since = fields.Datetime.now() - timedelta(days=days)
        known = {c.remote_id: c for c in self.campaign_ids}
        for item in self._dispatch("fetch_campaigns", since, optional=True) or []:
            vals = {
                "name": item["name"],
                "subject": item.get("subject"),
                "list_name": item.get("list_name"),
                "sent_date": item.get("sent_date"),
                "emails_sent": item.get("emails_sent") or 0,
                "opens": item.get("opens") or 0,
                "clicks": item.get("clicks") or 0,
                "unsubscribes": item.get("unsubscribes") or 0,
                "bounces": item.get("bounces") or 0,
            }
            campaign = known.get(str(item["id"]))
            if campaign:
                campaign.write(vals)
            else:
                self.env["marketing.campaign"].create({**vals, "account_id": self.id, "remote_id": str(item["id"])})

    # -- running -------------------------------------------------------------------------------

    def _sync(self):
        """Everything one account needs every 15 minutes. Returns False when it had to stop."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self._sync_lists()
                if self._public_url():
                    try:
                        self._dispatch("register_webhooks", optional=True)
                    except MarketingError as exc:
                        _logger.warning("Marketing account %s: instant updates not available: %s", self.id, exc)
                ok = all(
                    [
                        self._sync_link(link)
                        for link in self.link_ids.filtered(lambda link: link.sync and not link.missing)
                    ]
                )
                self._pull_campaigns()
        except MarketingError as exc:
            self._record_problem(exc)
            return False
        except Exception as exc:
            _logger.exception("Marketing account %s failed", self.id)
            self.connection_message = self.env._(
                "Unexpected problem (%s). The Odoo log has the details.", type(exc).__name__
            )
            return False
        self.write(
            {
                "state": "connected",
                "connection_message": False if ok else self.env._("Some lists have problems."),
                "last_sync": fields.Datetime.now(),
            }
        )
        return ok

    @api.model
    def _cron_sync(self):
        for account in self.search([("auto_sync", "=", True), ("state", "=", "connected")]):
            account._sync()

    # -- webhooks --------------------------------------------------------------------------------

    def _handle_webhook(self, body, form):
        """A provider told us about a change: read just that contact and apply it."""
        self.ensure_one()
        for list_remote_id, email in self._dispatch("parse_webhook", body, form) or []:
            for link in self.link_ids:
                if not link.sync or (list_remote_id and link.remote_id != list_remote_id):
                    continue
                try:
                    member = self._dispatch("fetch_member", link, email)
                except MarketingError as exc:
                    _logger.warning("Marketing webhook for %s on account %s: %s", email, self.id, exc)
                    continue
                if member:
                    self._apply_members(link, [member])
