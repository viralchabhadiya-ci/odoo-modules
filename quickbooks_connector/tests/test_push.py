from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models.quickbooks_error import QuickBooksAuthError, QuickBooksError
from .fake import QuickBooksCase


@tagged("post_install", "-at_install")
class TestPushDocuments(QuickBooksCase):
    def test_an_invoice_creates_its_customer_item_and_invoice(self):
        invoice = self.invoice(price=100.0, qty=2.0)
        stats = self.sync()
        self.assertEqual((stats["documents sent"], stats["failed"]), (1, 0))

        customer = self.fake.calls_to("Customer")[0]
        self.assertEqual(customer["DisplayName"], "QB Acme")
        self.assertEqual(customer["PrimaryEmailAddr"], {"Address": "ap@acme.test"})
        self.assertEqual(customer["BillAddr"], {"City": "Dallas"})
        item = self.fake.calls_to("Item")[0]
        self.assertEqual(
            (item["Name"], item["Type"], item["IncomeAccountRef"]), ("Odoo sales line", "Service", {"value": "11"})
        )

        sent = self.fake.calls_to("Invoice")[0]
        remote_customer = self.link(self.customer, "Customer").remote_id
        self.assertEqual(sent["CustomerRef"], {"value": remote_customer})
        self.assertEqual(
            (sent["TxnDate"], sent["DocNumber"], sent["GlobalTaxCalculation"]),
            ("2026-10-01", invoice.name[:21], "TaxExcluded"),
        )
        self.assertEqual(sent["BillEmail"], {"Address": "ap@acme.test"})
        (line,) = sent["Line"]
        self.assertEqual((line["Amount"], line["DetailType"]), (200.0, "SalesItemLineDetail"))
        detail = line["SalesItemLineDetail"]
        self.assertEqual((detail["Qty"], detail["UnitPrice"], detail["TaxCodeRef"]), (2.0, 100.0, {"value": "NON"}))
        link = self.link(invoice, "Invoice")
        self.assertTrue(link.remote_id)
        self.assertFalse(link.error)
        self.assertTrue(self.account.last_sync)

    def test_nothing_is_sent_twice(self):
        self.invoice()
        self.sync()
        calls = len(self.fake.calls)
        stats = self.sync()
        self.assertEqual(len(self.fake.calls), calls)
        self.assertFalse(stats["documents sent"])

    def test_old_documents_stay_out_until_the_date_is_moved(self):
        old = self.invoice(date="2026-01-10")
        self.account.sync_from = "2026-06-01"
        self.sync()
        self.assertFalse(self.fake.calls_to("Invoice"))
        self.account.sync_from = "2026-01-01"
        self.sync()
        self.assertTrue(self.link(old, "Invoice").remote_id)

    def test_discounts_are_folded_into_the_unit_price(self):
        self.invoice(price=100.0, qty=3.0, discount=10.0)
        self.sync()
        line = self.fake.calls_to("Invoice")[0]["Line"][0]
        self.assertEqual(line["Amount"], 270.0)
        self.assertEqual(line["SalesItemLineDetail"]["UnitPrice"], 90.0)

    def test_products_become_items_with_their_income_account(self):
        income = self.company_data["default_account_revenue"]
        product = self.env["product.product"].create(
            {"name": "Widget: Large", "default_code": "W-1", "list_price": 50.0, "type": "service"}
        )
        mapped = self.env["quickbooks.ref"].create(
            {
                "account_id": self.account.id,
                "kind": "account",
                "remote_id": "77",
                "name": "Widget sales",
                "account_type": "Income",
            }
        )
        product.product_tmpl_id.with_company(self.company).property_account_income_id = income
        income.with_company(self.company).quickbooks_ref_id = mapped
        self.invoice(product=product)
        self.sync()
        item = self.fake.calls_to("Item")[0]
        self.assertEqual((item["Name"], item["Sku"], item["Type"]), ("Widget- Large", "W-1", "Service"))
        self.assertEqual(item["IncomeAccountRef"], {"value": "77"})

    def test_us_companies_flag_taxed_lines(self):
        tax = self.env["account.tax"].create({"name": "QB US sales tax", "amount": 10.0, "type_tax_use": "sale"})
        self.invoice(taxes=tax)
        self.sync()
        detail = self.fake.calls_to("Invoice")[0]["Line"][0]["SalesItemLineDetail"]
        self.assertEqual(detail["TaxCodeRef"], {"value": "TAX"})

    def test_outside_the_us_every_line_needs_a_mapped_tax_code(self):
        self.account.country_code = "GB"
        tax = self.env["account.tax"].create({"name": "QB UK VAT", "amount": 10.0, "type_tax_use": "sale"})
        taxed, free = self.invoice(taxes=tax), self.invoice()
        stats = self.sync()
        self.assertEqual((stats["documents sent"], stats["failed"]), (1, 1))
        failed = self.link(taxed, "Invoice")
        self.assertIn("has no QuickBooks tax code", failed.error)
        self.assertEqual(failed.attempts, 1)
        self.assertIn(taxed.name, self.account.sync_errors)
        detail = self.fake.calls_to("Invoice")[0]["Line"][0]["SalesItemLineDetail"]
        self.assertEqual(detail["TaxCodeRef"], {"value": "T0"})  # the free invoice, with the zero-rate code
        self.assertTrue(self.link(free, "Invoice").remote_id)

        tax.quickbooks_taxcode_id = self.vat10
        self.sync()
        self.assertFalse(self.link(taxed, "Invoice").error)
        self.assertEqual(
            self.fake.calls_to("Invoice")[1]["Line"][0]["SalesItemLineDetail"]["TaxCodeRef"], {"value": "T10"}
        )
        self.assertFalse(self.account.sync_errors)

    def test_a_credit_note_becomes_a_credit_memo(self):
        note = self.invoice(move_type="out_refund", price=30.0)
        self.sync()
        self.assertEqual(self.fake.calls_to("CreditMemo")[0]["Line"][0]["Amount"], 30.0)
        self.assertTrue(self.link(note, "CreditMemo").remote_id)

    def test_a_vendor_bill_uses_expense_account_lines(self):
        expense = self.company_data["default_account_expense"]
        mapped = self.env["quickbooks.ref"].create(
            {
                "account_id": self.account.id,
                "kind": "account",
                "remote_id": "88",
                "name": "Rent",
                "account_type": "Expense",
            }
        )
        expense.with_company(self.company).quickbooks_ref_id = mapped
        bill = self.invoice(move_type="in_invoice", price=80.0)
        bill.invoice_line_ids.account_id = expense
        self.sync()
        sent = self.fake.calls_to("Bill")[0]
        self.assertEqual(sent["DocNumber"], "VB-77")
        self.assertEqual(sent["VendorRef"], {"value": self.link(self.vendor, "Vendor").remote_id})
        (line,) = sent["Line"]
        self.assertEqual((line["DetailType"], line["Amount"]), ("AccountBasedExpenseLineDetail", 80.0))
        self.assertEqual(line["AccountBasedExpenseLineDetail"]["AccountRef"], {"value": "88"})
        self.assertTrue(self.link(bill, "Bill").remote_id)

    def test_a_vendor_credit(self):
        self.invoice(move_type="in_refund", price=20.0)
        self.sync()
        self.assertEqual(len(self.fake.calls_to("VendorCredit")), 1)

    def test_sections_and_notes_travel_as_description_lines(self):
        move = self.invoice(post=False)
        self.env["account.move.line"].create(
            {"move_id": move.id, "display_type": "line_note", "name": "Thanks for your business"}
        )
        move.action_post()
        self.sync()
        kinds = [line["DetailType"] for line in self.fake.calls_to("Invoice")[0]["Line"]]
        self.assertEqual(sorted(kinds), ["DescriptionOnly", "SalesItemLineDetail"])

    def test_foreign_currency_documents_carry_their_rate(self):
        currency = self.setup_other_currency("EUR")
        move = self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": self.customer.id,
                "invoice_date": "2026-10-01",
                "currency_id": currency.id,
                "invoice_line_ids": [(0, 0, {"name": "x", "quantity": 1, "price_unit": 50.0})],
            }
        )
        move.action_post()
        self.sync()
        sent = self.fake.calls_to("Invoice")[0]
        self.assertEqual(sent["CurrencyRef"], {"value": currency.name})
        self.assertGreater(sent["ExchangeRate"], 0)

    def test_a_different_total_in_quickbooks_is_flagged(self):
        move = self.invoice(price=100.0)
        self.fake.total_factor = 1.1
        self.sync()
        self.assertIn("differs from Odoo 100.00", self.link(move, "Invoice").note)

    def test_a_document_taken_back_to_draft_is_voided_and_sent_again_when_reposted(self):
        invoice = self.invoice()
        self.sync()
        first = self.link(invoice, "Invoice").remote_id
        invoice.button_draft()
        stats = self.sync()
        self.assertEqual(stats["documents withdrawn"], 1)
        self.assertEqual(self.fake.calls_to("Invoice", "void")[0]["Id"], first)
        self.sync()  # voided only once
        self.assertEqual(len(self.fake.calls_to("Invoice", "void")), 1)
        invoice.action_post()
        self.later()
        self.sync()
        second = self.link(invoice, "Invoice").remote_id
        self.assertNotEqual(first, second)
        self.assertNotIn("Id", self.fake.calls_to("Invoice")[-1])

    def test_a_bill_taken_back_to_draft_is_deleted(self):
        bill = self.invoice(move_type="in_invoice")
        self.sync()
        bill.button_draft()
        self.sync()
        self.assertEqual(len(self.fake.calls_to("Bill", "delete")), 1)

    def test_one_failure_does_not_stop_the_rest_and_retries_stop_after_three(self):
        bad, good = self.invoice(), self.invoice(price=5.0)
        for _round in range(4):
            self.fake.fail["Invoice"] = QuickBooksError("QuickBooks says no")
            self.sync()
        link = self.link(bad, "Invoice")
        self.assertEqual(link.attempts, 3)
        self.assertIn("QuickBooks says no", link.error)
        self.assertEqual(
            len(self.fake.calls_to("Invoice")), 3 + 1
        )  # three tries of the bad one, then the good one once
        self.assertTrue(self.link(good, "Invoice").remote_id)
        self.assertEqual(self.account.error_count, 1)
        self.fake.fail.clear()
        self.account.action_retry_failed()
        self.assertTrue(self.link(bad, "Invoice").remote_id)
        self.assertFalse(self.link(bad, "Invoice").error)

    def test_losing_the_connection_stops_the_round_and_asks_to_connect_again(self):
        self.invoice()
        self.fake.fail["Customer"] = QuickBooksAuthError("Token revoked")
        self.sync()
        self.assertEqual((self.account.state, self.account.connection_message), ("error", "Token revoked"))

    def test_send_now_ignores_the_date_limit_and_reports_errors(self):
        old = self.invoice(date="2019-05-05")
        old.action_send_to_quickbooks()
        self.assertTrue(self.link(old, "Invoice").remote_id)
        self.account.country_code = "GB"
        tax = self.env["account.tax"].create({"name": "QB No code", "amount": 5.0, "type_tax_use": "sale"})
        taxed = self.invoice(taxes=tax)
        with self.assertRaises(UserError) as caught:
            taxed.action_send_to_quickbooks()
        self.assertIn("no QuickBooks tax code", str(caught.exception))

    def test_the_cron_only_syncs_connected_automatic_connections(self):
        self.invoice()
        self.account.auto_sync = False
        self.env["quickbooks.account"]._cron_sync()
        self.assertFalse(self.fake.calls)
        self.account.auto_sync = True
        self.env["quickbooks.account"]._cron_sync()
        self.assertTrue(self.fake.calls_to("Invoice"))
        self.assertEqual(self.invoice().quickbooks_state, "none")

    def test_invoices_can_be_switched_off(self):
        self.invoice()
        self.account.push_invoices = False
        self.sync()
        self.assertFalse(self.fake.calls_to("Invoice"))


@tagged("post_install", "-at_install")
class TestPushContacts(QuickBooksCase):
    def test_a_customer_that_is_also_a_vendor_gets_a_distinct_name(self):
        both = self.env["res.partner"].create({"name": "QB Both", "is_company": True})
        self.invoice(partner=both)
        self.invoice(partner=both, move_type="in_invoice")
        self.sync()
        self.assertEqual(self.fake.calls_to("Customer")[0]["DisplayName"], "QB Both")
        self.assertEqual(self.fake.calls_to("Vendor")[0]["DisplayName"], "QB Both (Vendor)")

    def test_a_name_that_exists_in_quickbooks_is_adopted_not_duplicated(self):
        existing = self.fake.remote("Customer", DisplayName="QB Acme")
        move = self.invoice()
        self.sync()
        link = self.link(self.customer, "Customer")
        self.assertEqual(link.remote_id, existing["Id"])
        self.assertEqual(self.fake.calls_to("Customer")[-1]["Id"], existing["Id"])  # updated, not created again
        self.assertEqual(len(self.fake.store["Customer"]), 1)
        self.assertTrue(self.link(move, "Invoice").remote_id)

    def test_a_name_taken_by_another_kind_of_contact_gets_a_suffix(self):
        self.fake.remote("Customer", DisplayName="QB Supplies")  # same name, but a customer there
        self.invoice(move_type="in_invoice")
        self.sync()
        self.assertEqual(self.fake.calls_to("Vendor")[-1]["DisplayName"], "QB Supplies (Vendor)")
        self.assertTrue(self.link(self.vendor, "Vendor").remote_id)

    def test_changes_to_a_sent_contact_are_updated_once(self):
        self.invoice()
        self.sync()
        self.later()
        self.customer.email = "new@acme.test"
        self.sync()
        update = self.fake.calls_to("Customer")[-1]
        link = self.link(self.customer, "Customer")
        self.assertEqual(
            (update["Id"], update["sparse"], update["PrimaryEmailAddr"]),
            (link.remote_id, True, {"Address": "new@acme.test"}),
        )
        calls = len(self.fake.calls)
        self.sync()
        self.assertEqual(len(self.fake.calls), calls)

    def test_a_stale_sync_token_is_refreshed_and_retried(self):
        self.invoice()
        self.sync()
        link = self.link(self.customer, "Customer")
        link.sync_token = "99"
        self.later()
        self.customer.phone = "555-0100"
        self.sync()
        self.assertEqual(self.fake.store["Customer"][link.remote_id]["PrimaryPhone"], {"FreeFormNumber": "555-0100"})
        self.assertFalse(link.error)

    def test_contacts_are_only_created_when_a_document_needs_them(self):
        self.env["res.partner"].create({"name": "QB Nobody", "is_company": True})
        self.sync()
        self.assertFalse(self.fake.calls_to("Customer"))


@tagged("post_install", "-at_install")
class TestPushPayments(QuickBooksCase):
    def test_a_customer_payment_is_sent_against_its_invoice(self):
        invoice = self.invoice(price=100.0)
        self.sync()
        self.pay(invoice, 40.0)
        stats = self.sync()
        self.assertEqual(stats["payments sent"], 1)
        sent = self.fake.calls_to("Payment")[0]
        self.assertEqual(sent["TotalAmt"], 40.0)
        self.assertEqual(sent["CustomerRef"], {"value": self.link(self.customer, "Customer").remote_id})
        self.assertEqual(sent["DepositToAccountRef"], {"value": "13"})
        self.assertEqual(
            sent["Line"],
            [{"Amount": 40.0, "LinkedTxn": [{"TxnId": self.link(invoice, "Invoice").remote_id, "TxnType": "Invoice"}]}],
        )
        calls = len(self.fake.calls)
        self.sync()
        self.assertEqual(len(self.fake.calls), calls)

    def test_a_vendor_payment_is_sent_as_a_bill_payment(self):
        bill = self.invoice(move_type="in_invoice", price=60.0)
        self.sync()
        payment = (
            self.env["account.payment.register"]
            .with_context(active_model="account.move", active_ids=bill.ids)
            .create({"payment_date": "2026-10-06", "journal_id": self.company_data["default_journal_bank"].id})
            ._create_payments()
        )
        self.sync()
        sent = self.fake.calls_to("BillPayment")[0]
        self.assertEqual((sent["PayType"], sent["TotalAmt"]), ("Check", 60.0))
        self.assertEqual(sent["CheckPayment"], {"BankAccountRef": {"value": "13"}})
        self.assertEqual(sent["Line"][0]["LinkedTxn"][0]["TxnType"], "Bill")
        self.assertTrue(self.link(payment, "BillPayment").remote_id)

    def test_a_payment_waits_until_its_invoice_is_in_quickbooks(self):
        invoice = self.invoice(price=100.0)
        self.account.push_invoices = False
        self.sync()
        self.pay(invoice, 40.0)
        stats = self.sync()
        self.assertFalse(self.fake.calls_to("Payment"))
        self.assertFalse(stats["failed"])
        self.account.push_invoices = True
        self.account.link_ids.filtered(lambda l: l.entity == "Payment").write(
            {"last_sync": False}
        )  # as if an hour had passed
        self.sync()
        self.assertEqual(len(self.fake.calls_to("Payment")), 1)

    def test_payments_can_be_switched_off(self):
        invoice = self.invoice()
        self.sync()
        self.pay(invoice, 10.0)
        self.account.push_payments = False
        self.sync()
        self.assertFalse(self.fake.calls_to("Payment"))
