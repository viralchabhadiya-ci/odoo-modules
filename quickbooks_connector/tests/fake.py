import copy
import re

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.quickbooks_error import QuickBooksError

NAME_FIELDS = ("DisplayName", "Name")


class FakeQuickBooks:
    """Stands in for QuickBooksClient: keeps records in memory and applies QuickBooks' main rules."""

    def __init__(self):
        self.store = {}
        self.calls = []
        self.next_id = 100
        self.pending = {}
        self.where = {}
        self.total_factor = 1.0
        self.fail = {}
        self.info = {"CompanyName": "Sandbox Company", "Country": "US"}

    def calls_to(self, entity, op="save"):
        return [payload for kind, name, payload in self.calls if kind == op and name == entity]

    def remote(self, entity, **values):
        """A record that appears in QuickBooks on its own (entered there by someone)."""
        rid = str(self.next_id)
        self.next_id += 1
        record = {"Id": rid, "SyncToken": "0", "Active": True, **values}
        self.store.setdefault(entity, {})[rid] = record
        self.pending.setdefault(entity, []).append(rid)
        return record

    def change(self, entity, rid, **values):
        record = self.store[entity][rid]
        record.update(values)
        record["SyncToken"] = str(int(record["SyncToken"]) + 1)
        self.pending.setdefault(entity, []).append(rid)

    # -- the client's interface ---------------------------------------------------------------------

    def company_info(self):
        return self.info

    def read(self, entity, rid):
        return copy.deepcopy(self.store[entity][rid])

    def save(self, entity, payload, **_params):
        self.calls.append(("save", entity, copy.deepcopy(payload)))
        if entity in self.fail:
            raise self.fail.pop(entity)
        rows = self.store.setdefault(entity, {})
        if "Id" in payload:
            current = rows[payload["Id"]]
            if payload.get("SyncToken") != current["SyncToken"]:
                raise QuickBooksError("Stale Object Error: you and another user are working on the same thing", "5010")
            current.update({k: v for k, v in payload.items() if k != "sparse"})
            current["SyncToken"] = str(int(current["SyncToken"]) + 1)
            return copy.deepcopy(current)
        name = next((payload[f] for f in NAME_FIELDS if payload.get(f)), None)
        if name:
            for other in ("Customer", "Vendor") if entity in ("Customer", "Vendor") else (entity,):
                if any(r.get(f) == name for r in self.store.get(other, {}).values() for f in NAME_FIELDS):
                    raise QuickBooksError(
                        f"Duplicate Name Exists Error: The name {name} supplied already exists.", "6240"
                    )
        rid = str(self.next_id)
        self.next_id += 1
        record = {**payload, "Id": rid, "SyncToken": "0", "Active": True}
        if "Line" in payload:
            record["TotalAmt"] = round(sum(line.get("Amount", 0) for line in payload["Line"]) * self.total_factor, 2)
        rows[rid] = record
        return copy.deepcopy(record)

    def void(self, entity, payload):
        self.calls.append(("void", entity, copy.deepcopy(payload)))
        self.store[entity][payload["Id"]]["voided"] = True
        return {}

    def delete(self, entity, payload):
        self.calls.append(("delete", entity, copy.deepcopy(payload)))
        self.store[entity].pop(payload["Id"], None)
        return {}

    def query(self, sql):
        entity, field, value = re.match(r"select \* from (\w+) where (\w+) = '(.*)'", sql).groups()
        rows = [r for r in self.store.get(entity, {}).values() if r.get(field) == value]
        return {entity: copy.deepcopy(rows)} if rows else {}

    def query_all(self, entity, where=""):
        self.where[entity] = where
        rows = self.store.get(entity, {})
        if where.startswith("MetaData"):
            ids, self.pending[entity] = self.pending.get(entity, []), []
            return [copy.deepcopy(rows[i]) for i in dict.fromkeys(ids) if i in rows]
        return [copy.deepcopy(r) for r in rows.values()]


class QuickBooksCase(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.env.user.group_ids |= cls.env.ref("quickbooks_connector.group_quickbooks_manager")
        cls.account = cls.env["quickbooks.account"].create(
            {
                "company_id": cls.company.id,
                "client_id": "cid",
                "client_secret": "secret",
                "state": "connected",
                "realm_id": "9130",
                "country_code": "US",
                "sync_from": "2020-01-01",
            }
        )
        cls.token = (
            cls.env["quickbooks.token"]
            .sudo()
            .create(
                {
                    "account_id": cls.account.id,
                    "refresh_token": "r1",
                    "access_token": "a1",
                    "expiry": "2999-01-01 00:00:00",
                }
            )
        )
        cls.customer = cls.env["res.partner"].create(
            {"name": "QB Acme", "is_company": True, "email": "ap@acme.test", "city": "Dallas"}
        )
        cls.vendor = cls.env["res.partner"].create({"name": "QB Supplies", "is_company": True})

    def setUp(self):
        super().setUp()
        self.registry_enter_test_mode()  # the token refresh uses its own cursor; here it shares the test's transaction
        self.fake = FakeQuickBooks()
        self.refs()
        self.patch(type(self.env["quickbooks.account"]), "_client", lambda account: self.fake)

    def invoice(
        self,
        partner=None,
        price=100.0,
        qty=1.0,
        move_type="out_invoice",
        post=True,
        date="2026-10-01",
        taxes=None,
        product=None,
        discount=0.0,
    ):
        line = {
            "name": "Consulting",
            "quantity": qty,
            "price_unit": price,
            "discount": discount,
            "tax_ids": [(6, 0, (taxes or self.env["account.tax"]).ids)],
        }
        if product:
            line["product_id"] = product.id
        move = self.env["account.move"].create(
            {
                "move_type": move_type,
                "partner_id": (partner or (self.vendor if move_type.startswith("in_") else self.customer)).id,
                "invoice_date": date,
                "invoice_line_ids": [(0, 0, line)],
                **({"ref": "VB-77"} if move_type.startswith("in_") else {}),
            }
        )
        if post:
            move.action_post()
        return move

    def refs(self):
        """Accounts and tax codes as 'Refresh lists' would have loaded them; also chosen as the defaults."""
        Ref = self.env["quickbooks.ref"]

        def make(kind, rid, name, **values):
            return Ref.create({"account_id": self.account.id, "kind": kind, "remote_id": rid, "name": name, **values})

        self.income = make("account", "11", "Sales", account_type="Income")
        self.expense = make("account", "12", "Supplies", account_type="Expense")
        self.bank = make("account", "13", "Checking", account_type="Bank")
        self.vat10 = make("taxcode", "T10", "VAT 10%", rate=10.0)
        self.exempt = make("taxcode", "T0", "Exempt", rate=0.0)
        self.account.write(
            {
                "income_ref_id": self.income.id,
                "expense_ref_id": self.expense.id,
                "bank_ref_id": self.bank.id,
                "zero_tax_ref_id": self.exempt.id,
            }
        )

    def later(self):
        """Pretend an hour has passed since the last round (write dates are the transaction's start in tests)."""
        self.env.flush_all()
        self.env.cr.execute("UPDATE quickbooks_link SET last_sync = last_sync - interval '1 hour'")
        self.env["quickbooks.link"].invalidate_model()

    def sync(self):
        return self.account._sync()

    def pay(self, move, amount, date="2026-10-05"):
        return (
            self.env["account.payment.register"]
            .with_context(active_model="account.move", active_ids=move.ids)
            .create(
                {"amount": amount, "payment_date": date, "journal_id": self.company_data["default_journal_bank"].id}
            )
            ._create_payments()
        )

    def link(self, record, entity):
        return self.env["quickbooks.link"].search(
            [
                ("account_id", "=", self.account.id),
                ("entity", "=", entity),
                ("res_model", "=", record._name),
                ("res_id", "=", record.id),
            ]
        )
