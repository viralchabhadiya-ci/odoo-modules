from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from ..models.quickbooks_error import QuickBooksAuthError
from .fake import QuickBooksCase

OAUTH = "odoo.addons.quickbooks_connector.models.quickbooks_account.oauth_post"


@tagged("post_install", "-at_install")
class TestConnection(QuickBooksCase):
    def test_connect_sends_the_user_to_intuit_with_a_signed_state(self):
        action = self.account.action_connect()
        url = urlparse(action["url"])
        query = parse_qs(url.query)
        self.assertEqual(url.netloc, "appcenter.intuit.com")
        self.assertEqual(query["client_id"], ["cid"])
        self.assertEqual(query["scope"], ["com.intuit.quickbooks.accounting"])
        self.assertTrue(query["redirect_uri"][0].endswith("/quickbooks/oauth/callback"))
        self.assertEqual(self.env["quickbooks.account"]._from_state(query["state"][0]), self.account)

    def test_a_forged_state_is_refused(self):
        State = self.env["quickbooks.account"]._from_state
        self.assertFalse(State(f"{self.account.id}-forged"))
        self.assertFalse(State("x-y"))
        self.assertFalse(State(None))
        self.assertFalse(State("99999-" + self.account._state_token().partition("-")[2]))

    def test_connect_needs_the_app_keys(self):
        self.account.client_secret = False
        with self.assertRaises(UserError):
            self.account.action_connect()

    def test_oauth_completion_stores_the_tokens_and_loads_the_company(self):
        self.token.unlink()
        self.account.write({"state": "draft", "realm_id": False})
        body = {"access_token": "A2", "refresh_token": "R2", "expires_in": 3600}
        self.fake.store["Account"] = {}
        with patch(OAUTH, return_value=body) as post:
            self.account._complete_oauth("the-code", "4620816365")
        data = post.call_args.args[2]
        self.assertEqual((data["grant_type"], data["code"]), ("authorization_code", "the-code"))
        self.assertEqual(self.account.realm_id, "4620816365")
        self.assertEqual(self.account.state, "connected")
        self.assertEqual(self.account.remote_name, "Sandbox Company")
        self.assertEqual(self.account.country_code, "US")
        self.assertEqual(
            self.env["quickbooks.token"].sudo().search([("account_id", "=", self.account.id)]).refresh_token, "R2"
        )

    def test_a_fresh_token_is_reused(self):
        with patch(OAUTH) as post:
            self.assertEqual(self.account._access_token(), "a1")
        post.assert_not_called()

    def test_an_expiring_token_is_refreshed_and_the_new_refresh_token_kept(self):
        self.token.write({"expiry": fields.Datetime.now() + timedelta(minutes=1)})
        self.env.flush_all()  # the refresh reads through its own environment
        with patch(OAUTH, return_value={"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}) as post:
            self.assertEqual(self.account._access_token(), "a2")
        self.assertEqual(post.call_args.args[2], {"grant_type": "refresh_token", "refresh_token": "r1"})
        self.token.invalidate_recordset()
        self.assertEqual((self.token.access_token, self.token.refresh_token), ("a2", "r2"))
        self.assertGreater(self.token.expiry, fields.Datetime.now() + timedelta(minutes=50))

    def test_not_connected(self):
        self.token.refresh_token = False
        with self.assertRaises(QuickBooksAuthError):
            self.account._access_token()

    def test_test_connection_marks_a_lost_connection(self):
        with patch.object(type(self.fake), "company_info", side_effect=QuickBooksAuthError("Token revoked")):
            result = self.account.action_test()
        self.assertEqual(result["params"]["type"], "danger")
        self.assertEqual((self.account.state, self.account.connection_message), ("error", "Token revoked"))
        result = self.account.action_test()
        self.assertEqual(result["params"]["type"], "success")
        self.assertEqual((self.account.state, self.account.connection_message), ("connected", False))

    def test_disconnect_forgets_the_tokens(self):
        with patch("odoo.addons.quickbooks_connector.models.quickbooks_account.requests") as http:
            self.account.action_disconnect()
        self.assertEqual(http.post.call_args.kwargs["json"], {"token": "r1"})
        self.assertEqual((self.account.state, self.account.realm_id), ("draft", False))
        self.assertFalse(self.env["quickbooks.token"].sudo().search([("account_id", "=", self.account.id)]))

    def test_refreshing_lists_loads_accounts_and_tax_codes_and_matches_ours(self):
        self.account.write(
            {"income_ref_id": False, "expense_ref_id": False, "bank_ref_id": False, "zero_tax_ref_id": False}
        )
        self.account.ref_ids.unlink()
        sales_tax = self.env["account.tax"].create({"name": "QB VAT 10%", "amount": 10.0, "type_tax_use": "sale"})
        by_rate = self.env["account.tax"].create({"name": "Local levy", "amount": 5.0, "type_tax_use": "sale"})
        odoo_income = self.company_data["default_account_revenue"]
        odoo_income.code = "400000"
        self.fake.store["Account"] = {
            "1": {"Id": "1", "Name": "Sales of Product Income", "AccountType": "Income"},
            "2": {"Id": "2", "Name": "Cost of Goods Sold", "AccountType": "Cost of Goods Sold"},
            "3": {"Id": "3", "Name": "Checking", "AccountType": "Bank"},
            "4": {"Id": "4", "Name": "Revenue", "AccountType": "Income", "AcctNum": "400000"},
        }
        self.fake.store["TaxRate"] = {"r1": {"Id": "r1", "RateValue": 5.0}, "r2": {"Id": "r2", "RateValue": 10.0}}
        self.fake.store["TaxCode"] = {
            "t1": {
                "Id": "t1",
                "Name": "QB VAT 10%",
                "SalesTaxRateList": {"TaxRateDetail": [{"TaxRateRef": {"value": "r2"}}]},
            },
            "t2": {
                "Id": "t2",
                "Name": "Five percent",
                "SalesTaxRateList": {"TaxRateDetail": [{"TaxRateRef": {"value": "r1"}}]},
            },
            "t3": {"Id": "t3", "Name": "Exempt", "SalesTaxRateList": {"TaxRateDetail": []}},
        }
        self.account.action_refresh_lists()
        refs = {(r.kind, r.remote_id): r for r in self.account.ref_ids}
        self.assertEqual(len(refs), 7)
        self.assertEqual(refs[("taxcode", "t2")].rate, 5.0)
        self.assertEqual(sales_tax.quickbooks_taxcode_id, refs[("taxcode", "t1")])
        self.assertEqual(
            by_rate.quickbooks_taxcode_id, refs[("taxcode", "t2")]
        )  # no name match, one code with that rate
        self.assertEqual(
            odoo_income.with_company(self.company).quickbooks_ref_id, refs[("account", "4")]
        )  # by account number
        self.assertEqual(self.account.income_ref_id, refs[("account", "1")])  # "sales" preferred
        self.assertEqual(self.account.expense_ref_id, refs[("account", "2")])
        self.assertEqual(self.account.bank_ref_id, refs[("account", "3")])
        self.assertEqual(self.account.zero_tax_ref_id, refs[("taxcode", "t3")])
        # a choice made by hand is never overwritten, and entries that vanished are archived
        sales_tax.quickbooks_taxcode_id = refs[("taxcode", "t3")]
        del self.fake.store["Account"]["2"]
        self.account.action_refresh_lists()
        self.assertEqual(sales_tax.quickbooks_taxcode_id, refs[("taxcode", "t3")])
        self.assertFalse(refs[("account", "2")].active)


@tagged("post_install", "-at_install")
class TestCallback(HttpCase):
    def setUp(self):
        super().setUp()
        self.env["quickbooks.account"].search([]).unlink()
        self.account = self.env["quickbooks.account"].create(
            {"company_id": self.env.company.id, "client_id": "c", "client_secret": "s"}
        )
        self.authenticate("admin", "admin")

    def callback(self, **params):
        return self.url_open(
            "/quickbooks/oauth/callback?" + "&".join(f"{k}={v}" for k, v in params.items()), allow_redirects=False
        )

    def test_callback_completes_the_connection_and_returns_to_the_form(self):
        with patch.object(type(self.account), "_complete_oauth", autospec=True) as complete:
            result = self.callback(code="abc", realmId="777", state=self.account._state_token())
        complete.assert_called_once()
        self.assertEqual(complete.call_args.args[1:], ("abc", "777"))
        self.assertEqual(result.status_code, 303)
        self.assertIn(f"quickbooks_account_action/{self.account.id}", result.headers["Location"])

    def test_callback_with_a_bad_state_does_nothing(self):
        with patch.object(type(self.account), "_complete_oauth", autospec=True) as complete:
            result = self.callback(code="abc", realmId="777", state=f"{self.account.id}-forged")
        self.assertEqual(result.status_code, 400)
        complete.assert_not_called()

    def test_a_refusal_at_intuit_is_shown_on_the_connection(self):
        self.callback(error="access_denied", state=self.account._state_token())
        self.account.invalidate_recordset()
        self.assertEqual(self.account.state, "error")
        self.assertIn("access_denied", self.account.connection_message)

    def test_a_failed_exchange_is_shown_on_the_connection(self):
        with patch(OAUTH, side_effect=QuickBooksAuthError("Intuit refused the sign-in: bad code")):
            self.callback(code="abc", realmId="777", state=self.account._state_token())
        self.account.invalidate_recordset()
        self.assertEqual(self.account.state, "error")
        self.assertIn("bad code", self.account.connection_message)
