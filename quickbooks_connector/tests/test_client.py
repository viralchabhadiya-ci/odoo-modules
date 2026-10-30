from unittest.mock import MagicMock, patch

import requests

from odoo.tests import TransactionCase, tagged

from ..models import quickbooks_client as qbc
from ..models.quickbooks_error import QuickBooksAuthError, QuickBooksError

REQUESTS = "odoo.addons.quickbooks_connector.models.quickbooks_client.requests"


def response(status=200, body=None, headers=None):
    answer = MagicMock()
    answer.status_code = status
    answer.json.return_value = body if body is not None else {}
    answer.headers = headers or {}
    return answer


@tagged("post_install", "-at_install")
class TestQuickBooksClient(TransactionCase):
    def client(self, **kwargs):
        return qbc.QuickBooksClient("sandbox", "9130", "tok", **kwargs)

    def test_quote(self):
        self.assertEqual(qbc.quote("O'Brien & Co"), "'O\\'Brien & Co'")
        self.assertEqual(qbc.quote("a\\b"), "'a\\\\b'")

    def test_request_sends_token_and_version(self):
        with patch(REQUESTS) as http:
            http.request.return_value = response(200, {"Customer": {"Id": "5"}})
            http.RequestException = requests.RequestException
            saved = self.client().save("Customer", {"DisplayName": "X"})
        self.assertEqual(saved, {"Id": "5"})
        args, kwargs = http.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://sandbox-quickbooks.api.intuit.com/v3/company/9130/customer")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(kwargs["params"]["minorversion"], qbc.MINOR_VERSION)
        self.assertEqual(kwargs["json"], {"DisplayName": "X"})

    def test_production_uses_the_production_host(self):
        self.assertTrue(
            qbc.QuickBooksClient("production", "1", "t").base.startswith("https://quickbooks.api.intuit.com/")
        )

    def test_fault_becomes_a_readable_error_with_its_code_and_trace_id(self):
        fault = {
            "Fault": {
                "type": "ValidationFault",
                "Error": [
                    {"Message": "Duplicate Name Exists Error", "Detail": "The name X already exists", "code": "6240"}
                ],
            }
        }
        with patch(REQUESTS) as http:
            http.request.return_value = response(400, fault, {"intuit_tid": "abc-123"})
            http.RequestException = requests.RequestException
            with self.assertRaises(QuickBooksError) as caught:
                self.client().save("Customer", {})
        self.assertEqual(caught.exception.code, "6240")
        self.assertIn("The name X already exists", str(caught.exception))
        self.assertIn("abc-123", str(caught.exception))

    def test_authentication_fault(self):
        fault = {"Fault": {"type": "AUTHENTICATION", "Error": [{"Message": "AuthenticationFailed", "code": "120"}]}}
        with patch(REQUESTS) as http:
            http.request.return_value = response(200, fault)
            http.RequestException = requests.RequestException
            with self.assertRaises(QuickBooksAuthError):
                self.client().company_info()

    def test_401_refreshes_the_token_once_then_retries(self):
        refresh = MagicMock(return_value="new")
        with patch(REQUESTS) as http:
            http.request.side_effect = [response(401), response(200, {"CompanyInfo": {"CompanyName": "Co"}})]
            http.RequestException = requests.RequestException
            info = self.client(refresh=refresh).company_info()
        self.assertEqual(info["CompanyName"], "Co")
        refresh.assert_called_once()
        self.assertEqual(http.request.call_args.kwargs["headers"]["Authorization"], "Bearer new")

    def test_401_twice_asks_to_connect_again(self):
        with patch(REQUESTS) as http:
            http.request.return_value = response(401)
            http.RequestException = requests.RequestException
            with self.assertRaises(QuickBooksAuthError):
                self.client(refresh=lambda: "new").company_info()

    def test_throttling_is_waited_out(self):
        with patch(REQUESTS) as http, patch.object(qbc.time, "sleep") as sleep:
            http.request.side_effect = [response(429, headers={"Retry-After": "3"}), response(200, {"CompanyInfo": {}})]
            http.RequestException = requests.RequestException
            self.client().company_info()
        sleep.assert_called_once_with(3)

    def test_network_errors_are_reported(self):
        with patch(REQUESTS) as http:
            http.RequestException = requests.RequestException
            http.request.side_effect = requests.ConnectionError("down")
            with self.assertRaises(QuickBooksError) as caught:
                self.client().company_info()
        self.assertIn("could not be reached", str(caught.exception))

    def test_query_all_pages_through_the_results(self):
        pages = [
            {"QueryResponse": {"Customer": [{"Id": str(i)} for i in range(qbc.PAGE)]}},
            {"QueryResponse": {"Customer": [{"Id": "x"}]}},
        ]
        with patch(REQUESTS) as http:
            http.request.side_effect = [response(200, page) for page in pages]
            http.RequestException = requests.RequestException
            rows = self.client().query_all("Customer", "Active = true")
        self.assertEqual(len(rows), qbc.PAGE + 1)
        queries = [call.kwargs["params"]["query"] for call in http.request.call_args_list]
        self.assertEqual(
            queries[0], f"select * from Customer where Active = true startposition 1 maxresults {qbc.PAGE}"
        )
        self.assertIn(f"startposition {qbc.PAGE + 1}", queries[1])

    def test_void_and_delete_use_operations(self):
        with patch(REQUESTS) as http:
            http.request.return_value = response(200, {"Invoice": {}})
            http.RequestException = requests.RequestException
            self.client().void("Invoice", {"Id": "1", "SyncToken": "0"})
            self.client().delete("Bill", {"Id": "2", "SyncToken": "0"})
        operations = [call.kwargs["params"]["operation"] for call in http.request.call_args_list]
        self.assertEqual(operations, ["void", "delete"])

    def test_oauth_post(self):
        with patch(REQUESTS) as http:
            http.RequestException = requests.RequestException
            http.post.return_value = response(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
            self.assertEqual(qbc.oauth_post("id", "secret", {"grant_type": "x"})["access_token"], "a")
            self.assertEqual(http.post.call_args.kwargs["auth"], ("id", "secret"))
            http.post.return_value = response(400, {"error": "invalid_grant", "error_description": "Token expired"})
            with self.assertRaises(QuickBooksAuthError) as caught:
                qbc.oauth_post("id", "secret", {})
            self.assertIn("Token expired", str(caught.exception))
            http.post.return_value = response(503, {})
            with self.assertRaises(QuickBooksError):
                qbc.oauth_post("id", "secret", {})
