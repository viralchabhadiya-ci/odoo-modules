from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.addons.marketing_connector_base.models.marketing_error import MarketingAuthError, MarketingError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.mailchimp_client import MailchimpClient, MailchimpNotFound, subscriber_hash
from ..models.marketing_account import normalize_member, normalize_report, parse_time

REQUEST = "odoo.addons.marketing_mailchimp.models.mailchimp_client.MailchimpClient.request"
HTTP = "odoo.addons.marketing_mailchimp.models.mailchimp_client.requests.request"


def member(email, status="subscribed", first="", last="", changed="2026-10-29T10:00:00+00:00"):
    return {
        "id": subscriber_hash(email),
        "email_address": email,
        "status": status,
        "merge_fields": {"FNAME": first, "LNAME": last},
        "last_changed": changed,
    }


class FakeMailchimp:
    """Answers the few Mailchimp routes the connector uses."""

    def __init__(self):
        self.lists = [
            {"id": "L1", "name": "Newsletter", "stats": {"member_count": 2}},
            {"id": "L2", "name": "Customers", "stats": {"member_count": 0}},
        ]
        self.members = {"L1": [], "L2": []}
        self.reports = []
        self.reject = {}  # email -> error text
        self.fail = {}  # path -> exception
        self.calls = []

    def __call__(self, method, path, params=None, json=None):
        self.calls.append((method, path, params, json))
        if path in self.fail:
            raise self.fail[path]
        params = params or {}
        parts = path.strip("/").split("/")
        if path == "":
            return {"account_name": "Acme Ltd"}
        if parts == ["lists"] and method == "GET":
            return {"lists": self._page(self.lists, params)}
        if parts[0] == "reports":
            return {"reports": self._page(self.reports, params)}
        if parts[0] == "lists" and len(parts) == 3 and parts[2] == "members" and method == "GET":
            rows = self.members[parts[1]]
            if params.get("since_last_changed"):
                rows = [m for m in rows if m["last_changed"] >= params["since_last_changed"]]
            return {"members": self._page(rows, params)}
        if parts[0] == "lists" and len(parts) == 4 and parts[2] == "members":
            for row in self.members[parts[1]]:
                if row["id"] == parts[3]:
                    return row
            raise MailchimpNotFound("Mailchimp could not find it")
        if parts[0] == "lists" and len(parts) == 2 and method == "POST":
            errors = []
            for item in json["members"]:
                if item["email_address"] in self.reject:
                    errors.append({"email_address": item["email_address"], "error": self.reject[item["email_address"]]})
            return {"errors": errors}
        if parts[0] == "lists" and parts[-1] == "webhooks":
            return {"id": f"hook-{parts[1]}"}
        raise AssertionError(f"unexpected call {method} {path}")

    @staticmethod
    def _page(rows, params):
        offset = params.get("offset", 0)
        return rows[offset : offset + params.get("count", len(rows))]

    def pushes(self, list_id="L1"):
        return [c[3]["members"] for c in self.calls if c[0] == "POST" and c[1] == f"lists/{list_id}"]


@tagged("post_install", "-at_install")
class TestMailchimp(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env["marketing.account"].create(
            {"name": "Mailchimp", "provider": "mailchimp", "api_key": "abc123-us21"}
        )

    def setUp(self):
        super().setUp()
        self.shop = FakeMailchimp()

    def connect(self):
        with patch(REQUEST, side_effect=self.shop):
            self.account.action_connect()
        return self.account.link_ids.filtered(lambda link: link.remote_id == "L1")

    def synced_link(self):
        link = self.connect()
        link.sync = True
        return link

    def sync(self):
        with patch(REQUEST, side_effect=self.shop):
            return self.account._sync()

    def contact(self, email):
        return self.env["mailing.contact"].search([("email_normalized", "=", email)])

    def subscription(self, email, link):
        return self.contact(email).subscription_ids.filtered(lambda s: s.list_id == link.list_id)

    # -- helpers -------------------------------------------------------------------------

    def test_member_and_report_helpers(self):
        data = normalize_member(member("a@x.com", first="Ann", last="Lee"))
        self.assertEqual(
            (data["email"], data["first_name"], data["last_name"], data["subscribed"]), ("a@x.com", "Ann", "Lee", True)
        )
        self.assertEqual(data["updated"], datetime(2026, 10, 29, 10, 0))
        self.assertFalse(normalize_member(member("a@x.com", status="pending"))["subscribed"])
        self.assertFalse(normalize_member(member("a@x.com", status="cleaned"))["subscribed"])
        self.assertEqual(parse_time("2026-10-29T12:00:00+02:00"), datetime(2026, 10, 29, 10, 0))
        self.assertFalse(parse_time(""))
        self.assertEqual(subscriber_hash(" A@X.com "), subscriber_hash("a@x.com"))
        report = normalize_report(
            {
                "id": "c1",
                "campaign_title": "October news",
                "subject_line": "Hello",
                "emails_sent": 100,
                "opens": {"unique_opens": 40},
                "clicks": {"unique_subscriber_clicks": 10},
                "unsubscribed": 2,
                "bounces": {"hard_bounces": 1, "soft_bounces": 3},
                "send_time": "2026-10-28T08:00:00+00:00",
            }
        )
        self.assertEqual((report["opens"], report["clicks"], report["unsubscribes"], report["bounces"]), (40, 10, 2, 4))

    # -- connection -----------------------------------------------------------------------

    def test_connect_finds_the_audiences(self):
        self.connect()
        self.assertEqual(self.account.state, "connected")
        self.assertEqual(sorted(self.account.link_ids.mapped("name")), ["Customers", "Newsletter"])
        self.assertEqual(self.account.link_ids.filtered(lambda link: link.remote_id == "L1").remote_count, 2)
        self.assertTrue(all(not link.sync for link in self.account.link_ids), "nothing syncs until it is ticked")
        self.assertTrue(self.account.connect_help)

    def test_connect_tells_what_is_wrong(self):
        self.shop.fail[""] = MarketingAuthError("Mailchimp rejected the API key.")
        with patch(REQUEST, side_effect=self.shop):
            result = self.account.action_connect()
        self.assertEqual(result["params"]["type"], "danger")
        self.assertEqual(self.account.state, "error")
        self.assertIn("rejected", self.account.connection_message)

    def test_a_vanished_audience_is_marked_and_stops_syncing(self):
        link = self.synced_link()
        self.shop.lists = [self.shop.lists[1]]
        self.sync()
        self.assertTrue(link.missing)
        self.assertFalse(link.sync)

    def test_key_needs_the_data_center(self):
        self.account.api_key = "nodash"
        with self.assertRaises(MarketingError):
            MailchimpClient(self.account)
        self.account.api_key = ""
        with self.assertRaises(MarketingError):
            MailchimpClient(self.account)

    def test_http_errors_are_explained(self):
        def answer(status, body):
            response = MagicMock(status_code=status, content=b"x", text="x")
            response.json.return_value = body
            return response

        client = MailchimpClient(self.account)
        with patch(HTTP, return_value=answer(401, {})), self.assertRaises(MarketingAuthError):
            client.request("GET", "")
        with patch(HTTP, return_value=answer(404, {"detail": "no such list"})), self.assertRaises(MailchimpNotFound):
            client.request("GET", "lists/x")
        with (
            patch(HTTP, return_value=answer(400, {"detail": "Bad merge field"})),
            self.assertRaises(MarketingError) as caught,
        ):
            client.request("POST", "lists/x")
        self.assertIn("Bad merge field", str(caught.exception))
        with patch(HTTP, return_value=answer(200, {"ok": 1})):
            self.assertEqual(client.request("GET", ""), {"ok": 1})

    # -- Mailchimp to Odoo ------------------------------------------------------------------

    def test_ticking_an_audience_creates_the_odoo_list_once(self):
        link = self.synced_link()
        self.env["mailing.list"].create({"name": "customers"})
        self.sync()
        self.assertEqual(link.list_id.name, "Newsletter")
        self.sync()
        self.assertEqual(self.env["mailing.list"].search_count([("name", "=", "Newsletter")]), 1)
        # an Odoo list with the same name (any case) is reused
        other = self.account.link_ids - link
        other.sync = True
        self.sync()
        self.assertEqual(other.list_id.name, "customers")

    def test_subscribers_become_contacts_and_unsubscribed_stay_out(self):
        link = self.synced_link()
        self.shop.members["L1"] = [
            member("ann@x.com", first="Ann", last="Lee"),
            member("bob@x.com", status="unsubscribed"),
            member("pat@x.com", status="pending"),
        ]
        self.assertTrue(self.sync())
        ann = self.contact("ann@x.com")
        self.assertEqual(ann.name, "Ann Lee")
        self.assertFalse(self.subscription("ann@x.com", link).opt_out)
        self.assertFalse(self.contact("bob@x.com"), "someone who already left is not worth a contact")
        self.assertFalse(self.contact("pat@x.com"), "a pending contact is not created, they have not confirmed")
        self.assertTrue(link.last_pull)

    def test_unsubscribe_at_mailchimp_opts_the_contact_out(self):
        link = self.synced_link()
        known = self.env["mailing.contact"].create(
            {
                "name": "Bob",
                "email": "Bob@X.com",
                "list_ids": [(6, 0, [self.env["mailing.list"].create({"name": "Newsletter"}).id])],
            }
        )
        self.shop.members["L1"] = [member("bob@x.com", status="unsubscribed")]
        self.sync()
        self.assertEqual(self.subscription("bob@x.com", link).contact_id, known)
        self.assertTrue(self.subscription("bob@x.com", link).opt_out)

    def test_cleaned_address_counts_as_opted_out(self):
        link = self.synced_link()
        self.shop.members["L1"] = [member("ann@x.com")]
        self.sync()
        self.shop.members["L1"] = [member("ann@x.com", status="cleaned", changed="2026-10-30T10:00:00+00:00")]
        self.sync()
        self.assertTrue(self.subscription("ann@x.com", link).opt_out)

    def test_nobody_is_created_when_the_option_is_off(self):
        self.account.create_contacts = False
        self.synced_link()
        self.shop.members["L1"] = [member("ann@x.com")]
        self.sync()
        self.assertFalse(self.contact("ann@x.com"))

    def test_resubscribing_at_mailchimp_counts_only_when_it_is_newer_than_the_opt_out(self):
        link = self.synced_link()
        self.shop.members["L1"] = [member("ann@x.com")]
        self.sync()
        subscription = self.subscription("ann@x.com", link)
        subscription.opt_out = True
        subscription.opt_out_datetime = fields.Datetime.now()
        # Mailchimp still says subscribed, but that is older than the opt-out in Odoo
        self.shop.members["L1"] = [member("ann@x.com", changed="2020-01-01T00:00:00+00:00")]
        link.last_pull = False
        self.sync()
        self.assertTrue(subscription.opt_out)
        # now they sign up again at Mailchimp, after the opt-out
        future = (fields.Datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        self.shop.members["L1"] = [member("ann@x.com", changed=future)]
        link.last_pull = False
        self.sync()
        self.assertFalse(subscription.opt_out)

    def test_a_newer_name_at_mailchimp_replaces_the_local_one(self):
        self.synced_link()
        self.shop.members["L1"] = [member("ann@x.com", first="Ann")]
        self.sync()
        future = (fields.Datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        self.shop.members["L1"] = [member("ann@x.com", first="Anna", last="Lee", changed=future)]
        self.account.link_ids.write({"last_pull": False})
        self.sync()
        self.assertEqual(self.contact("ann@x.com").name, "Anna Lee")

    def test_only_changes_since_the_last_sync_are_requested(self):
        self.synced_link()
        self.sync()
        self.shop.calls.clear()
        self.sync()
        members_calls = [c for c in self.shop.calls if c[1] == "lists/L1/members"]
        self.assertTrue(members_calls[0][2]["since_last_changed"].endswith("+00:00"))

    # -- Odoo to Mailchimp ------------------------------------------------------------------

    def test_odoo_contacts_are_sent_and_not_sent_again(self):
        link = self.synced_link()
        link._ensure_list()
        self.env["mailing.contact"].create(
            {"name": "Carl Dee", "email": "carl@x.com", "list_ids": [(6, 0, link.list_id.ids)]}
        )
        self.sync()
        (batch,) = self.shop.pushes()
        self.assertEqual(
            batch,
            [
                {
                    "email_address": "carl@x.com",
                    "status_if_new": "subscribed",
                    "merge_fields": {"FNAME": "Carl", "LNAME": "Dee"},
                }
            ],
        )
        self.shop.calls.clear()
        self.account.link_ids.write({"last_push": False})  # even a full resend skips unchanged contacts
        self.sync()
        self.assertEqual(self.shop.pushes(), [])

    def test_opt_out_in_odoo_is_sent_as_unsubscribed_but_new_opted_out_contacts_are_not(self):
        link = self.synced_link()
        link._ensure_list()
        carl = self.env["mailing.contact"].create(
            {"name": "Carl", "email": "carl@x.com", "list_ids": [(6, 0, link.list_id.ids)]}
        )
        dora = self.env["mailing.contact"].create(
            {
                "name": "Dora",
                "email": "dora@x.com",
                "subscription_ids": [(0, 0, {"list_id": link.list_id.id, "opt_out": True})],
            }
        )
        self.sync()
        self.assertEqual([m["email_address"] for m in self.shop.pushes()[0]], ["carl@x.com"])
        carl.subscription_ids.opt_out = True
        self.shop.calls.clear()
        self.sync()
        (batch,) = self.shop.pushes()
        self.assertEqual(batch[0]["status"], "unsubscribed")
        self.assertEqual(batch[0]["status_if_new"], "unsubscribed")
        self.assertFalse(self.env["marketing.member"].search([("contact_id", "=", dora.id)]))

    def test_blacklisted_contacts_are_sent_as_unsubscribed(self):
        link = self.synced_link()
        link._ensure_list()
        self.env["mailing.contact"].create(
            {"name": "Eve", "email": "eve@x.com", "list_ids": [(6, 0, link.list_id.ids)]}
        )
        self.sync()
        self.env["mail.blacklist"]._add("eve@x.com")
        self.env.invalidate_all()
        self.shop.calls.clear()
        self.sync()
        self.assertEqual(self.shop.pushes()[0][0]["status"], "unsubscribed")

    def test_refused_addresses_are_listed_and_retried_when_fixed(self):
        link = self.synced_link()
        link._ensure_list()
        carl = self.env["mailing.contact"].create(
            {"name": "Carl", "email": "carl@x.com", "list_ids": [(6, 0, link.list_id.ids)]}
        )
        self.shop.reject["carl@x.com"] = "carl@x.com looks fake or invalid"
        self.sync()
        member_row = link.member_ids
        self.assertIn("fake", member_row.sync_error)
        self.assertEqual(link.problem_count, 1)
        self.assertEqual(self.account.problem_count, 1)
        self.assertEqual(self.account.action_view_problems()["domain"][1], ("sync_error", "!=", False))
        self.shop.reject.clear()
        carl.name = "Carl D"
        self.sync()
        self.assertFalse(member_row.sync_error)

    def test_someone_who_left_mailchimp_is_not_subscribed_again(self):
        link = self.synced_link()
        self.shop.members["L1"] = [member("ann@x.com")]
        self.sync()
        self.shop.members["L1"] = [member("ann@x.com", status="unsubscribed", changed="2026-10-29T10:00:01+00:00")]
        self.account.link_ids.write({"last_pull": False})
        self.sync()
        subscription = self.subscription("ann@x.com", link)
        self.assertTrue(subscription.opt_out)
        subscription.opt_out = False  # someone in Odoo ticks them back in
        self.shop.calls.clear()
        self.sync()
        self.assertEqual(self.shop.pushes(), [], "Mailchimp would refuse it; the contact must sign up again themselves")
        self.assertIn("sign-up form", link.member_ids.sync_error)

    def test_a_big_list_is_sent_in_batches(self):
        link = self.synced_link()
        link._ensure_list()
        self.env["mailing.contact"].create(
            [{"name": f"P{i}", "email": f"p{i}@x.com", "list_ids": [(6, 0, link.list_id.ids)]} for i in range(7)]
        )
        with patch("odoo.addons.marketing_connector_base.models.marketing_account.CHUNK", 3):
            self.sync()
        self.assertEqual([len(b) for b in self.shop.pushes()], [3, 3, 1])

    # -- problems -----------------------------------------------------------------------------

    def test_a_dead_key_disconnects_but_a_busy_server_does_not(self):
        self.synced_link()
        self.shop.fail["lists"] = MarketingError("Mailchimp says there are too many requests.")
        self.assertFalse(self.sync())
        self.assertEqual(self.account.state, "connected")
        self.assertIn("too many", self.account.connection_message)
        self.shop.fail["lists"] = MarketingAuthError("Mailchimp rejected the API key.")
        self.assertFalse(self.sync())
        self.assertEqual(self.account.state, "error")

    def test_one_broken_audience_does_not_stop_the_others(self):
        first = self.synced_link()
        second = self.account.link_ids - first
        second.sync = True
        self.shop.fail["lists/L1/members"] = MarketingError("Mailchimp could not find it: gone")
        self.shop.members["L2"] = [member("zed@x.com")]
        self.assertFalse(self.sync())
        self.assertIn("gone", first.error)
        self.assertFalse(second.error)
        self.assertTrue(self.contact("zed@x.com"))
        self.assertIn("Some lists", self.account.connection_message)
        self.assertEqual(self.account.state, "connected")

    def test_the_scheduled_run_only_touches_connected_accounts(self):
        self.synced_link()
        draft = self.env["marketing.account"].create({"name": "Draft", "provider": "mailchimp", "api_key": "k-us1"})
        with patch(REQUEST, side_effect=self.shop):
            self.env["marketing.account"]._cron_sync()
        self.assertTrue(self.account.last_sync)
        self.assertFalse(draft.last_sync)

    def test_sync_now_button_reports_the_outcome(self):
        link = self.synced_link()
        with patch(REQUEST, side_effect=self.shop):
            self.assertEqual(self.account.action_sync_now()["params"]["type"], "success")
            self.assertEqual(link.action_sync_now()["params"]["type"], "success")
            self.shop.fail["lists/L1/members"] = MarketingError("Mailchimp refused this: nope")
            self.assertEqual(link.action_sync_now()["params"]["type"], "danger")
            self.assertEqual(self.account.action_sync_now()["params"]["type"], "danger")

    # -- campaigns ------------------------------------------------------------------------------

    def test_campaign_results_are_updated_not_duplicated(self):
        self.synced_link()
        self.shop.reports = [
            {
                "id": "c1",
                "campaign_title": "October news",
                "subject_line": "Hi",
                "list_name": "Newsletter",
                "send_time": "2026-10-28T08:00:00+00:00",
                "emails_sent": 200,
                "opens": {"unique_opens": 80},
                "clicks": {"unique_subscriber_clicks": 20},
                "unsubscribed": 1,
                "bounces": {"hard_bounces": 0, "soft_bounces": 2},
            }
        ]
        self.sync()
        campaign = self.account.campaign_ids
        self.assertEqual((campaign.emails_sent, campaign.opens, campaign.bounces), (200, 80, 2))
        self.assertEqual((campaign.open_rate, campaign.click_rate), (40.0, 10.0))
        self.shop.reports[0]["opens"]["unique_opens"] = 100
        self.sync()
        self.assertEqual(len(self.account.campaign_ids), 1)
        self.assertEqual(self.account.campaign_ids.opens, 100)

    # -- webhooks ---------------------------------------------------------------------------------

    def test_webhooks_are_registered_once_per_synced_audience(self):
        link = self.synced_link()
        self.assertTrue(self.account.supports_webhooks)
        with patch(REQUEST, side_effect=self.shop):
            self.account._mailchimp_register_webhooks()
            self.account._mailchimp_register_webhooks()
        posts = [c for c in self.shop.calls if c[1] == "lists/L1/webhooks"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][3]["sources"]["api"], False)
        self.assertIn(f"/marketing/mailchimp/webhook/{self.account.id}/", posts[0][3]["url"])
        self.assertEqual(link.webhook_ref, "hook-L1")
        self.assertTrue(self.account.webhooks_registered)

    def test_http_register_needs_a_public_address(self):
        self.synced_link()
        with self.assertRaises(Exception) as caught:
            self.account.action_register_webhooks()
        self.assertIn("reachable from the internet", str(caught.exception))

    def test_webhook_events_apply_just_that_contact(self):
        link = self.synced_link()
        self.shop.members["L1"] = [member("ann@x.com")]
        self.sync()
        self.shop.members["L1"] = [member("ann@x.com", status="unsubscribed", changed="2026-10-30T10:00:00+00:00")]
        form = {"type": "unsubscribe", "data[list_id]": "L1", "data[email]": "ann@x.com"}
        with patch(REQUEST, side_effect=self.shop):
            self.account._handle_webhook(b"", form)
        self.assertTrue(self.subscription("ann@x.com", link).opt_out)
        self.assertEqual(
            self.account._mailchimp_parse_webhook(b"", {"data[list_id]": "L1", "data[new_email]": "n@x.com"}),
            [("L1", "n@x.com")],
        )
        self.assertEqual(self.account._mailchimp_parse_webhook(b"", {}), [])
        # an address Mailchimp no longer has, or another audience, is ignored
        with patch(REQUEST, side_effect=self.shop):
            self.account._handle_webhook(b"", {"data[list_id]": "L1", "data[email]": "ghost@x.com"})
            self.account._handle_webhook(b"", {"data[list_id]": "L9", "data[email]": "ann@x.com"})
        self.assertFalse(self.contact("ghost@x.com"))


@tagged("post_install", "-at_install")
class TestMailchimpWebhookRoute(HttpCase):
    def test_route_checks_the_secret_and_answers_mailchimp_get(self):
        account = self.env["marketing.account"].create({"name": "M", "provider": "mailchimp", "api_key": "k-us1"})
        base = f"/marketing/mailchimp/webhook/{account.id}"
        self.assertEqual(self.url_open(f"{base}/wrong").status_code, 401)
        self.assertEqual(
            self.url_open(f"/marketing/brevo/webhook/{account.id}/{account.webhook_token}").status_code, 401
        )
        self.assertEqual(self.url_open(f"{base}/{account.webhook_token}").status_code, 200)
        with patch.object(type(account), "_handle_webhook") as handler:
            response = self.url_open(
                f"{base}/{account.webhook_token}", data={"type": "unsubscribe", "data[email]": "a@x.com"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(handler.called)
