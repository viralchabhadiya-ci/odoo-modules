from odoo.tests import tagged

from .fake import QuickBooksCase


@tagged("post_install", "-at_install")
class TestPullContacts(QuickBooksCase):
    def test_new_customers_and_vendors_are_brought_in_once(self):
        us = self.env.ref("base.us")
        self.fake.remote(
            "Customer",
            DisplayName="QB Remote Ltd",
            CompanyName="QB Remote Ltd",
            PrimaryEmailAddr={"Address": "hi@remote.test"},
            PrimaryPhone={"FreeFormNumber": "555-0199"},
            BillAddr={
                "Line1": "1 Main St",
                "City": "Austin",
                "PostalCode": "73301",
                "Country": us.name,
                "CountrySubDivisionCode": "TX",
            },
        )
        self.fake.remote("Vendor", DisplayName="QB Remote Vendor", CompanyName="QB Remote Vendor")
        stats = self.sync()
        self.assertEqual(stats["contacts brought in"], 2)
        partner = self.env["res.partner"].search([("name", "=", "QB Remote Ltd")])
        self.assertEqual(len(partner), 1)
        self.assertEqual(
            (partner.email, partner.phone, partner.city, partner.street, partner.zip),
            ("hi@remote.test", "555-0199", "Austin", "1 Main St", "73301"),
        )
        self.assertEqual((partner.country_id, partner.state_id.code, partner.is_company), (us, "TX", True))
        self.assertGreater(partner.customer_rank, 0)
        vendor = self.env["res.partner"].search([("name", "=", "QB Remote Vendor")])
        self.assertGreater(vendor.supplier_rank, 0)
        self.assertEqual(
            self.link(partner, "Customer").remote_id,
            self.fake.store["Customer"][self.link(partner, "Customer").remote_id]["Id"],
        )

        self.sync()  # nothing new, and nothing is sent back
        self.assertEqual(self.env["res.partner"].search_count([("name", "=", "QB Remote Ltd")]), 1)
        self.assertFalse(self.fake.calls_to("Customer"))
        self.assertIn("MetaData.LastUpdatedTime >", self.fake.where["Customer"])

    def test_the_first_round_asks_for_everything(self):
        self.sync()
        self.assertEqual(self.fake.where["Customer"], "")

    def test_a_contact_that_exists_in_odoo_is_linked_not_duplicated(self):
        self.fake.remote(
            "Customer",
            DisplayName="Someone Else Ltd",
            CompanyName="Someone Else Ltd",
            PrimaryEmailAddr={"Address": "AP@ACME.test"},
        )
        self.fake.remote("Vendor", DisplayName="QB Supplies", CompanyName="QB Supplies")
        self.sync()
        self.assertTrue(self.link(self.customer, "Customer").remote_id)  # matched by email, whatever the case
        self.assertTrue(self.link(self.vendor, "Vendor").remote_id)  # matched by name
        self.assertEqual(self.env["res.partner"].search_count([("name", "=", "Someone Else Ltd")]), 0)
        self.assertGreater(self.vendor.supplier_rank, 0)

    def test_inactive_contacts_are_not_brought_in(self):
        self.fake.remote("Customer", DisplayName="QB Gone", Active=False)
        self.sync()
        self.assertFalse(self.env["res.partner"].search([("name", "=", "QB Gone")]))

    def test_a_change_in_quickbooks_updates_odoo(self):
        record = self.fake.remote(
            "Customer",
            DisplayName="QB Remote Ltd",
            CompanyName="QB Remote Ltd",
            PrimaryEmailAddr={"Address": "a@remote.test"},
        )
        self.sync()
        partner = self.env["res.partner"].search([("name", "=", "QB Remote Ltd")])
        self.fake.change("Customer", record["Id"], PrimaryEmailAddr={"Address": "b@remote.test"})
        self.sync()
        self.assertEqual(partner.email, "b@remote.test")
        self.assertFalse(self.fake.calls_to("Customer"))  # not echoed back

    def test_when_both_sides_changed_odoo_wins(self):
        record = self.fake.remote(
            "Customer",
            DisplayName="QB Remote Ltd",
            CompanyName="QB Remote Ltd",
            PrimaryEmailAddr={"Address": "a@remote.test"},
        )
        self.sync()
        partner = self.env["res.partner"].search([("name", "=", "QB Remote Ltd")])
        self.later()
        partner.email = "odoo@remote.test"
        self.fake.change("Customer", record["Id"], PrimaryEmailAddr={"Address": "qb@remote.test"})
        self.sync()
        self.assertEqual(partner.email, "odoo@remote.test")
        self.assertEqual(self.fake.store["Customer"][record["Id"]]["PrimaryEmailAddr"], {"Address": "odoo@remote.test"})

    def test_contacts_can_be_switched_off(self):
        self.fake.remote("Customer", DisplayName="QB Remote Ltd")
        self.account.pull_contacts = False
        self.sync()
        self.assertFalse(self.env["res.partner"].search([("name", "=", "QB Remote Ltd")]))


@tagged("post_install", "-at_install")
class TestPullPayments(QuickBooksCase):
    def setUp(self):
        super().setUp()
        self.account.payment_journal_id = self.company_data["default_journal_bank"]

    def remote_payment(self, invoice, amount, **extra):
        remote_id = self.link(invoice, "Invoice").remote_id
        return self.fake.remote(
            "Payment",
            TxnDate="2026-10-07",
            PaymentRefNum="CHK-9",
            Line=[{"Amount": amount, "LinkedTxn": [{"TxnId": remote_id, "TxnType": "Invoice"}]}],
            **extra,
        )

    def test_a_payment_entered_in_quickbooks_is_registered_in_odoo(self):
        invoice = self.invoice(price=100.0)
        self.sync()
        record = self.remote_payment(invoice, 40.0)
        stats = self.sync()
        self.assertEqual(stats["payments brought in"], 1)
        self.assertEqual(invoice.amount_residual, 60.0)
        payment = invoice._get_reconciled_payments()
        self.assertEqual((payment.amount, str(payment.date)), (40.0, "2026-10-07"))
        self.assertIn("CHK-9", payment.memo)
        self.assertEqual(
            self.link(payment, "Payment").remote_id, f"{record['Id']}/{self.link(invoice, 'Invoice').remote_id}"
        )

        self.fake.change("Payment", record["Id"], PrivateNote="touched")  # seen again: not registered twice
        self.sync()
        self.assertEqual(invoice.amount_residual, 60.0)
        self.assertFalse(self.fake.calls_to("Payment"))  # and not sent back

    def test_a_payment_sent_from_odoo_is_not_brought_back(self):
        invoice = self.invoice(price=100.0)
        self.sync()
        self.pay(invoice, 40.0)
        self.sync()
        sent = self.fake.store["Payment"]
        (key,) = sent
        self.fake.pending["Payment"] = [key]
        self.sync()
        self.assertEqual(invoice.amount_residual, 60.0)

    def test_payments_for_unknown_or_paid_invoices_are_ignored(self):
        invoice = self.invoice(price=100.0)
        self.sync()
        self.fake.remote("Payment", Line=[{"Amount": 5.0, "LinkedTxn": [{"TxnId": "nope", "TxnType": "Invoice"}]}])
        self.fake.remote(
            "Payment",
            Line=[
                {
                    "Amount": 5.0,
                    "LinkedTxn": [{"TxnId": self.link(invoice, "Invoice").remote_id, "TxnType": "CreditMemo"}],
                }
            ],
        )
        self.pay(invoice, 100.0)
        self.sync()
        self.remote_payment(invoice, 100.0)
        stats = self.sync()
        self.assertFalse(stats["payments brought in"])
        self.assertEqual(invoice.amount_residual, 0.0)

    def test_a_payment_cannot_be_registered_without_a_journal(self):
        invoice = self.invoice(price=100.0)
        self.sync()
        self.account.payment_journal_id = False
        self.remote_payment(invoice, 40.0)
        self.sync()
        self.assertEqual(invoice.amount_residual, 100.0)

    def test_bill_payments_are_brought_in(self):
        bill = self.invoice(move_type="in_invoice", price=70.0)
        self.sync()
        self.fake.remote(
            "BillPayment",
            TxnDate="2026-10-08",
            Line=[{"Amount": 70.0, "LinkedTxn": [{"TxnId": self.link(bill, "Bill").remote_id, "TxnType": "Bill"}]}],
        )
        self.sync()
        self.assertEqual(bill.amount_residual, 0.0)

    def test_bringing_payments_in_can_be_switched_off(self):
        invoice = self.invoice(price=100.0)
        self.sync()
        self.remote_payment(invoice, 40.0)
        self.account.pull_payments = False
        self.sync()
        self.assertEqual(invoice.amount_residual, 100.0)
