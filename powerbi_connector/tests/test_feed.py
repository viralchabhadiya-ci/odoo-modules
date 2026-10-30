import base64
import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools import mute_logger

from ..models.powerbi_client import PowerBIClient, PowerBIError
from ..models.powerbi_feed import hash_token, token_from_header

BASE = "https://odoo.example.com/powerbi/db/odata/1"


def basic(token, user="odoo"):
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


class FeedCase:
    def make_feed(self, **vals):
        feed = self.env["powerbi.feed"].create({"name": "BI", "user_id": self.env.ref("base.user_admin").id, **vals})
        self.token = feed.action_new_token()["context"]["default_token"]
        return feed

    def add_partners(self, feed, **vals):
        return self.env["powerbi.entity"].create(
            {
                "feed_id": feed.id,
                "model_id": self.env["ir.model"]._get("res.partner").id,
                "name": "partners",
                "domain": "[('name', '=like', 'BI Test%')]",
                **vals,
            }
        )

    def ask(self, feed, path="", **args):
        status, content_type, body = feed._odata(path, args, BASE)
        parsed = json.loads(body) if "json" in content_type else body
        return status, parsed


@tagged("post_install", "-at_install")
class TestPowerBIFeed(FeedCase, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.germany = cls.env.ref("base.de")
        cls.partners = cls.env["res.partner"].create(
            [
                {"name": "BI Test Ann", "email": "ann@x.com", "country_id": cls.germany.id, "type": "invoice"},
                {"name": "BI Test Bob", "type": "contact"},
                {"name": "BI Test Cleo", "email": "cleo@x.com", "type": "contact", "active": True},
                {"name": "BI Test Dan", "country_id": cls.germany.id, "type": "contact"},
                {"name": "BI Test Eve", "type": "contact"},
                {"name": "Somebody else"},
            ]
        )
        cls.feed = cls.make_feed(cls, page_size=100)
        cls.entity = cls.add_partners(cls, cls.feed)

    def names(self, **args):
        status, body = self.ask(self.feed, "partners", **args)
        self.assertEqual(status, 200, body)
        return [row["name"] for row in body["value"]]

    # -- documents ------------------------------------------------------------------------------

    def test_service_document_and_metadata(self):
        status, body = self.ask(self.feed)
        self.assertEqual(status, 200)
        self.assertEqual(body["value"], [{"name": "partners", "kind": "EntitySet", "url": "partners"}])
        status, xml = self.ask(self.feed, "$metadata")
        self.assertEqual(status, 200)
        for expected in (
            '<EntityType Name="partners">',
            '<Property Name="id" Type="Edm.Int64" Nullable="false"/>',
            '<Property Name="name" Type="Edm.String"/>',
            '<Property Name="country_id" Type="Edm.Int64"/>',
            '<Property Name="country_id_name" Type="Edm.String"/>',
            '<Property Name="type_label" Type="Edm.String"/>',
            '<Property Name="create_date" Type="Edm.DateTimeOffset"/>',
            '<Property Name="active" Type="Edm.Boolean"/>',
            '<EntitySet Name="partners" EntityType="Odoo.partners"/>',
        ):
            self.assertIn(expected, xml)
        self.assertNotIn("message_ids", xml)
        self.assertNotIn("child_ids", xml, "one2many columns are not exposed")

    def test_rows_carry_ids_names_labels_and_formats(self):
        status, body = self.ask(self.feed, "partners", **{"$filter": "name eq 'BI Test Ann'"})
        (row,) = body["value"]
        self.assertEqual(row["id"], self.partners[0].id)
        self.assertEqual((row["country_id"], row["country_id_name"]), (self.germany.id, self.germany.display_name))
        self.assertEqual((row["type"], row["type_label"]), ("invoice", "Invoice"))
        self.assertEqual(row["email"], "ann@x.com")
        self.assertTrue(row["create_date"].endswith("Z"))
        self.assertIs(row["active"], True)
        self.assertEqual(body["@odata.context"], f"{BASE}/$metadata#partners")
        empty = self.ask(self.feed, "partners", **{"$filter": "name eq 'BI Test Bob'"})[1]["value"][0]
        self.assertIsNone(empty["email"])
        self.assertIsNone(empty["country_id"])
        self.assertIsNone(empty["country_id_name"])

    def test_chosen_columns_only(self):
        fields_ = self.env["ir.model.fields"].search([("model", "=", "res.partner"), ("name", "in", ["name", "email"])])
        self.entity.field_ids = fields_
        row = self.ask(self.feed, "partners", **{"$top": "1"})[1]["value"][0]
        self.assertEqual(sorted(row), ["email", "id", "name"])
        self.entity.field_ids = False

    # -- query options ---------------------------------------------------------------------------

    def test_filters(self):
        self.assertEqual(self.names(**{"$filter": "contains(name,'an')"}), ["BI Test Dan"])
        self.assertEqual(self.names(**{"$filter": "startswith(name,'BI Test C')"}), ["BI Test Cleo"])
        self.assertEqual(self.names(**{"$filter": "email ne null"}), ["BI Test Ann", "BI Test Cleo"])
        self.assertEqual(
            self.names(**{"$filter": "email eq null and country_id eq null"}), ["BI Test Bob", "BI Test Eve"]
        )
        self.assertEqual(self.names(**{"$filter": f"country_id eq {self.germany.id}"}), ["BI Test Ann", "BI Test Dan"])
        self.assertEqual(
            self.names(**{"$filter": f"country_id_name eq '{self.germany.name}'"}), ["BI Test Ann", "BI Test Dan"]
        )
        self.assertEqual(self.names(**{"$filter": "type_label eq 'Invoice'"}), ["BI Test Ann"])
        self.assertEqual(
            self.names(**{"$filter": "type eq 'invoice' or name eq 'BI Test Eve'"}), ["BI Test Ann", "BI Test Eve"]
        )
        self.assertEqual(self.names(**{"$filter": f"id eq {self.partners[1].id}"}), ["BI Test Bob"])
        self.assertEqual(
            self.names(**{"$filter": "create_date ge 2000-01-01T00:00:00Z and active eq true"}), self.names()
        )
        self.assertEqual(self.names(**{"$filter": "create_date lt 2000-01-01T00:00:00+01:00"}), [])

    def test_the_tables_own_filter_cannot_be_escaped(self):
        self.assertNotIn("Somebody else", self.names(**{"$filter": "name eq 'Somebody else' or name ne 'x'"}))

    def test_select_order_top_skip_count(self):
        status, body = self.ask(
            self.feed,
            "partners",
            **{"$select": "name", "$orderby": "name desc", "$top": "2", "$skip": "1", "$count": "true"},
        )
        self.assertEqual(
            body["value"],
            [{"id": self.partners[3].id, "name": "BI Test Dan"}, {"id": self.partners[2].id, "name": "BI Test Cleo"}],
        )
        self.assertEqual(body["@odata.count"], 5)
        self.assertEqual(self.names(**{"$orderby": "country_id_name, name"})[:2], ["BI Test Ann", "BI Test Dan"])
        self.assertEqual(self.ask(self.feed, "partners/$count", **{"$filter": "email ne null"}), (200, "2"))
        self.assertEqual(self.ask(self.feed, "partners", **{"$top": "0"})[1]["value"], [])

    def test_paging_follows_the_next_link(self):
        self.feed.page_size = 2
        status, body = self.ask(self.feed, "partners")
        self.assertEqual(len(body["value"]), 2)
        seen = [row["name"] for row in body["value"]]
        while "@odata.nextLink" in body:
            link = urlparse(body["@odata.nextLink"])
            self.assertTrue(link.path.endswith("/partners"))
            args = {key: values[0] for key, values in parse_qs(link.query).items()}
            status, body = self.ask(self.feed, "partners", **args)
            seen += [row["name"] for row in body["value"]]
        self.assertEqual(seen, ["BI Test Ann", "BI Test Bob", "BI Test Cleo", "BI Test Dan", "BI Test Eve"])
        # a client $top larger than a page is honoured over several pages, a smaller one ends at once
        body = self.ask(self.feed, "partners", **{"$top": "3"})[1]
        self.assertEqual(parse_qs(urlparse(body["@odata.nextLink"]).query)["$top"], ["1"])
        self.assertNotIn("@odata.nextLink", self.ask(self.feed, "partners", **{"$top": "2"})[1])
        self.feed.page_size = 100

    def test_mistakes_get_a_clear_answer(self):
        cases = [
            ("nope", {}, 404),
            ("partners/other", {}, 404),
            ("partners", {"$filter": "missing eq 1"}, 400),
            ("partners", {"$filter": "name eq"}, 400),
            ("partners", {"$select": "name,missing"}, 400),
            ("partners", {"$orderby": "name sideways"}, 400),
            ("partners", {"$top": "-1"}, 400),
            ("partners", {"$skip": "x"}, 400),
        ]
        for path, args, expected in cases:
            status, body = self.ask(self.feed, path, **args)
            self.assertEqual(status, expected, (path, args))
            self.assertIn("error", body)

    # -- who may see what --------------------------------------------------------------------------

    def test_the_feed_reads_as_its_user(self):
        plain = self.env["res.users"].create(
            {"name": "Plain", "login": "plain@x.com", "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]}
        )
        feed = self.make_feed(user_id=plain.id)
        self.env["powerbi.entity"].create(
            {"feed_id": feed.id, "model_id": self.env["ir.model"]._get("ir.cron").id, "name": "jobs"}
        )
        self.add_partners(feed)
        self.assertEqual(
            [item["name"] for item in self.ask(feed)[1]["value"]],
            ["partners"],
            "a table the user cannot read is not offered",
        )
        self.assertEqual(self.ask(feed, "jobs")[0], 404)

    def test_columns_the_user_may_not_read_are_left_out(self):
        plain = self.env["res.users"].create(
            {"name": "Plain2", "login": "plain2@x.com", "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]}
        )
        feed = self.make_feed(user_id=plain.id)
        self.env["powerbi.entity"].create(
            {
                "feed_id": feed.id,
                "model_id": self.env["ir.model"]._get("res.users").id,
                "name": "users",
                "domain": "[('login', '=', 'plain2@x.com')]",
            }
        )
        status, body = self.ask(feed, "users")
        self.assertEqual(status, 200)
        self.assertNotIn("password", body["value"][0])

    # -- keys -------------------------------------------------------------------------------------------

    def test_keys_are_kept_as_fingerprints_and_checked(self):
        feed = self.make_feed()
        self.assertNotIn(self.token, feed.sudo().token_hash)
        self.assertEqual(feed.sudo().token_hash, hash_token(self.token))
        auth = self.env["powerbi.feed"]._authenticate
        self.assertEqual(auth(feed.id, basic(self.token)), feed)
        self.assertEqual(auth(feed.id, basic("", user=self.token)), feed)
        self.assertEqual(auth(feed.id, f"Bearer {self.token}"), feed)
        for wrong in (basic("wrong"), "Bearer nope", "Basic !!!", "", "Digest x", None):
            self.assertFalse(auth(feed.id, wrong), wrong)
        self.assertFalse(auth(feed.id + 9999, basic(self.token)))

    def test_a_new_key_replaces_the_old_one_and_revoking_stops_access(self):
        feed = self.make_feed()
        old = self.token
        action = feed.action_new_token()
        self.assertEqual(action["res_model"], "powerbi.token.wizard")
        self.assertEqual(action["context"]["default_url"], feed.url)
        self.assertIn(f"/powerbi/{self.env.cr.dbname}/odata/{feed.id}", feed.url)
        self.assertFalse(self.env["powerbi.feed"]._authenticate(feed.id, basic(old)))
        feed.action_revoke_token()
        self.assertFalse(self.env["powerbi.feed"]._authenticate(feed.id, basic(action["context"]["default_token"])))
        self.assertFalse(feed.token_hint)

    def test_expired_archived_or_unfit_feeds_are_refused(self):
        feed = self.make_feed(token_expiry=date.today() - timedelta(days=1))
        auth = self.env["powerbi.feed"]._authenticate
        self.assertFalse(auth(feed.id, basic(self.token)))
        feed.token_expiry = date.today()
        self.assertTrue(auth(feed.id, basic(self.token)))
        feed.user_id.active = False
        self.assertFalse(auth(feed.id, basic(self.token)))
        feed.user_id.active = True
        feed.active = False
        self.assertFalse(auth(feed.id, basic(self.token)))

    def test_header_parsing(self):
        self.assertEqual(token_from_header("Bearer abc"), "abc")
        self.assertEqual(token_from_header("bearer  abc "), "abc")
        self.assertEqual(token_from_header(basic("abc")), "abc")
        self.assertEqual(token_from_header(basic("", user="abc")), "abc")
        self.assertEqual(token_from_header("Basic ???"), "")

    def test_last_use_is_noted_at_most_every_few_minutes(self):
        feed = self.make_feed()
        feed._touch()
        first = feed.last_used
        self.assertTrue(first)
        feed._touch()
        self.assertEqual(feed.last_used, first)
        feed.last_used = fields.Datetime.now() - timedelta(hours=1)
        feed._touch()
        self.assertGreater(feed.last_used, first - timedelta(hours=1))

    # -- setting up ---------------------------------------------------------------------------------------

    def test_table_names_and_filters_are_checked(self):
        with self.assertRaises(ValidationError):
            self.entity.name = "1 bad name"
        with self.assertRaises(ValidationError):
            self.entity.domain = "[('name', '=')"
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.cr.savepoint():
            self.add_partners(self.feed)  # the same name twice in a feed

    def test_ready_made_tables(self):
        """A set adds the tables whose app is installed (the CI database has sales, accounting and stock, not always purchase)."""
        feed = self.make_feed()
        sets = (
            (
                feed.action_add_sales,
                (
                    ("res.partner", "customers"),
                    ("sale.order", "sales_orders"),
                    ("sale.order.line", "sales_order_lines"),
                ),
            ),
            (
                feed.action_add_accounting,
                (("account.move", "invoices"), ("account.move.line", "invoice_lines"), ("account.payment", "payments")),
            ),
            (
                feed.action_add_inventory,
                (("stock.quant", "stock_levels"), ("stock.move", "stock_moves"), ("stock.warehouse", "warehouses")),
            ),
            (
                feed.action_add_purchases,
                (("purchase.order", "purchase_orders"), ("purchase.order.line", "purchase_order_lines")),
            ),
        )
        for add, tables in sets:
            present = [name for model, name in tables if model in self.env]
            if not present:
                with self.assertRaises(UserError):
                    add()
                continue
            add()
            for name in present:
                self.assertEqual(self.ask(feed, name, **{"$top": "1"})[0], 200, name)
            with self.assertRaises(UserError):
                add()  # already there
        if "sale.order" in self.env:
            orders = feed.entity_ids.filtered(lambda e: e.name == "sales_orders")
            self.assertIn("amount_total", orders.field_ids.mapped("name"))
        if "account.move" in self.env:
            invoices = feed.entity_ids.filtered(lambda e: e.name == "invoices")
            self.assertIn("move_type", invoices.domain)


@tagged("post_install", "-at_install")
class TestPowerBIRoute(FeedCase, HttpCase):
    def test_power_bi_can_read_the_feed_over_http(self):
        feed = self.make_feed()
        self.add_partners(feed)
        self.env["res.partner"].create({"name": "BI Test Zoe", "email": "zoe@x.com"})
        root = f"/powerbi/{self.env.cr.dbname}/odata/{feed.id}"
        auth = {"Authorization": basic(self.token)}

        refused = self.url_open(f"{root}/partners")
        self.assertEqual(refused.status_code, 401)
        self.assertIn("Basic", refused.headers["WWW-Authenticate"])
        self.assertEqual(self.url_open(f"{root}/partners", headers={"Authorization": basic("wrong")}).status_code, 401)
        self.assertEqual(self.url_open(f"/powerbi/nodb/odata/{feed.id}", headers=auth).status_code, 404)

        service = self.url_open(root, headers=auth)
        self.assertEqual(service.status_code, 200)
        self.assertEqual(service.headers["OData-Version"], "4.0")
        self.assertEqual(service.json()["value"][0]["name"], "partners")

        data = self.url_open(
            f"{root}/partners?$filter=email%20eq%20'zoe@x.com'&$select=name,email&$count=true", headers=auth
        )
        self.assertEqual(data.status_code, 200)
        self.assertIn("application/json", data.headers["Content-Type"])
        body = data.json()
        self.assertEqual([r["name"] for r in body["value"]], ["BI Test Zoe"])
        self.assertTrue(body["@odata.context"].startswith("http"))
        self.assertEqual(body["@odata.count"], 1)

        metadata = self.url_open(f"{root}/$metadata", headers=auth)
        self.assertIn("application/xml", metadata.headers["Content-Type"])
        self.assertIn("<EntityType", metadata.text)
        self.assertEqual(self.url_open(f"{root}/partners/$count", headers=auth).text, "1")
        self.assertEqual(self.url_open(f"{root}/partners?$filter=nope", headers=auth).status_code, 400)
        self.assertEqual(self.url_open(f"{root}/unknown", headers=auth).status_code, 404)
        self.assertTrue(feed.last_used)


PBI = "odoo.addons.powerbi_connector.models.powerbi_client.PowerBIClient"
HTTP = "odoo.addons.powerbi_connector.models.powerbi_client.requests"


class FakePowerBI:
    """Remembers what was sent to the dataset routes."""

    def __init__(self):
        self.calls = []
        self.fail = {}

    def __call__(self, method, path, json=None):
        self.calls.append((method, path, json))
        for key, error in self.fail.items():
            if path.startswith(key):
                raise error
        if method == "POST" and path.startswith("datasets?"):
            return {"id": "ds-1"}
        return {}

    def rows(self, table):
        return [c[2]["rows"] for c in self.calls if c[0] == "POST" and c[1].endswith(f"/tables/{table}/rows")]


@tagged("post_install", "-at_install")
class TestPowerBIPush(FeedCase, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["res.partner"].create([{"name": f"BI Test {i}", "email": f"p{i}@x.com"} for i in range(3)])
        cls.feed = cls.make_feed(
            cls,
            push_enabled=True,
            pbi_tenant_id="tenant",
            pbi_client_id="app",
            pbi_client_secret="secret",
            pbi_workspace_id="ws-1",
        )
        cls.add_partners(
            cls,
            cls.feed,
            field_ids=[
                (
                    6,
                    0,
                    cls.env["ir.model.fields"]
                    .search(
                        [("model", "=", "res.partner"), ("name", "in", ["name", "email", "create_date", "country_id"])]
                    )
                    .ids,
                )
            ],
        )

    def setUp(self):
        super().setUp()
        self.api = FakePowerBI()

    def push(self):
        with patch(f"{PBI}.request", side_effect=self.api):
            return self.feed._push()

    def test_first_push_creates_the_dataset_and_sends_the_rows(self):
        self.assertEqual(self.push(), 3)
        create = [c for c in self.api.calls if c[1].startswith("datasets?")][0][2]
        self.assertEqual((create["name"], create["defaultMode"]), ("BI", "Push"))
        columns = {c["name"]: c["dataType"] for c in create["tables"][0]["columns"]}
        self.assertEqual(columns["id"], "Int64")
        self.assertEqual(columns["create_date"], "DateTime")
        self.assertEqual(columns["country_id_name"], "String")
        self.assertEqual(self.feed.pbi_dataset_id, "ds-1")
        self.assertTrue(self.feed.last_push)
        (rows,) = self.api.rows("partners")
        self.assertEqual(sorted(r["email"] for r in rows), ["p0@x.com", "p1@x.com", "p2@x.com"])
        self.assertIn(("DELETE", "datasets/ds-1/tables/partners/rows", None), self.api.calls)

    def test_later_pushes_replace_the_rows_and_keep_the_dataset(self):
        self.push()
        self.api.calls.clear()
        self.push()
        self.assertFalse([c for c in self.api.calls if c[1].startswith("datasets?")])
        self.assertEqual(self.feed.pbi_dataset_id, "ds-1")
        clear_index = self.api.calls.index(("DELETE", "datasets/ds-1/tables/partners/rows", None))
        self.assertEqual(self.api.calls[clear_index + 1][0], "POST", "cleared, then refilled")

    def test_big_tables_go_in_several_calls(self):
        with patch("odoo.addons.powerbi_connector.models.powerbi_feed_push.ROWS_PER_CALL", 2):
            self.push()
        self.assertEqual([len(rows) for rows in self.api.rows("partners")], [2, 1])

    def test_dates_are_sent_as_date_times(self):
        self.env["res.currency.rate"].create(
            {"name": "2026-10-29", "rate": 1.234, "currency_id": self.env.ref("base.EUR").id}
        )
        self.env["powerbi.entity"].create(
            {
                "feed_id": self.feed.id,
                "model_id": self.env["ir.model"]._get("res.currency.rate").id,
                "name": "rates",
                "domain": "[('rate', '=', 1.234)]",
            }
        )
        self.push()
        create = [c for c in self.api.calls if c[1].startswith("datasets?")][0][2]
        rates = next(t for t in create["tables"] if t["name"] == "rates")
        self.assertIn({"name": "name", "dataType": "DateTime"}, rates["columns"])
        (rows,) = self.api.rows("rates")
        self.assertEqual(rows[0]["name"], "2026-10-29T00:00:00Z")
        self.assertEqual(rows[0]["rate"], 1.234)

    def test_changed_columns_update_the_table_without_a_new_dataset(self):
        self.push()
        self.api.calls.clear()
        self.feed.entity_ids.field_ids = self.env["ir.model.fields"].search(
            [("model", "=", "res.partner"), ("name", "in", ["name", "email"])]
        )
        self.push()
        puts = [c for c in self.api.calls if c[0] == "PUT"]
        self.assertEqual([c[1] for c in puts], ["datasets/ds-1/tables/partners"])
        self.assertNotIn("create_date", [c["name"] for c in puts[0][2]["columns"]])
        self.assertEqual(self.feed.pbi_dataset_id, "ds-1")

    def test_a_new_table_needs_a_new_dataset(self):
        self.push()
        self.api.calls.clear()
        self.env["powerbi.entity"].create(
            {"feed_id": self.feed.id, "model_id": self.env["ir.model"]._get("res.country").id, "name": "countries"}
        )
        self.push()
        methods = [(c[0], c[1]) for c in self.api.calls[:2]]
        self.assertEqual(methods[0], ("DELETE", "datasets/ds-1"))
        self.assertTrue(methods[1][1].startswith("datasets?"))

    def test_problems_are_explained(self):
        self.api.fail["datasets?"] = PowerBIError("Power BI refused access (x).")
        with patch(f"{PBI}.request", side_effect=self.api):
            result = self.feed.action_push_now()
        self.assertEqual(result["params"]["type"], "danger")
        self.assertIn("refused access", self.feed.push_message)
        self.api.fail.clear()
        with patch(f"{PBI}.request", side_effect=self.api):
            result = self.feed.action_push_now()
        self.assertEqual(result["params"]["type"], "success")
        self.assertFalse(self.feed.push_message)

    def test_the_schedule_sends_only_when_due(self):
        with patch(f"{PBI}.request", side_effect=self.api):
            self.feed._cron_push()
            self.assertTrue(self.feed.last_push)
            self.api.calls.clear()
            self.feed._cron_push()
            self.assertEqual(self.api.calls, [], "sent a moment ago, nothing is due")
            self.feed.last_push = fields.Datetime.now() - timedelta(hours=25)
            self.feed._cron_push()
            self.assertTrue(self.api.calls)
            self.feed.write({"last_push": fields.Datetime.now() - timedelta(days=9), "push_interval": "0"})
            self.api.calls.clear()
            self.feed._cron_push()
            self.assertEqual(self.api.calls, [], "manual only")
            self.feed.push_interval = "1"
            self.api.fail["datasets/ds-1/tables"] = PowerBIError("Power BI says there are too many requests.")
            self.feed._cron_push()
        self.assertIn("too many", self.feed.push_message)

    def test_credentials_are_needed(self):
        self.feed.pbi_client_secret = False
        with self.assertRaises(PowerBIError):
            PowerBIClient(self.feed)

    def test_sign_in_and_http_errors(self):
        client = PowerBIClient(self.feed)
        ok = MagicMock(status_code=200)
        ok.json.return_value = {"access_token": "T"}
        with patch(f"{HTTP}.post", return_value=ok) as post:
            self.assertEqual(client.token(), "T")
            client.token()
        self.assertEqual(post.call_count, 1, "the token is reused")
        self.assertEqual(post.call_args.kwargs["data"]["scope"], "https://analysis.windows.net/powerbi/api/.default")
        bad = MagicMock(status_code=401, text="x")
        bad.json.return_value = {"error_description": "AADSTS7000215: Invalid client secret provided.\r\nTrace ID: 1"}
        with patch(f"{HTTP}.post", return_value=bad), self.assertRaises(PowerBIError) as caught:
            PowerBIClient(self.feed).token()
        self.assertIn("Invalid client secret", str(caught.exception))

        def answer(status, body=None, headers=None):
            response = MagicMock(status_code=status, content=b"x", text="x", headers=headers or {})
            response.json.return_value = body or {}
            return response

        client._token = "T"
        with patch(f"{HTTP}.request", return_value=answer(200, {"ok": 1})) as call:
            self.assertEqual(client.request("GET", "datasets"), {"ok": 1})
        self.assertIn("/groups/ws-1/datasets", call.call_args.args[1])
        for status, text in ((403, "Service principals"), (404, "workspace ID"), (400, "boom")):
            with (
                patch(f"{HTTP}.request", return_value=answer(status, {"error": {"message": "boom"}})),
                self.assertRaises(PowerBIError) as caught,
            ):
                client.request("GET", "datasets")
            self.assertIn(text, str(caught.exception))
        with (
            patch(f"{HTTP}.request", side_effect=[answer(401), answer(200, {"ok": 2})]),
            patch.object(PowerBIClient, "token", return_value="T2"),
        ):
            self.assertEqual(client.request("GET", "datasets"), {"ok": 2})
        with (
            patch("odoo.addons.powerbi_connector.models.powerbi_client.time.sleep"),
            patch(f"{HTTP}.request", side_effect=[answer(429, headers={"Retry-After": "1"}), answer(200, {"ok": 3})]),
        ):
            self.assertEqual(client.request("GET", "datasets"), {"ok": 3})
