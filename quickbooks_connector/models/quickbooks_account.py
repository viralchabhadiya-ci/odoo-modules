import logging
import re
from datetime import timedelta
from urllib.parse import urlencode

import requests
from psycopg2.errors import SerializationFailure

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import hmac as hmac_tool

from .quickbooks_client import AUTH_URL, REVOKE_URL, SCOPE, QuickBooksClient, oauth_post
from .quickbooks_error import QuickBooksAuthError, QuickBooksError

_logger = logging.getLogger(__name__)

EXPIRY_MARGIN = timedelta(minutes=5)
INCOME_TYPES = ["Income", "Other Income"]
EXPENSE_TYPES = ["Expense", "Other Expense", "Cost of Goods Sold"]
BANK_TYPES = ["Bank", "Other Current Asset", "Credit Card"]


def normal(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


class QuickBooksAccount(models.Model):
    """A connection between one Odoo company and one QuickBooks Online company."""

    _name = "quickbooks.account"
    _description = "QuickBooks Online Connection"
    _rec_name = "company_id"

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    environment = fields.Selection(
        [("sandbox", "Sandbox (test company)"), ("production", "Production")], default="sandbox", required=True
    )
    client_id = fields.Char(
        string="Client ID", help="From the Keys & credentials page of your app at developer.intuit.com."
    )
    client_secret = fields.Char(string="Client secret")
    redirect_uri = fields.Char(compute="_compute_redirect_uri", string="Redirect URI")
    state = fields.Selection(
        [("draft", "Not connected"), ("connected", "Connected"), ("error", "Connect again")],
        default="draft",
        copy=False,
    )
    connection_message = fields.Char(copy=False)
    realm_id = fields.Char(string="QuickBooks company ID", copy=False)
    remote_name = fields.Char(string="QuickBooks company", copy=False)
    country_code = fields.Char(string="QuickBooks country", copy=False)

    auto_sync = fields.Boolean(string="Sync automatically", default=True)
    sync_from = fields.Date(
        string="Send documents dated from",
        default=fields.Date.context_today,
        help="Invoices, bills and payments dated before this are never sent. Set an earlier date to send history.",
    )
    push_invoices = fields.Boolean(string="Send invoices and credit notes", default=True)
    push_bills = fields.Boolean(string="Send vendor bills and credits", default=True)
    push_payments = fields.Boolean(string="Send payments", default=True)
    pull_contacts = fields.Boolean(string="Bring in customers and vendors", default=True)
    pull_payments = fields.Boolean(string="Bring in payments entered in QuickBooks", default=True)
    payment_journal_id = fields.Many2one(
        "account.journal",
        string="Journal for payments from QuickBooks",
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
    )
    income_ref_id = fields.Many2one(
        "quickbooks.ref",
        string="Income account",
        domain=f"[('account_id', '=', id), ('kind', '=', 'account'), ('account_type', 'in', {INCOME_TYPES})]",
        help="Used for products and invoice lines whose Odoo account has no QuickBooks match.",
    )
    expense_ref_id = fields.Many2one(
        "quickbooks.ref",
        string="Expense account",
        domain=f"[('account_id', '=', id), ('kind', '=', 'account'), ('account_type', 'in', {EXPENSE_TYPES})]",
    )
    bank_ref_id = fields.Many2one(
        "quickbooks.ref",
        string="Bank account for payments",
        domain=f"[('account_id', '=', id), ('kind', '=', 'account'), ('account_type', 'in', {BANK_TYPES})]",
        help="Where payments sent from Odoo are deposited (customers) or paid from (vendors).",
    )
    zero_tax_ref_id = fields.Many2one(
        "quickbooks.ref",
        string="Tax code for lines without tax",
        domain="[('account_id', '=', id), ('kind', '=', 'taxcode')]",
        help="Outside the US, QuickBooks wants a tax code on every line, also on lines that carry no tax.",
    )
    last_pull = fields.Datetime(copy=False)
    last_sync = fields.Datetime(copy=False)
    sync_errors = fields.Text(readonly=True, copy=False, help="What went wrong in the last round, record by record.")
    ref_ids = fields.One2many("quickbooks.ref", "account_id")
    link_ids = fields.One2many("quickbooks.link", "account_id")
    link_count = fields.Integer(compute="_compute_counts")
    error_count = fields.Integer(compute="_compute_counts")

    _unique_company = models.Constraint("unique (company_id)", "This company already has a QuickBooks connection.")

    # -- display ---------------------------------------------------------------------------------

    @api.depends("company_id")
    def _compute_redirect_uri(self):
        for account in self:
            account.redirect_uri = f"{account.get_base_url()}/quickbooks/oauth/callback"

    def _compute_counts(self):
        groups = {
            account.id: (count, errors)
            for account, count, errors in self.env["quickbooks.link"]._read_group(
                [("account_id", "in", self.ids)], ["account_id"], ["__count", "error:count"]
            )
        }
        for account in self:
            account.link_count, account.error_count = groups.get(account.id, (0, 0))

    def action_open_links(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Sync log"),
            "res_model": "quickbooks.link",
            "view_mode": "list,form",
            "domain": [("account_id", "=", self.id)],
        }

    def action_open_errors(self):
        action = self.action_open_links()
        action["domain"] = [("account_id", "=", self.id), ("error", "!=", False)]
        return action

    # -- connect (OAuth 2.0 authorization code) -------------------------------------------------------

    def _state_token(self):
        return f"{self.id}-{hmac_tool(self.env(su=True), 'quickbooks_oauth', self.id)[:24]}"

    @api.model
    def _from_state(self, state):
        """The connection a callback's `state` belongs to, or an empty recordset when it was not issued by us."""
        account_id, _sep, _signature = (state or "").partition("-")
        account = self.browse(int(account_id)).exists() if account_id.isdigit() else self.browse()
        return account if account and account._state_token() == state else self.browse()

    def action_connect(self):
        self.ensure_one()
        if not self.client_id or not self.client_secret:
            raise UserError(self.env._("Enter the Client ID and Client secret of your Intuit app first."))
        query = urlencode(
            {
                "client_id": self.client_id,
                "scope": SCOPE,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "state": self._state_token(),
            }
        )
        return {"type": "ir.actions.act_url", "url": f"{AUTH_URL}?{query}", "target": "self"}

    def _complete_oauth(self, code, realm_id):
        """The callback's code and company id: get the tokens, remember the company, load its lists."""
        self.ensure_one()
        body = oauth_post(
            self.client_id,
            self.client_secret,
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri},
        )
        self.realm_id = realm_id
        with self.pool.cursor() as cr:
            self._write_tokens(self.env(cr=cr), body)
        self._after_connect()

    def _after_connect(self):
        info = self._client().company_info()
        self.write(
            {
                "state": "connected",
                "connection_message": False,
                "remote_name": info.get("CompanyName") or info.get("LegalName"),
                "country_code": info.get("Country"),
            }
        )
        self._refresh_lists()

    def _write_tokens(self, env, body):
        values = {
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
            "expiry": fields.Datetime.now() + timedelta(seconds=int(body.get("expires_in", 3600))),
        }
        Token = env["quickbooks.token"].sudo()
        token = Token.search([("account_id", "=", self.id)])
        if token:
            token.write(values)
        else:
            Token.create({"account_id": self.id, **values})

    @staticmethod
    def _fresh(token):
        return token.expiry and token.expiry - fields.Datetime.now() > EXPIRY_MARGIN

    def _access_token(self, force=False):
        """A usable access token, refreshed when it is about to run out.

        Intuit rotates the refresh token on every use, so refreshing happens in its own transaction, committed at once: a
        sync that fails and rolls back must never lose the new token. The token has its own table so that this
        transaction never waits for a lock the sync holds."""
        self.ensure_one()
        token = self.env["quickbooks.token"].sudo().search([("account_id", "=", self.id)])
        if not token.refresh_token:
            raise QuickBooksAuthError("This company is not connected to QuickBooks.")
        if not force and self._fresh(token):
            return token.access_token
        for _try in range(2):
            try:
                with self.pool.cursor() as cr:
                    env = self.env(cr=cr)
                    cr.execute(
                        "SELECT id FROM quickbooks_token WHERE account_id = %s FOR UPDATE", [self.id]
                    )  # one refresher at a time
                    locked = env["quickbooks.token"].sudo().search([("account_id", "=", self.id)])
                    if not force and self._fresh(locked):  # someone else just did it
                        return locked.access_token
                    body = oauth_post(
                        self.client_id,
                        self.client_secret,
                        {"grant_type": "refresh_token", "refresh_token": locked.refresh_token},
                    )
                    self._write_tokens(env, body)
                self.env["quickbooks.token"].invalidate_model()
                return body["access_token"]
            except SerializationFailure:  # the other refresher committed while we waited: look again
                self.env["quickbooks.token"].invalidate_model()
                force = False
        raise QuickBooksError("The QuickBooks sign-in could not be refreshed. Try again in a moment.")

    def _client(self):
        self.ensure_one()
        return QuickBooksClient(
            self.environment, self.realm_id, self._access_token(), refresh=lambda: self._access_token(force=True)
        )

    def action_disconnect(self):
        for account in self:
            tokens = self.env["quickbooks.token"].sudo().search([("account_id", "=", account.id)])
            if tokens.refresh_token:
                try:  # best effort: Intuit forgets the connection; if it cannot be reached we forget our side anyway
                    requests.post(
                        REVOKE_URL,
                        json={"token": tokens.refresh_token},
                        auth=(account.client_id, account.client_secret),
                        headers={"Accept": "application/json"},
                        timeout=20,
                    )
                except Exception:
                    _logger.info("QuickBooks token could not be revoked", exc_info=True)
            tokens.unlink()
            account.write({"state": "draft", "realm_id": False})

    def action_test(self):
        self.ensure_one()
        try:
            info = self._client().company_info()
        except QuickBooksError as exc:
            if isinstance(exc, QuickBooksAuthError):  # shown, not raised: raising would undo the state change
                self.write({"state": "error", "connection_message": str(exc)})
            return self._notify(self.env._("Not connected"), str(exc), "danger")
        self.write({"state": "connected", "connection_message": False, "remote_name": info.get("CompanyName")})
        return self._notify(
            self.env._("Connected"),
            self.env._("QuickBooks company: %s", info.get("CompanyName") or self.realm_id),
            "success",
        )

    def _notify(self, title, message, kind):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": kind, "sticky": kind == "danger"},
        }

    # -- lists (accounts and tax codes) ------------------------------------------------------------------

    def action_refresh_lists(self):
        self.ensure_one()
        try:
            self._refresh_lists()
        except QuickBooksError as exc:
            raise UserError(str(exc)) from exc

    def _refresh_lists(self):
        self.ensure_one()
        client = self._client()
        Ref = self.env["quickbooks.ref"]
        existing = {
            (ref.kind, ref.remote_id): ref
            for ref in Ref.with_context(active_test=False).search([("account_id", "=", self.id)])
        }
        rates = {rate["Id"]: rate.get("RateValue") or 0.0 for rate in client.query_all("TaxRate", "Active = true")}
        seen = set()
        for row in client.query_all("Account", "Active = true"):
            seen.add(("account", row["Id"]))
            self._upsert_ref(
                existing,
                "account",
                row["Id"],
                row.get("Name") or row["Id"],
                number=row.get("AcctNum"),
                account_type=row.get("AccountType"),
            )
        for row in client.query_all("TaxCode", "Active = true"):
            seen.add(("taxcode", row["Id"]))
            listed = (
                (row.get("SalesTaxRateList") or {}).get("TaxRateDetail")
                or (row.get("PurchaseTaxRateList") or {}).get("TaxRateDetail")
                or []
            )
            rate = sum(rates.get((detail.get("TaxRateRef") or {}).get("value"), 0.0) for detail in listed)
            self._upsert_ref(existing, "taxcode", row["Id"], row.get("Name") or row["Id"], rate=rate)
        for key, ref in existing.items():
            if key not in seen and ref.active:
                ref.active = False
        self._match_odoo_records()
        self._default_refs()

    def _upsert_ref(self, existing, kind, remote_id, name, **values):
        values = {"name": name, "active": True, **values}
        ref = existing.get((kind, remote_id))
        if ref:
            ref.write({k: v for k, v in values.items() if ref[k] != v})
        else:
            existing[(kind, remote_id)] = self.env["quickbooks.ref"].create(
                {"account_id": self.id, "kind": kind, "remote_id": remote_id, **values}
            )

    def _match_odoo_records(self):
        """Point Odoo accounts and taxes at the QuickBooks entry with the same number, name or rate. Never overwrites a choice."""
        refs = self.ref_ids.filtered("active")
        accounts, taxcodes = refs.filtered(lambda r: r.kind == "account"), refs.filtered(lambda r: r.kind == "taxcode")
        by_number = {ref.number: ref for ref in accounts if ref.number}
        by_name = {normal(ref.name): ref for ref in accounts}
        for odoo_account in (
            self.env["account.account"]
            .with_company(self.company_id)
            .search([("company_ids", "in", self.company_id.id)])
        ):
            if not odoo_account.quickbooks_ref_id:
                match = by_number.get(odoo_account.code) or by_name.get(normal(odoo_account.name))
                if match:
                    odoo_account.quickbooks_ref_id = match
        by_tax_name = {normal(ref.name): ref for ref in taxcodes}
        for tax in self.env["account.tax"].search([("company_id", "=", self.company_id.id)]):
            if tax.quickbooks_taxcode_id and tax.quickbooks_taxcode_id.account_id == self:
                continue
            same_rate = taxcodes.filtered(lambda r, tax=tax: r.rate and abs(r.rate - abs(tax.amount)) < 0.0001)
            match = by_tax_name.get(normal(tax.name)) or (same_rate if len(same_rate) == 1 else None)
            if match:
                tax.quickbooks_taxcode_id = match

    def _default_refs(self):
        """First time only: pick sensible default accounts so that a plain setup needs no choices."""
        accounts = self.ref_ids.filtered(lambda r: r.kind == "account" and r.active)

        def first(types, *words):
            candidates = accounts.filtered(lambda r: r.account_type in types)
            for word in words:
                for ref in candidates:
                    if word in ref.name.lower():
                        return ref
            return candidates[:1]

        if not self.income_ref_id:
            self.income_ref_id = first(INCOME_TYPES, "sales", "services")
        if not self.expense_ref_id:
            self.expense_ref_id = first(EXPENSE_TYPES, "cost of goods", "supplies", "purchases")
        if not self.bank_ref_id:
            self.bank_ref_id = first(["Bank"], "checking", "bank")
        if not self.zero_tax_ref_id:
            codes = self.ref_ids.filtered(lambda r: r.kind == "taxcode" and r.active)
            for word in ("exempt", "out of scope", "no vat", "no tax", "zero", "free"):
                found = codes.filtered(lambda r, word=word: word in r.name.lower())[:1]
                if found:
                    self.zero_tax_ref_id = found
                    break
        if not self.payment_journal_id:
            self.payment_journal_id = self.env["account.journal"].search(
                [("type", "=", "bank"), ("company_id", "=", self.company_id.id)], limit=1
            )
