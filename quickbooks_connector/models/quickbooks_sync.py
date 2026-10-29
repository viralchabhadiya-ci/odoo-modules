import hashlib
import json
import logging
from collections import Counter
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import email_split, html2plaintext

from .quickbooks_client import quote
from .quickbooks_error import QuickBooksAuthError, QuickBooksError
from .quickbooks_link import MAX_ATTEMPTS

_logger = logging.getLogger(__name__)

BATCH = 100
MARGIN = timedelta(minutes=2)
WAIT = timedelta(hours=1)  # how long a payment whose invoices are not in QuickBooks yet is left alone
MAX_MESSAGES = 30
DUPLICATE, STALE = "6240", "5010"
VOID = "void"
MOVE_ENTITIES = {
    "out_invoice": "Invoice",
    "out_refund": "CreditMemo",
    "in_invoice": "Bill",
    "in_refund": "VendorCredit",
}
SALES = ("Invoice", "CreditMemo")
# QuickBooks payment entity -> entity of the documents it pays
PAYMENTS = {"Payment": "Invoice", "BillPayment": "Bill"}


def fingerprint(payload):
    return hashlib.md5(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()  # noqa: S324


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def clean_name(name, limit):
    """Item and customer names may not hold colons, tabs or line breaks."""
    return " ".join((name or "").replace(":", "-").split())[:limit]


class QuickBooksSync(models.Model):
    """What moves between Odoo and QuickBooks. Odoo is the source for documents, either side may change contacts."""

    _inherit = "quickbooks.account"

    # -- entry points ----------------------------------------------------------------------------------

    @api.model
    def _cron_sync(self):
        for account in self.search([("state", "=", "connected"), ("auto_sync", "=", True)]):
            account._sync()

    def action_sync_now(self):
        self.ensure_one()
        stats = self._sync()
        parts = [f"{n} {what}" for what, n in sorted((stats or {}).items()) if n]
        failed = (stats or {}).get("failed")
        return self._notify(
            self.env._("QuickBooks sync"),
            ", ".join(parts) or self.env._("Nothing to send or bring in."),
            "warning" if failed else "success",
        )

    def action_retry_failed(self):
        self.ensure_one()
        self.link_ids.filtered("error").write({"attempts": 0})
        return self.action_sync_now()

    def _send_now(self, moves):
        """Send these documents at once, whatever their date. Errors are shown to the user."""
        stats = Counter()
        self.sync_errors = False
        try:
            client = self._client()
            for move in moves.filtered(lambda m: m.move_type in MOVE_ENTITIES):
                self._link(MOVE_ENTITIES[move.move_type], move).attempts = 0
                self._push_move(client, move, stats)
        except QuickBooksError as exc:
            raise UserError(str(exc)) from exc
        if stats["failed"]:
            raise UserError(self.sync_errors)
        return stats

    def _sync(self):
        """One round: send what changed in Odoo, then bring in what changed in QuickBooks. Returns counts."""
        self.ensure_one()
        stats = Counter()
        self.sync_errors = False
        try:
            client = self._client()
            self._push_all(client, stats)
            self._pull_all(client, stats)
        except QuickBooksAuthError as exc:
            self.write({"state": "error", "connection_message": str(exc)})
        except QuickBooksError as exc:
            self.connection_message = str(exc)
        else:
            self.connection_message = False
        self.last_sync = fields.Datetime.now()
        return stats

    def _attempt(self, stats, what, label, work, link=None):
        """Run one record's work in a savepoint. A failure goes on its link and on the connection, and the round carries on.
        `work` returning False means there was nothing to do."""
        try:
            with self.env.cr.savepoint():
                done = work()
        except QuickBooksAuthError:
            raise
        except Exception as exc:
            _logger.info("QuickBooks %s failed for %s", what, label, exc_info=not isinstance(exc, QuickBooksError))
            if link:
                link.write({"error": str(exc)[:2000], "attempts": link.attempts + 1})
            stats["failed"] += 1
            if stats["failed"] <= MAX_MESSAGES:
                self.sync_errors = f"{self.sync_errors or ''}{label}: {exc}\n"
        else:
            if done is not False:
                stats[what] += 1

    def _link(self, entity, record):
        Link = self.env["quickbooks.link"]
        domain = [
            ("account_id", "=", self.id),
            ("entity", "=", entity),
            ("res_model", "=", record._name),
            ("res_id", "=", record.id),
        ]
        return Link.search(domain, limit=1) or Link.create(
            {"account_id": self.id, "entity": entity, "res_model": record._name, "res_id": record.id}
        )

    def _find_link(self, entity, remote_id):
        return self.env["quickbooks.link"].search(
            [("account_id", "=", self.id), ("entity", "=", entity), ("remote_id", "=", str(remote_id))], limit=1
        )

    # -- saving to QuickBooks ------------------------------------------------------------------------------

    def _save(self, client, link, payload, duplicate=None):
        """Create or update the record behind `link`. Skips what has not changed; copes with stale tokens and duplicate names.

        `duplicate(body)` is called when QuickBooks says the name exists: it returns the existing record to adopt, or None
        after changing the body's name."""
        digest = fingerprint(payload)
        entity = link.entity
        if link.remote_id and link.payload_hash == digest and not link.error:
            link.last_sync = fields.Datetime.now()
            return None
        body = dict(payload)
        if link.remote_id and link.payload_hash != VOID:
            body.update({"Id": link.remote_id, "SyncToken": link.sync_token, "sparse": True})
        saved = None
        for _attempt in range(3):
            try:
                saved = client.save(entity, body)
                break
            except QuickBooksError as exc:
                if exc.code == STALE and "Id" in body:
                    body["SyncToken"] = client.read(entity, body["Id"]).get("SyncToken")
                elif exc.code == DUPLICATE and duplicate and "Id" not in body:
                    existing = duplicate(body)
                    if existing:
                        body.update({"Id": existing["Id"], "SyncToken": existing.get("SyncToken"), "sparse": True})
                else:
                    raise
        if saved is None:
            raise QuickBooksError(f"QuickBooks did not accept the {entity}.")
        link.write(
            {
                "remote_id": saved.get("Id"),
                "sync_token": saved.get("SyncToken"),
                "payload_hash": digest,
                "last_sync": fields.Datetime.now(),
                "error": False,
                "attempts": 0,
            }
        )
        return saved

    # -- contacts ---------------------------------------------------------------------------------------------

    def _partner_payload(self, partner):
        name = clean_name(
            partner.display_name if not partner.parent_id else partner.commercial_partner_id.display_name, 100
        )
        payload = {"DisplayName": name, "Active": partner.active}
        if partner.is_company:
            payload["CompanyName"] = name[:50]
        else:
            first, _sep, last = partner.name.partition(" ") if partner.name else ("", "", "")
            payload.update({"GivenName": first[:25], "FamilyName": last[:25]})
        emails = email_split(partner.email or "")
        if emails:
            payload["PrimaryEmailAddr"] = {"Address": emails[0]}
        if partner.phone:
            payload["PrimaryPhone"] = {"FreeFormNumber": partner.phone[:21]}
        if "mobile" in partner._fields and partner.mobile:
            payload["Mobile"] = {"FreeFormNumber": partner.mobile[:21]}
        if partner.website:
            payload["WebAddr"] = {"URI": partner.website[:1000]}
        address = {
            "Line1": partner.street,
            "Line2": partner.street2,
            "City": partner.city,
            "CountrySubDivisionCode": partner.state_id.code,
            "PostalCode": partner.zip,
            "Country": partner.country_id.name,
        }
        address = {key: value for key, value in address.items() if value}
        if address:
            payload["BillAddr"] = address
        return payload

    def _ensure_partner(self, client, partner, entity):
        """The QuickBooks id of a customer or vendor, creating it the first time."""
        partner = partner.commercial_partner_id
        link = self._link(entity, partner)
        if not link.remote_id:
            self._save_partner(client, link, partner)
        return link.remote_id

    def _save_partner(self, client, link, partner):
        entity, other = link.entity, "Vendor" if link.entity == "Customer" else "Customer"
        payload = self._partner_payload(partner)
        if self._find_link_by_record(other, partner) and not link.remote_id:
            payload["DisplayName"] = clean_name(
                f"{payload['DisplayName']} ({entity})", 100
            )  # names are shared by both lists

        def duplicate(body):
            found = client.query(f"select * from {entity} where DisplayName = {quote(body['DisplayName'])}").get(entity)
            if found and not self.env["quickbooks.link"].search_count(
                [("account_id", "=", self.id), ("entity", "=", entity), ("remote_id", "=", found[0]["Id"])]
            ):
                return found[0]
            body["DisplayName"] = clean_name(f"{body['DisplayName']} ({entity})", 100)
            return None

        return self._save(client, link, payload, duplicate)

    def _find_link_by_record(self, entity, record):
        return self.env["quickbooks.link"].search(
            [
                ("account_id", "=", self.id),
                ("entity", "=", entity),
                ("res_model", "=", record._name),
                ("res_id", "=", record.id),
                ("remote_id", "!=", False),
            ],
            limit=1,
        )

    def _push_contacts(self, client, stats):
        """Contacts already in QuickBooks are updated when changed in Odoo (new ones are created when a document needs them)."""
        self.env.flush_all()
        self.env.cr.execute(
            """SELECT l.id FROM quickbooks_link l JOIN res_partner p ON l.res_model = 'res.partner' AND p.id = l.res_id
                WHERE l.account_id = %s AND l.entity IN ('Customer', 'Vendor') AND l.remote_id IS NOT NULL
                  AND (p.write_date > l.last_sync OR l.error IS NOT NULL) AND l.attempts < %s
                ORDER BY l.id LIMIT %s""",
            [self.id, MAX_ATTEMPTS, BATCH],
        )
        for link in self.env["quickbooks.link"].browse([row[0] for row in self.env.cr.fetchall()]):
            partner = self.env["res.partner"].browse(link.res_id)
            self._attempt(
                stats,
                "contacts sent",
                partner.display_name,
                lambda link=link, partner=partner: self._save_partner(client, link, partner),
                link,
            )

    # -- items ---------------------------------------------------------------------------------------------------

    def _income_ref(self, product):
        ref = False
        if product:
            ref = (
                product.product_tmpl_id.with_company(self.company_id)
                ._get_product_accounts()
                .get("income")
                .quickbooks_ref_id
            )
        return (ref or self.income_ref_id).remote_id

    def _ensure_item(self, client, line):
        """The QuickBooks item for an invoice line: one per product, or a shared one for lines without a product."""
        product = line.product_id
        record = product if product else self
        link = self._link("Item", record)
        if link.remote_id:
            return link.remote_id
        if not self._income_ref(product):
            raise QuickBooksError("Choose the QuickBooks income account in the connection settings, then try again.")
        name = clean_name(
            product.with_context(display_default_code=False).display_name if product else "Odoo sales line", 100
        )
        payload = {
            "Name": name,
            "Type": "Service" if not product or product.type == "service" else "NonInventory",
            "IncomeAccountRef": {"value": self._income_ref(product)},
            "Active": True,
        }
        if product:
            if product.default_code:
                payload["Sku"] = product.default_code[:100]
            payload["UnitPrice"] = product.list_price
        if self.expense_ref_id:
            payload["ExpenseAccountRef"] = {"value": self.expense_ref_id.remote_id}

        def duplicate(body):
            found = client.query(f"select * from Item where Name = {quote(body['Name'])}").get("Item")
            if found and not self.env["quickbooks.link"].search_count(
                [("account_id", "=", self.id), ("entity", "=", "Item"), ("remote_id", "=", found[0]["Id"])]
            ):
                return found[0]
            body["Name"] = clean_name(f"{body['Name']} (Odoo {record.id})", 100)
            return None

        self._save(client, link, payload, duplicate)
        return link.remote_id

    # -- documents --------------------------------------------------------------------------------------------------

    def _tax_code(self, line):
        taxes = line.tax_ids.flatten_taxes_hierarchy()
        if (self.country_code or "").upper() == "US":
            return "TAX" if taxes else "NON"
        if not taxes:
            if not self.zero_tax_ref_id:
                raise QuickBooksError(
                    "Choose the QuickBooks tax code for lines without tax in the connection settings."
                )
            return self.zero_tax_ref_id.remote_id
        mapped = taxes.filtered("quickbooks_taxcode_id")
        if not mapped:
            raise QuickBooksError(
                f"The tax '{taxes[0].name}' has no QuickBooks tax code. Open the tax and choose one, or press Refresh lists."
            )
        return mapped[0].quickbooks_taxcode_id.remote_id

    def _expense_account(self, line):
        return (line.account_id.with_company(self.company_id).quickbooks_ref_id or self.expense_ref_id).remote_id

    def _document_payload(self, client, move, entity):
        sale = entity in SALES
        partner = move.commercial_partner_id
        payload = {
            "TxnDate": str(move.invoice_date or move.date),
            "PrivateNote": f"Odoo {move.name}",
            "GlobalTaxCalculation": "TaxExcluded",
            "Line": [],
        }
        if sale:
            payload["DocNumber"] = (move.name or "")[:21]
            payload["CustomerRef"] = {"value": self._ensure_partner(client, partner, "Customer")}
            emails = email_split(partner.email or "")
            if emails and entity == "Invoice":
                payload["BillEmail"] = {"Address": emails[0]}
            if move.narration:
                payload["CustomerMemo"] = {"value": html2plaintext(move.narration)[:1000]}
        else:
            payload["DocNumber"] = (move.ref or move.name or "")[:21]
            payload["VendorRef"] = {"value": self._ensure_partner(client, partner, "Vendor")}
        if move.invoice_date_due and entity in ("Invoice", "Bill"):
            payload["DueDate"] = str(move.invoice_date_due)
        if move.currency_id != move.company_currency_id:
            payload["CurrencyRef"] = {"value": move.currency_id.name}
            payload["ExchangeRate"] = round(
                move.currency_id._get_conversion_rate(
                    move.currency_id, move.company_currency_id, move.company_id, move.invoice_date or move.date
                ),
                6,
            )
        for line in move.invoice_line_ids.sorted("sequence"):
            if line.display_type in ("line_section", "line_note"):
                payload["Line"].append(
                    {
                        "DetailType": "DescriptionOnly",
                        "Description": (line.name or "")[:4000],
                        "DescriptionLineDetail": {},
                    }
                )
                continue
            if line.display_type != "product":
                continue
            amount = move.currency_id.round(line.price_subtotal)
            description = (line.name or "")[:4000]
            if sale:
                unit = round(amount / line.quantity, 6) if line.quantity else line.price_unit
                detail = {
                    "ItemRef": {"value": self._ensure_item(client, line)},
                    "Qty": line.quantity,
                    "UnitPrice": unit,
                    "TaxCodeRef": {"value": self._tax_code(line)},
                }
                payload["Line"].append(
                    {
                        "DetailType": "SalesItemLineDetail",
                        "Amount": amount,
                        "Description": description,
                        "SalesItemLineDetail": detail,
                    }
                )
            else:
                account = self._expense_account(line)
                if not account:
                    raise QuickBooksError(
                        "Choose the QuickBooks expense account in the connection settings, then try again."
                    )
                detail = {"AccountRef": {"value": account}, "TaxCodeRef": {"value": self._tax_code(line)}}
                payload["Line"].append(
                    {
                        "DetailType": "AccountBasedExpenseLineDetail",
                        "Amount": amount,
                        "Description": description,
                        "AccountBasedExpenseLineDetail": detail,
                    }
                )
        if not any(line["DetailType"] != "DescriptionOnly" for line in payload["Line"]):
            raise QuickBooksError("The document has no lines to send.")
        return payload

    def _push_move(self, client, move, stats):
        entity = MOVE_ENTITIES[move.move_type]
        link = self._link(entity, move)
        if move.state != "posted":
            self._attempt(stats, "documents withdrawn", move.name, lambda: self._withdraw(client, link), link)
            return

        def work():
            payload = self._document_payload(client, move, entity)
            saved = self._save(client, link, payload)
            if saved:
                total = abs(saved.get("TotalAmt") or 0.0)
                expected = abs(move.amount_total)
                warn = (
                    f"QuickBooks total {total:.2f} differs from Odoo {expected:.2f}; check the tax setup."
                    if abs(total - expected) > 0.01
                    else False
                )
                link.note = warn

        self._attempt(stats, "documents sent", move.name, work, link)

    def _withdraw(self, client, link):
        """A document that left 'posted' in Odoo: void the invoice or credit memo in QuickBooks, delete the bill."""
        if not link.remote_id or link.payload_hash == VOID:
            return
        body = {"Id": link.remote_id, "SyncToken": link.sync_token}
        if link.entity in SALES:
            client.void(link.entity, body)
        else:
            client.delete(link.entity, body)
        link.write(
            {"payload_hash": VOID, "last_sync": fields.Datetime.now(), "error": False, "attempts": 0, "note": False}
        )

    def _moves_to_push(self, move_types):
        self.env.flush_all()
        self.env.cr.execute(
            """SELECT m.id FROM account_move m
                LEFT JOIN quickbooks_link l ON l.account_id = %(account)s AND l.res_model = 'account.move' AND l.res_id = m.id
                WHERE m.company_id = %(company)s AND m.move_type IN %(types)s AND m.invoice_date >= %(since)s
                  AND ((m.state = 'posted' AND (l.id IS NULL
                          OR (l.attempts < %(attempts)s AND (l.error IS NOT NULL OR l.last_sync IS NULL OR m.write_date > l.last_sync))))
                    OR (m.state <> 'posted' AND l.remote_id IS NOT NULL AND l.payload_hash <> %(void)s AND l.attempts < %(attempts)s))
                ORDER BY m.id LIMIT %(limit)s""",
            {
                "account": self.id,
                "company": self.company_id.id,
                "types": tuple(move_types),
                "since": self.sync_from or fields.Date.today(),
                "attempts": MAX_ATTEMPTS,
                "void": VOID,
                "limit": BATCH,
            },
        )
        return self.env["account.move"].browse([row[0] for row in self.env.cr.fetchall()])

    # -- payments -------------------------------------------------------------------------------------------------------

    def _applied_amounts(self, payment):
        """{invoice or bill: amount of this payment applied to it, in the document's currency}."""
        applied = {}
        for line in payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type in ("asset_receivable", "liability_payable")
        ):
            for partial in line.matched_debit_ids | line.matched_credit_ids:
                other = partial.debit_move_id if partial.credit_move_id == line else partial.credit_move_id
                amount = (
                    partial.debit_amount_currency if other == partial.debit_move_id else partial.credit_amount_currency
                )
                if other.move_id.is_invoice(include_receipts=False):
                    applied[other.move_id] = applied.get(other.move_id, 0.0) + amount
        return applied

    def _payment_payload(self, client, payment):
        inbound = payment.payment_type == "inbound"
        entity, doc_entity = ("Payment", "Invoice") if inbound else ("BillPayment", "Bill")
        lines = []
        for move, amount in self._applied_amounts(payment).items():
            doc_link = self._find_link_by_record(MOVE_ENTITIES[move.move_type], move)
            if doc_link and doc_link.entity == doc_entity and doc_link.payload_hash != VOID and amount > 0:
                lines.append(
                    {"Amount": round(amount, 2), "LinkedTxn": [{"TxnId": doc_link.remote_id, "TxnType": doc_entity}]}
                )
        if not lines:
            return entity, None
        partner = payment.partner_id.commercial_partner_id
        payload = {
            "TotalAmt": round(payment.amount, 2),
            "TxnDate": str(payment.date),
            "PrivateNote": f"Odoo {payment.name}",
            "Line": lines,
        }
        if payment.currency_id != self.company_id.currency_id:
            payload["CurrencyRef"] = {"value": payment.currency_id.name}
        if inbound:
            payload["CustomerRef"] = {"value": self._ensure_partner(client, partner, "Customer")}
            payload["PaymentRefNum"] = (payment.name or "")[:21]
            if self.bank_ref_id:
                payload["DepositToAccountRef"] = {"value": self.bank_ref_id.remote_id}
        else:
            if not self.bank_ref_id:
                raise QuickBooksError("Choose the QuickBooks bank account for payments in the connection settings.")
            payload["VendorRef"] = {"value": self._ensure_partner(client, partner, "Vendor")}
            payload["PayType"] = "Check"
            payload["CheckPayment"] = {"BankAccountRef": {"value": self.bank_ref_id.remote_id}}
        return entity, payload

    def _push_payment(self, client, payment, stats):
        link = self._link("Payment" if payment.payment_type == "inbound" else "BillPayment", payment)

        def work():
            _entity, payload = self._payment_payload(client, payment)
            if payload is None:  # its invoices are not in QuickBooks (yet)
                link.last_sync = fields.Datetime.now()
                return False
            self._save(client, link, payload)

        self._attempt(stats, "payments sent", payment.name, work, link)

    def _payments_to_push(self):
        self.env.flush_all()
        self.env.cr.execute(
            """SELECT p.id FROM account_payment p JOIN account_move m ON m.id = p.move_id
                LEFT JOIN quickbooks_link l ON l.account_id = %(account)s AND l.res_model = 'account.payment' AND l.res_id = p.id
                WHERE m.company_id = %(company)s AND m.state = 'posted' AND p.date >= %(since)s
                  AND p.partner_id IS NOT NULL AND p.payment_type IN ('inbound', 'outbound')
                  AND (l.id IS NULL OR (l.remote_id IS NULL AND l.attempts < %(attempts)s
                                        AND (l.error IS NOT NULL OR l.last_sync IS NULL OR l.last_sync < %(stale)s)))
                ORDER BY l.last_sync NULLS FIRST, p.id LIMIT %(limit)s""",
            {
                "account": self.id,
                "company": self.company_id.id,
                "since": self.sync_from or fields.Date.today(),
                "attempts": MAX_ATTEMPTS,
                "stale": fields.Datetime.now() - WAIT,
                "limit": BATCH,
            },
        )
        return self.env["account.payment"].browse([row[0] for row in self.env.cr.fetchall()])

    def _push_all(self, client, stats):
        self._push_contacts(client, stats)
        if self.push_invoices:
            for move in self._moves_to_push(["out_invoice", "out_refund"]):
                self._push_move(client, move, stats)
        if self.push_bills:
            for move in self._moves_to_push(["in_invoice", "in_refund"]):
                self._push_move(client, move, stats)
        if self.push_payments:
            for payment in self._payments_to_push():
                self._push_payment(client, payment, stats)

    # -- bringing in -------------------------------------------------------------------------------------------------------

    def _since_clause(self):
        return f"MetaData.LastUpdatedTime > {quote(iso(self.last_pull - MARGIN))}" if self.last_pull else ""

    def _pull_all(self, client, stats):
        started = fields.Datetime.now()
        where = self._since_clause()
        if self.pull_contacts:
            for entity in ("Customer", "Vendor"):
                for row in client.query_all(entity, where):
                    self._pull_partner(stats, entity, row)
        if self.pull_payments:
            for entity in PAYMENTS:
                for row in client.query_all(entity, where):
                    self._pull_payment(stats, entity, row)
        self.last_pull = started

    def _partner_values(self, entity, row):
        email = (row.get("PrimaryEmailAddr") or {}).get("Address")
        address = row.get("BillAddr") or {}
        company = row.get("CompanyName")
        values = {
            "name": row.get("DisplayName") or company or row.get("FullyQualifiedName"),
            "is_company": bool(company) or not (row.get("GivenName") or row.get("FamilyName")),
            "email": email or False,
            "phone": (row.get("PrimaryPhone") or {}).get("FreeFormNumber") or False,
            "website": (row.get("WebAddr") or {}).get("URI") or False,
            "street": address.get("Line1") or False,
            "street2": address.get("Line2") or False,
            "city": address.get("City") or False,
            "zip": address.get("PostalCode") or False,
        }
        if "mobile" in self.env["res.partner"]._fields:
            values["mobile"] = (row.get("Mobile") or {}).get("FreeFormNumber") or False
        country = (
            self.env["res.country"].search([("name", "=ilike", address.get("Country") or "")], limit=1)
            if address.get("Country")
            else False
        )
        if country:
            values["country_id"] = country.id
            code = address.get("CountrySubDivisionCode")
            state = (
                self.env["res.country.state"].search(
                    [("country_id", "=", country.id), ("code", "=ilike", code or "")], limit=1
                )
                if code
                else False
            )
            values["state_id"] = state.id if state else False
        return values

    def _pull_partner(self, stats, entity, row):
        remote_id = row["Id"]
        link = self._find_link(entity, remote_id)
        Partner = self.env["res.partner"]
        if link:
            partner = Partner.browse(link.res_id).exists()
            if not partner or row.get("SyncToken") == link.sync_token:
                return
            unchanged_in_odoo = fingerprint(self._partner_payload(partner)) == link.payload_hash

            def work():
                if unchanged_in_odoo and row.get("Active", True):
                    partner.write(self._partner_values(entity, row))
                    link.write(
                        {
                            "payload_hash": fingerprint(self._partner_payload(partner)),
                            "last_sync": fields.Datetime.now(),
                        }
                    )
                link.write(
                    {"sync_token": row.get("SyncToken"), "error": False}
                )  # changed in both places: Odoo wins at the next push

            self._attempt(stats, "contacts updated", partner.display_name, work, link)
            return
        if not row.get("Active", True):
            return
        values = self._partner_values(entity, row)
        if not values["name"]:
            return
        partner = self._match_partner(values)

        def work():
            record = partner or Partner.create(values)
            rank = "customer_rank" if entity == "Customer" else "supplier_rank"
            if rank in record._fields and not record[rank]:
                record[rank] = 1
            real = self._link(entity, record)
            real.write(
                {
                    "remote_id": remote_id,
                    "sync_token": row.get("SyncToken"),
                    "payload_hash": fingerprint(self._partner_payload(record)),
                    "last_sync": fields.Datetime.now(),
                }
            )

        self._attempt(stats, "contacts brought in", values["name"], work)

    def _match_partner(self, values):
        """An existing contact that is the same person or company: same email, else same name. Not linked to this entity yet."""
        Partner = self.env["res.partner"]
        for domain in (
            [("email", "=ilike", values["email"])] if values.get("email") else None,
            [("name", "=ilike", values["name"]), ("is_company", "=", values["is_company"])],
        ):
            if domain:
                found = Partner.search(domain + [("parent_id", "=", False)], limit=1)
                if found:
                    return found
        return Partner

    def _pull_payment(self, stats, entity, row):
        doc_entity = PAYMENTS[entity]
        key = row["Id"]
        Link = self.env["quickbooks.link"]
        known = Link.search_count(
            [
                ("account_id", "=", self.id),
                ("entity", "=", entity),
                "|",
                ("remote_id", "=", key),
                ("remote_id", "=like", f"{key}/%"),
            ]
        )
        if known or not self.payment_journal_id:
            return
        for line in row.get("Line") or []:
            for linked in line.get("LinkedTxn") or []:
                if linked.get("TxnType") != doc_entity:
                    continue
                doc_link = self._find_link(doc_entity, linked["TxnId"])
                move = self.env["account.move"].browse(doc_link.res_id).exists() if doc_link else None
                if (
                    not move
                    or move.state != "posted"
                    or move.payment_state in ("paid", "in_payment", "reversed")
                    or not line.get("Amount")
                ):
                    continue
                marker = f"{key}/{linked['TxnId']}"
                self._attempt(
                    stats,
                    "payments brought in",
                    f"{entity} {key}",
                    lambda move=move, line=line, marker=marker: self._register_payment(entity, move, line, row, marker),
                )

    def _register_payment(self, entity, move, line, row, marker):
        wizard = (
            self.env["account.payment.register"]
            .with_context(active_model="account.move", active_ids=move.ids)
            .create(
                {
                    "amount": min(line["Amount"], move.amount_residual),
                    "payment_date": row.get("TxnDate") or fields.Date.today(),
                    "journal_id": self.payment_journal_id.id,
                    "communication": f"QuickBooks {entity.lower()} {row.get('PaymentRefNum') or row['Id']}",
                }
            )
        )
        payments = wizard._create_payments()
        for payment in payments:
            real = self._link(entity, payment)
            real.write(
                {
                    "remote_id": marker,
                    "sync_token": row.get("SyncToken"),
                    "last_sync": fields.Datetime.now(),
                    "payload_hash": "pulled",
                }
            )
