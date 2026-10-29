import json
from unittest.mock import MagicMock, patch
from urllib.parse import quote

from odoo import fields
from odoo.addons.marketing_connector_base.models.marketing_error import MarketingAuthError, MarketingError
from odoo.tests import TransactionCase, tagged

from ..models.brevo_client import BrevoClient, BrevoNotFound, contact_path
from ..models.marketing_account import iso_time, normalize_campaign, normalize_contact, parse_time

REQUEST = "odoo.addons.marketing_brevo.models.brevo_client.BrevoClient.request"
HTTP = "odoo.addons.marketing_brevo.models.brevo_client.requests.request"
ACCOUNT = "odoo.addons.marketing_brevo.models.marketing_account"


def contact(email, blacklisted=False, first="", last="", lists=(1,), modified="2026-10-29T10:00:00.000Z", cid=1):
    return {
        "id": cid,
        "email": email,
        "emailBlacklisted": blacklisted,
        "modifiedAt": modified,
        "attributes": {"FIRSTNAME": first, "LASTNAME": last},
        "listIds": list(lists),
    }


class FakeBrevo:
    def __init__(self):
        self.lists = [
            {"id": 1, "name": "Newsletter", "uniqueSubscribers": 3},
            {"id": 2, "name": "Customers", "uniqueSubscribers": 0},
        ]
        self.contacts = {}  # email -> contact dict
        self.campaigns = []
        self.remove_failures = set()
        self.fail = {}
        self.calls = []

    def __call__(self, method, path, params=None, json=None):
        self.calls.append((method, path, params, json))
        if path in self.fail:
            raise self.fail[path]
        params = params or {}
        parts = path.split("/")
        if path == "account":
            return {"companyName": "Acme Ltd"}
        if path == "contacts/lists":
            return {"lists": self._page(self.lists, params)}
        if path == "emailCampaigns":
            return {"campaigns": self._page(self.campaigns, params)}
        if parts[:2] == ["contacts", "lists"] and parts[3] == "contacts" and len(parts) == 4:
            rows = [c for c in self.contacts.values() if int(parts[2]) in c["listIds"]]
            if params.get("modifiedSince"):
                rows = [c for c in rows if c["modifiedAt"] >= params["modifiedSince"]]
            return {"contacts": self._page(rows, params)}
        if path.endswith("/contacts/remove"):
            emails = json["emails"]
            return {
                "contacts": {
                    "success": [e for e in emails if e not in self.remove_failures],
                    "failure": [e for e in emails if e in self.remove_failures],
                }
            }
        if path == "contacts/import":
            return {"processId": 7}
        if path == "webhooks":
            return {"id": 5}
        if parts[0] == "contacts" and len(parts) == 2:
            email = parts[1].replace("%40", "@")
            if method == "PUT":
                return {}
            if email in self.contacts:
                return self.contacts[email]
            raise BrevoNotFound("Brevo could not find it")
        raise AssertionError(f"unexpected call {method} {path}")

    @staticmethod
    def _page(rows, params):
        offset = params.get("offset", 0)
        return rows[offset : offset + params.get("limit", len(rows))]

    def sent(self, path):
        return [c[3] for c in self.calls if c[1] == path]


@tagged("post_install", "-at_install")
class TestBrevo(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env["marketing.account"].create(
            {"name": "Brevo", "provider": "brevo", "api_key": "xkeysib-1"}
        )

    def setUp(self):
        super().setUp()
        self.shop = FakeBrevo()

    def connect(self):
        with patch(REQUEST, side_effect=self.shop):
            self.account.action_connect()
        return self.account.link_ids.filtered(lambda link: link.remote_id == "1")

    def synced_link(self):
        link = self.connect()
        link.sync = True
        return link

    def sync(self):
        with patch(REQUEST, side_effect=self.shop):
            return self.account._sync()

    def find(self, email):
        return self.env["mailing.contact"].search([("email_normalized", "=", email)])

    def subscription(self, email, link):
        return self.find(email).subscription_ids.filtered(lambda s: s.list_id == link.list_id)

    # -- helpers ---------------------------------------------------------------------------

    def test_helpers(self):
        data = normalize_contact(contact("a@x.com", first="Ann", last="Lee", cid=9))
        self.assertEqual(
            (data["first_name"], data["last_name"], data["subscribed"], data["remote_id"]), ("Ann", "Lee", True, "9")
        )
        self.assertFalse(normalize_contact(contact("a@x.com", blacklisted=True))["subscribed"])
        self.assertEqual(parse_time("2026-10-29T12:00:00.000+02:00").hour, 10)
        self.assertEqual(parse_time("2026-10-29T10:00:00.000Z").hour, 10)
        self.assertFalse(parse_time(None))
        self.assertEqual(iso_time(parse_time("2026-10-29T10:00:00Z")), "2026-10-29T10:00:00.000Z")
        self.assertEqual(contact_path(" A+b@X.com "), "contacts/" + quote("a+b@x.com", safe=""))
        campaign = normalize_campaign(
            {
                "id": 3,
                "name": "News",
                "subject": "Hi",
                "sentDate": "2026-10-28T08:00:00.000Z",
                "recipients": {"lists": [1, 99]},
                "statistics": {
                    "globalStats": {
                        "sent": 100,
                        "uniqueViews": 40,
                        "uniqueClicks": 12,
                        "unsubscriptions": 2,
                        "hardBounces": 1,
                        "softBounces": 2,
                    }
                },
            },
            {"1": "Newsletter"},
        )
        self.assertEqual(
            (
                campaign["list_name"],
                campaign["emails_sent"],
                campaign["opens"],
                campaign["clicks"],
                campaign["bounces"],
            ),
            ("Newsletter", 100, 40, 12, 3),
        )

    def test_http_errors_are_explained(self):
        def answer(status, body):
            response = MagicMock(status_code=status, content=b"x", text="x")
            response.json.return_value = body
            return response

        client = BrevoClient(self.account)
        with (
            patch(HTTP, return_value=answer(401, {"message": "Key not found"})),
            self.assertRaises(MarketingAuthError) as caught,
        ):
            client.request("GET", "account")
        self.assertIn("Authorised IPs", str(caught.exception))
        with (
            patch(HTTP, return_value=answer(404, {"message": "Contact does not exist"})),
            self.assertRaises(BrevoNotFound),
        ):
            client.request("GET", "contacts/x")
        with (
            patch(HTTP, return_value=answer(400, {"message": "Invalid attribute"})),
            self.assertRaises(MarketingError) as caught,
        ):
            client.request("POST", "contacts/import")
        self.assertIn("Invalid attribute", str(caught.exception))
        with patch(HTTP, return_value=answer(200, {"ok": 1})):
            self.assertEqual(client.request("GET", "account"), {"ok": 1})
        self.account.api_key = ""
        with self.assertRaises(MarketingError):
            BrevoClient(self.account)

    # -- connect -------------------------------------------------------------------------------

    def test_connect_finds_the_lists(self):
        self.connect()
        self.assertEqual(self.account.state, "connected")
        self.assertEqual(sorted(self.account.link_ids.mapped("name")), ["Customers", "Newsletter"])
        self.assertTrue(self.account.connect_help)
        self.assertTrue(self.account.supports_webhooks)

    def test_a_rejected_key_disconnects(self):
        self.shop.fail["account"] = MarketingAuthError("Brevo rejected the API key.")
        with patch(REQUEST, side_effect=self.shop):
            result = self.account.action_connect()
        self.assertEqual(result["params"]["type"], "danger")
        self.assertEqual(self.account.state, "error")

    def test_lists_are_read_page_by_page(self):
        self.shop.lists = [{"id": i, "name": f"L{i}", "uniqueSubscribers": 0} for i in range(1, 121)]
        self.connect()
        self.assertEqual(len(self.account.link_ids), 120)

    # -- Brevo to Odoo -------------------------------------------------------------------------

    def test_contacts_come_in_and_blacklisted_ones_opt_out(self):
        link = self.synced_link()
        known = self.env["mailing.contact"].create(
            {"name": "Bob", "email": "bob@x.com", "list_ids": [(0, 0, {"name": "Newsletter"})]}
        )
        self.shop.contacts = {
            "ann@x.com": contact("ann@x.com", first="Ann", last="Lee", cid=1),
            "bob@x.com": contact("bob@x.com", blacklisted=True, cid=2),
            "zed@x.com": contact("zed@x.com", blacklisted=True, cid=3),
        }
        self.assertTrue(self.sync())
        self.assertEqual(self.find("ann@x.com").name, "Ann Lee")
        self.assertFalse(self.subscription("ann@x.com", link).opt_out)
        self.assertTrue(self.subscription("bob@x.com", link).opt_out)
        self.assertEqual(self.subscription("bob@x.com", link).contact_id, known)
        self.assertFalse(self.find("zed@x.com"), "someone who already left is not worth a contact")

    def test_only_changes_since_the_last_sync_are_read(self):
        self.synced_link()
        self.sync()
        self.shop.calls.clear()
        self.sync()
        call = [c for c in self.shop.calls if c[1] == "contacts/lists/1/contacts"][0]
        self.assertTrue(call[2]["modifiedSince"].endswith("Z"))

    def test_contacts_are_read_in_pages(self):
        self.synced_link()
        self.shop.contacts = {f"p{i}@x.com": contact(f"p{i}@x.com", cid=i + 1) for i in range(5)}
        with patch(f"{ACCOUNT}.CONTACT_PAGE", 2):
            self.sync()
        self.assertEqual(self.env["mailing.contact"].search_count([("email_normalized", "like", "p%@x.com")]), 5)

    # -- Odoo to Brevo -------------------------------------------------------------------------

    def test_subscribed_contacts_are_imported_into_the_list_once(self):
        link = self.synced_link()
        link._ensure_list()
        self.env["mailing.contact"].create(
            {"name": "Carl Dee", "email": "carl@x.com", "list_ids": [(6, 0, link.list_id.ids)]}
        )
        self.env["mailing.contact"].create(
            {"name": "Solo", "email": "solo@x.com", "list_ids": [(6, 0, link.list_id.ids)]}
        )
        self.sync()
        (body,) = self.shop.sent("contacts/import")
        self.assertEqual(body["listIds"], [1])
        self.assertTrue(body["updateExistingContacts"])
        self.assertFalse(body["emptyContactsAttributes"])
        self.assertIn({"email": "carl@x.com", "attributes": {"FIRSTNAME": "Carl", "LASTNAME": "Dee"}}, body["jsonBody"])
        self.assertIn({"email": "solo@x.com", "attributes": {"FIRSTNAME": "Solo"}}, body["jsonBody"])
        self.shop.calls.clear()
        self.account.link_ids.write({"last_push": False})
        self.sync()
        self.assertEqual(self.shop.sent("contacts/import"), [])

    def test_opting_out_takes_the_contact_off_the_list_and_blacklisting_blacklists_them(self):
        link = self.synced_link()
        link._ensure_list()
        carl = self.env["mailing.contact"].create(
            {"name": "Carl", "email": "carl@x.com", "list_ids": [(6, 0, link.list_id.ids)]}
        )
        eve = self.env["mailing.contact"].create(
            {"name": "Eve", "email": "eve@x.com", "list_ids": [(6, 0, link.list_id.ids)]}
        )
        dora = self.env["mailing.contact"].create(
            {
                "name": "Dora",
                "email": "dora@x.com",
                "subscription_ids": [(0, 0, {"list_id": link.list_id.id, "opt_out": True})],
            }
        )
        self.sync()
        self.assertEqual(self.shop.sent("contacts/lists/1/contacts/remove"), [], "Dora never was at Brevo")
        carl.subscription_ids.opt_out = True
        self.env["mail.blacklist"]._add("eve@x.com")
        self.env.invalidate_all()
        self.shop.calls.clear()
        self.sync()
        (removal,) = self.shop.sent("contacts/lists/1/contacts/remove")
        self.assertEqual(sorted(removal["emails"]), ["carl@x.com", "eve@x.com"])
        puts = [c for c in self.shop.calls if c[0] == "PUT"]
        self.assertEqual([(c[1], c[3]) for c in puts], [(contact_path("eve@x.com"), {"emailBlacklisted": True})])
        self.assertFalse(self.env["marketing.member"].search([("contact_id", "=", dora.id)]))
        self.assertTrue(eve)

    def test_addresses_brevo_could_not_remove_are_listed(self):
        link = self.synced_link()
        link._ensure_list()
        carl = self.env["mailing.contact"].create(
            {"name": "Carl", "email": "carl@x.com", "list_ids": [(6, 0, link.list_id.ids)]}
        )
        self.sync()
        carl.subscription_ids.opt_out = True
        self.shop.remove_failures = {"carl@x.com"}
        self.sync()
        self.assertIn("could not take", link.member_ids.sync_error)
        self.assertEqual(self.account.problem_count, 1)

    def test_contact_unknown_at_brevo_is_skipped_when_blacklisting(self):
        link = self.synced_link()
        link._ensure_list()
        self.env["mailing.contact"].create(
            {"name": "Eve", "email": "eve@x.com", "list_ids": [(6, 0, link.list_id.ids)]}
        )
        self.sync()
        self.env["mail.blacklist"]._add("eve@x.com")
        self.env.invalidate_all()
        self.shop.fail[contact_path("eve@x.com")] = BrevoNotFound("gone")
        self.assertTrue(self.sync())

    # -- webhooks -------------------------------------------------------------------------------

    def test_one_webhook_is_registered_for_the_account(self):
        self.synced_link()
        with patch(REQUEST, side_effect=self.shop):
            self.account._brevo_register_webhooks()
            self.account._brevo_register_webhooks()
        (body,) = self.shop.sent("webhooks")
        self.assertEqual(body["type"], "marketing")
        self.assertIn("unsubscribed", body["events"])
        self.assertIn(f"/marketing/brevo/webhook/{self.account.id}/", body["url"])
        self.assertTrue(self.account.webhooks_registered)

    def test_webhook_unsubscribe_is_applied_to_that_contact_only(self):
        link = self.synced_link()
        self.shop.contacts = {"ann@x.com": contact("ann@x.com"), "bob@x.com": contact("bob@x.com", cid=2)}
        self.sync()
        self.shop.contacts["ann@x.com"] = contact("ann@x.com", blacklisted=True, modified="2026-10-30T10:00:00.000Z")
        self.shop.calls.clear()
        body = json.dumps({"event": "unsubscribed", "email": "ann@x.com"}).encode()
        with patch(REQUEST, side_effect=self.shop):
            self.account._handle_webhook(body, {})
        self.assertTrue(self.subscription("ann@x.com", link).opt_out)
        self.assertFalse(self.subscription("bob@x.com", link).opt_out)
        self.assertEqual(self.account._brevo_parse_webhook(b"not json", {}), [])
        self.assertEqual(self.account._brevo_parse_webhook(b"{}", {}), [])

    def test_a_contact_not_on_the_list_is_not_treated_as_unsubscribed(self):
        link = self.synced_link()
        link._ensure_list()
        self.env["mailing.contact"].create(
            {"name": "Fay", "email": "fay@x.com", "list_ids": [(6, 0, link.list_id.ids)]}
        )
        self.sync()
        self.shop.contacts["fay@x.com"] = contact("fay@x.com", lists=(2,))
        with patch(REQUEST, side_effect=self.shop):
            self.account._handle_webhook(json.dumps({"email": "fay@x.com"}).encode(), {})
            self.account._handle_webhook(json.dumps({"email": "nobody@x.com"}).encode(), {})
        self.assertFalse(self.subscription("fay@x.com", link).opt_out)
        self.assertFalse(self.find("nobody@x.com"))

    # -- campaigns -------------------------------------------------------------------------------

    def test_campaign_results_are_updated_not_duplicated(self):
        self.synced_link()
        self.shop.campaigns = [
            {
                "id": 3,
                "name": "October news",
                "subject": "Hi",
                "sentDate": "2026-10-28T08:00:00.000Z",
                "recipients": {"lists": [1]},
                "statistics": {
                    "globalStats": {"sent": 200, "uniqueViews": 80, "uniqueClicks": 20, "unsubscriptions": 1}
                },
            }
        ]
        self.sync()
        campaign = self.account.campaign_ids
        self.assertEqual(
            (campaign.list_name, campaign.emails_sent, campaign.open_rate, campaign.click_rate),
            ("Newsletter", 200, 40.0, 10.0),
        )
        self.shop.campaigns[0]["statistics"]["globalStats"]["uniqueViews"] = 100
        self.sync()
        self.assertEqual(len(self.account.campaign_ids), 1)
        self.assertEqual(self.account.campaign_ids.opens, 100)
        params = [c[2] for c in self.shop.calls if c[1] == "emailCampaigns"][0]
        self.assertEqual((params["status"], params["statistics"]), ("sent", "globalStats"))
        self.assertTrue(fields.Datetime.now())
