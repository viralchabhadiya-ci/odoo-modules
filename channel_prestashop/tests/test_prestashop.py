import copy
import re
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError
from odoo.tests import tagged

from ..models.channel_store import normalize_order, normalize_slip, to_local, to_utc
from ..models.ps_client import PrestaClient, normalize_site, text, to_xml

REQUEST = "odoo.addons.channel_prestashop.models.ps_client.PrestaClient.request"
HTTP = "odoo.addons.channel_prestashop.models.ps_client.requests.request"

ORDER = {
    "id": "11", "reference": "ABCDEF", "id_customer": "5", "id_address_invoice": "20", "id_address_delivery": "21",
    "id_currency": "1", "id_carrier": "2", "current_state": "2", "valid": "1", "payment": "Credit card", "module": "stripe",
    "date_add": "2026-10-29 12:00:00", "date_upd": "2026-10-29 12:05:00",
    "total_shipping_tax_excl": "5.000000", "total_shipping_tax_incl": "6.000000", "carrier_tax_rate": "20.000",
    "total_discounts_tax_excl": "2.000000", "total_discounts_tax_incl": "2.400000",
    "total_wrapping_tax_excl": "0.000000", "total_wrapping_tax_incl": "0.000000",
    "associations": {
        "order_rows": [
            {"id": "101", "product_id": "7", "product_attribute_id": "0", "product_quantity": "2", "product_name": "T-shirt",
             "product_reference": "TS-1", "product_ean13": "", "original_product_price": "12.500000",
             "unit_price_tax_excl": "10.000000", "unit_price_tax_incl": "12.000000"},
            {"id": "102", "product_id": "8", "product_attribute_id": "15", "product_quantity": "1", "product_name": "Mug - Colour : Red",
             "product_reference": "MUG-RED", "product_ean13": "", "original_product_price": "20.000000",
             "unit_price_tax_excl": "20.000000", "unit_price_tax_incl": "24.000000"},
        ]
    },
}

DATA = {
    "customers": [{"id": "5", "email": "Sam@Example.com", "firstname": "Sam", "lastname": "Roy", "is_guest": "0"}],
    "addresses": [
        {"id": "20", "firstname": "Sam", "lastname": "Roy", "company": "", "address1": "1 Rue A", "address2": "", "postcode": "75001",
         "city": "Paris", "id_country": "8", "id_state": "0", "phone": "0102", "phone_mobile": "0607"},
        {"id": "21", "firstname": "Sam", "lastname": "Roy", "company": "", "address1": "9 Rue B", "address2": "2nd floor", "postcode": "69001",
         "city": "Lyon", "id_country": "8", "id_state": "0", "phone": "", "phone_mobile": ""},
    ],
    "countries": [{"id": "8", "iso_code": "FR"}],
    "states": [{"id": "1", "iso_code": "CA"}],
    "currencies": [{"id": "1", "iso_code": "EUR"}],
    "carriers": [{"id": "2", "name": "My carrier"}],
    "order_states": [
        {"id": "1", "paid": "0", "shipped": "0", "delivery": "0", "deleted": "0", "template": "awaiting_payment"},
        {"id": "2", "paid": "1", "shipped": "0", "delivery": "0", "deleted": "0", "template": "payment"},
        {"id": "4", "paid": "1", "shipped": "1", "delivery": "0", "deleted": "0", "template": "shipped"},
        {"id": "5", "paid": "1", "shipped": "1", "delivery": "1", "deleted": "0", "template": "delivered"},
        {"id": "6", "paid": "0", "shipped": "0", "delivery": "0", "deleted": "0", "template": [{"id": "1", "value": "order_canceled"}]},
    ],
    "configurations": [
        {"name": "PS_TIMEZONE", "value": "Europe/Paris"},
        {"name": "PS_LANG_DEFAULT", "value": "1"},
        {"name": "PS_SHOP_NAME", "value": "Demo Shop"},
    ],
    "orders": [ORDER],
    "order_slips": [],
    "order_carriers": [{"id": "31", "id_order": "11", "id_carrier": "2", "id_order_invoice": "0", "weight": "1.0",
                        "shipping_cost_tax_excl": "5", "shipping_cost_tax_incl": "6", "tracking_number": ""}],
    "stock_availables": [
        {"id": "50", "id_product": "7", "id_product_attribute": "0", "quantity": "1", "depends_on_stock": "0", "out_of_stock": "2"},
        {"id": "51", "id_product": "8", "id_product_attribute": "15", "quantity": "1", "depends_on_stock": "0", "out_of_stock": "2"},
        {"id": "52", "id_product": "9", "id_product_attribute": "0", "quantity": "1", "depends_on_stock": "1", "out_of_stock": "2"},
    ],
    "product_options": [{"id": "1", "name": [{"id": "1", "value": "Size"}]}],
    "product_option_values": [
        {"id": "1", "id_attribute_group": "1", "name": "S"},
        {"id": "2", "id_attribute_group": "1", "name": "M"},
    ],
    "combinations": [
        {"id": "15", "id_product": "8", "reference": "MUG-S", "ean13": "", "price": "0.000000",
         "associations": {"product_option_values": [{"id": "1"}]}},
        {"id": "16", "id_product": "8", "reference": "MUG-M", "ean13": "", "price": "2.500000",
         "associations": {"product_option_values": [{"id": "2"}]}},
    ],
    "products": [
        {"id": "7", "reference": "TS-1", "ean13": "", "price": "10.000000", "active": "1", "id_default_image": "123",
         "name": [{"id": "1", "value": "T-shirt"}], "description": "<p>Soft</p>", "date_upd": "2026-10-01 10:00:00"},
        {"id": "8", "reference": "", "ean13": "", "price": "20.000000", "active": "1", "id_default_image": "0",
         "name": "Mug", "description": "", "date_upd": "2026-10-02 10:00:00"},
        {"id": "99", "reference": "OLD", "price": "1", "active": "0", "name": "Hidden", "date_upd": "2026-10-02 10:00:00"},
    ],
}


class FakeShop:
    """Stands in for PrestaShop: answers GETs from DATA like the webservice (id filters, paging) and records writes."""

    def __init__(self, data=None, **overrides):
        self.data = {**copy.deepcopy(DATA), **overrides, **(data or {})}
        self.calls = []

    def __call__(self, method, resource, params=None, xml=None):
        params = params or {}
        self.calls.append((method, resource, dict(params), xml))
        if method != "GET":
            return {}
        rows = self.data.get(resource, [])
        for key, value in params.items():
            match = re.fullmatch(r"filter\[(id|id_order|id_product|id_customer)\]", key)
            if match:
                wanted = set(value.strip("[]").split("|"))
                rows = [r for r in rows if str(r.get(match.group(1))) in wanted]
        offset, _sep, count = str(params.get("limit", "0,1000")).rpartition(",")
        rows = rows[int(offset or 0) : int(offset or 0) + int(count)]
        return {resource: rows} if rows else []

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@tagged("post_install", "-at_install")
class TestPrestaShop(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref("channel_connector_base.group_channel_manager")
        cls.company = cls.env.company
        cls.env.ref("base.EUR").active = True
        cls.env["account.tax"].create(
            {"name": "VAT 20%", "amount": 20.0, "amount_type": "percent", "type_tax_use": "sale", "company_id": cls.company.id}
        )
        bank = cls.env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", cls.company.id)], limit=1)
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.store = cls.env["channel.store"].create(
            {
                "name": "Presta",
                "channel_type": "prestashop",
                "shop_url": "https://shop.example.com",
                "api_key": "KEY",
                "prestashop_timezone": "Europe/Paris",
                "prestashop_language": 1,
                "state": "connected",
                "warehouse_id": cls.warehouse.id,
                "import_orders_from": "2020-01-01 00:00:00",
                "workflow_ids": [(0, 0, {"name": "Paid", "payment_journal_id": bank.id})],
            }
        )
        cls.shirt = cls.env["product.product"].create(
            {"name": "T-shirt", "default_code": "TS-1", "is_storable": True, "type": "consu"}
        )
        cls.mug = cls.env["product.product"].create(
            {"name": "Mug red", "default_code": "MUG-RED", "is_storable": True, "type": "consu"}
        )

    def _lookups(self, shop=None):
        shop = shop or FakeShop()
        with patch(REQUEST, side_effect=shop):
            return self.store._prestashop_lookups(self.store._presta(), [ORDER])

    def _normalized(self, **order_changes):
        return normalize_order({**copy.deepcopy(ORDER), **order_changes}, self._lookups())

    # -- helpers --------------------------------------------------------------------------

    def test_small_helpers(self):
        self.assertEqual(normalize_site("shop.example.com/api/"), "https://shop.example.com")
        self.assertEqual(text([{"id": "1", "value": "a"}, {"id": "2", "value": "b"}], 2), "b")
        self.assertEqual(text("plain"), "plain")
        self.assertEqual(text(None), "")
        self.assertIn("<quantity>5</quantity>", to_xml("stock_available", {"quantity": 5}))
        self.assertIn("&lt;b&gt;", to_xml("order_carrier", {"tracking_number": "<b>"}))

    def test_dates_follow_the_shop_time_zone(self):
        self.assertEqual(to_utc("2026-10-29 12:00:00", "Europe/Paris"), "2026-10-29T11:00:00Z")
        self.assertEqual(to_utc("2026-07-01 12:00:00", "Europe/Paris"), "2026-07-01T10:00:00Z")
        self.assertEqual(to_utc("2026-10-29 12:00:00", "Nowhere/Land"), "2026-10-29T12:00:00Z")
        self.assertIsNone(to_utc("0000-00-00 00:00:00", "UTC"))
        self.assertEqual(to_local(fields.Datetime.to_datetime("2026-10-29 11:00:00"), "Europe/Paris"), "2026-10-29 12:00:00")

    # -- order mapping --------------------------------------------------------------------------

    def test_order_is_mapped_without_tax_with_rates_discounts_and_shipping(self):
        data = self._normalized()
        shirt, mug = data["lines"]
        self.assertEqual((shirt["price"], shirt["discounted_price"]), (12.5, 10.0))
        self.assertEqual(shirt["taxes"], [{"rate": 20.0}])
        self.assertEqual((shirt["variant_id"], shirt["inventory_ref"]), ("p7", "7/0"))
        self.assertEqual((mug["variant_id"], mug["inventory_ref"], mug["sku"]), ("15", "8/15", "MUG-RED"))
        self.assertEqual(data["shipping_lines"], [{"title": "My carrier", "price": 5.0, "taxes": [{"rate": 20.0}]}])
        self.assertEqual(data["discount_lines"], [{"title": "Discount", "price": 2.0, "taxes": [{"rate": 20.0}]}])
        self.assertEqual(data["name"], "ABCDEF")
        self.assertEqual(data["date"], "2026-10-29T11:00:00Z")
        self.assertEqual(data["currency"], "EUR")
        self.assertEqual(data["email"], "Sam@Example.com")
        self.assertEqual(data["billing"]["phone"], "0607")
        self.assertEqual(data["shipping"]["street2"], "2nd floor")
        self.assertEqual(data["shipping"]["country_code"], "FR")
        self.assertEqual(data["gateways"], ["Credit card", "stripe"])
        self.assertTrue(data["paid"])
        self.assertFalse(data["fulfilled"])
        self.assertFalse(data["cancelled"])
        self.assertEqual(data["customer_id"], "5")

    def test_order_states_decide_cancelled_paid_and_shipped(self):
        self.assertTrue(self._normalized(current_state="6", valid="0")["cancelled"])
        waiting = self._normalized(current_state="1", valid="0")
        self.assertFalse(waiting["paid"])
        self.assertTrue(self._normalized(current_state="5")["fulfilled"])

    def test_slip_becomes_a_refund_by_order_row(self):
        slip = {
            "id": "3", "id_order": "11", "total_products_tax_incl": "12.00", "total_shipping_tax_incl": "0.00",
            "associations": {"order_slip_details": [{"id_order_detail": "101", "product_quantity": "1"}]},
        }
        self.assertEqual(
            normalize_slip(slip), {"external_id": "3", "amount": 12.0, "lines": [{"line_id": "101", "qty": 1.0}]}
        )

    # -- importing --------------------------------------------------------------------------------

    def test_order_import_books_discount_shipping_and_payment(self):
        order = self.store._process_order_data(self._normalized())
        self.assertEqual(order.channel_order_name, "ABCDEF")
        self.assertEqual(order.state, "sale")
        shirt_line = order.order_line.filtered(lambda line: line.product_id == self.shirt)
        self.assertAlmostEqual(shirt_line.price_unit, 12.5)
        self.assertAlmostEqual(shirt_line.discount, 20.0)
        self.assertEqual(order.order_line.filtered(lambda line: line.price_unit < 0).product_id, self.store.adjustment_product_id)
        self.assertAlmostEqual(order.amount_total, (20 + 20 + 5 - 2) * 1.2)
        self.assertEqual(order.invoice_ids.payment_state, "paid")
        self.assertEqual(order.partner_shipping_id.city, "Lyon")

    def test_scheduled_import_asks_in_shop_time_and_attaches_slips(self):
        slip = {"id": "3", "id_order": "11", "total_products_tax_incl": "12", "total_shipping_tax_incl": "0",
                "associations": {"order_slip_details": [{"id_order_detail": "101", "product_quantity": "1"}]}}
        shop = FakeShop(order_slips=[slip])
        self.store.import_orders_from = "2026-10-29 11:00:00"
        with patch(REQUEST, side_effect=shop):
            self.assertEqual(self.store._import_orders(), 1)
        orders_call = next(c for c in shop.calls if c[1] == "orders" and "filter[date_upd]" in c[2])
        self.assertEqual(orders_call[2]["filter[date_upd]"], ">[2026-10-29 12:00:00]")
        self.assertEqual(orders_call[2]["date"], "1")
        job = self.store.job_ids.filtered(lambda j: j.job_type == "import_order")
        self.assertEqual(job.get_payload()["order"]["refunds"][0]["external_id"], "3")

    def test_a_slip_on_an_unchanged_order_still_brings_the_order_in(self):
        slip = {"id": "4", "id_order": "11", "total_products_tax_incl": "5", "total_shipping_tax_incl": "0"}
        shop = FakeShop(orders=[ORDER])
        shop.data["orders"] = [ORDER]
        original = shop.__call__

        def only_slips(method, resource, params=None, xml=None):
            if resource == "orders" and "filter[date_upd]" in (params or {}):
                return []  # the order itself did not change
            return original(method, resource, params, xml)

        shop.data["order_slips"] = [slip]
        with patch(REQUEST, side_effect=only_slips):
            orders = list(self.store._fetch_orders(fields.Datetime.to_datetime("2026-10-29 11:00:00")))
        self.assertEqual([o["external_id"] for o in orders], ["11"])
        self.assertEqual(orders[0]["refunds"][0]["external_id"], "4")

    def test_credit_slip_creates_a_credit_note_for_the_returned_quantity(self):
        order = self.store._process_order_data(self._normalized())
        refund = {"external_id": "3", "amount": 12.0, "lines": [{"line_id": "101", "qty": 1.0}]}
        self.store._process_refund(order, refund)
        credit = order.invoice_ids.filtered(lambda m: m.move_type == "out_refund")
        self.assertEqual(len(credit), 1)
        self.assertAlmostEqual(credit.amount_total, 10.0 * 1.2)
        self.store._process_refund(order, refund)  # the same slip twice changes nothing
        self.assertEqual(len(order.invoice_ids.filtered(lambda m: m.move_type == "out_refund")), 1)

    def test_single_order_fetch_for_retries(self):
        with patch(REQUEST, side_effect=FakeShop()):
            data = self.store._fetch_order("11")
            self.assertIsNone(self.store._fetch_order("999"))
        self.assertEqual(data["external_id"], "11")

    # -- connection ---------------------------------------------------------------------------------

    def test_connection_reads_shop_name_time_zone_and_language(self):
        self.store.write({"prestashop_timezone": False, "prestashop_language": 0})
        with patch(REQUEST, side_effect=FakeShop()):
            name = self.store._test_connection()
            self.store._test_connection()
        self.assertEqual(name, "Demo Shop")
        self.assertEqual(self.store.prestashop_timezone, "Europe/Paris")
        self.assertEqual(self.store.prestashop_language, 1)
        self.assertEqual(len(self.store.location_ids), 1)
        self.assertEqual(self.store.location_ids.warehouse_id, self.warehouse)

    def test_connection_works_when_the_key_cannot_read_configurations(self):
        shop = FakeShop()

        def fake(method, resource, params=None, xml=None):
            if resource == "configurations":
                raise ChannelError("not allowed")
            return shop(method, resource, params, xml)

        self.store.prestashop_timezone = False
        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.store._test_connection(), "https://shop.example.com")
        self.assertEqual(self.store.prestashop_timezone, "UTC")

    def test_prestashop_has_no_webhooks(self):
        self.assertFalse(self.store.supports_webhooks)

    # -- products -------------------------------------------------------------------------------------

    def test_products_with_and_without_combinations(self):
        with patch(REQUEST, side_effect=FakeShop()):
            products = list(self.store._fetch_products())
        self.assertEqual([p["external_id"] for p in products], ["7", "8"])  # the inactive one is left out
        tshirt, mug = products
        self.assertEqual(tshirt["title"], "T-shirt")
        self.assertEqual(tshirt["variants"][0]["external_id"], "p7")
        self.assertEqual(tshirt["image_url"], "https://shop.example.com/img/p/1/2/3/123.jpg")
        self.assertIsNone(mug["image_url"])
        self.assertEqual(mug["options"], ["Size"])
        self.assertEqual([(v["external_id"], v["price"], v["options"]) for v in mug["variants"]], [("15", 20.0, ["S"]), ("16", 22.5, ["M"])])

    def test_product_import_links_by_reference_and_creates_variants(self):
        with patch(REQUEST, side_effect=FakeShop()), patch.object(type(self.store), "_download_image", return_value=False):
            self.assertEqual(self.store._import_products(), 2)
            self.env["channel.job"]._run_due_jobs()
        mapped = {m.external_id: m for m in self.store.mapping_ids.filtered(lambda m: m.res_model == "product.product")}
        self.assertEqual(mapped["p7"].res_id, self.shirt.id)
        self.assertEqual(mapped["p7"].external_ref, "7/0")
        self.assertEqual(mapped["15"].external_ref, "8/15")
        self.assertEqual(len(self.store.mapping_ids.filtered(lambda m: m.res_model == "product.template")), 2)

    # -- stock ------------------------------------------------------------------------------------------

    def test_stock_is_written_with_a_put_on_the_stock_record(self):
        self.env["stock.quant"]._update_available_quantity(self.shirt, self.warehouse.lot_stock_id, 6)
        self.store._set_mapping(self.shirt, "p7", "7/0")
        self.store._sync_locations([{"external_id": "0", "name": "Stock"}])
        shop = FakeShop()
        with patch(REQUEST, side_effect=shop):
            self.assertEqual(self.store._push_stock(), 1)
            self.env["channel.job"]._run_due_jobs()
        (method, resource, _params, xml), = shop.writes()
        self.assertEqual((method, resource), ("PUT", "stock_availables/50"))
        self.assertIn("<quantity>6</quantity>", xml)
        self.assertIn("<id_product>7</id_product>", xml)
        self.assertIn("<out_of_stock>2</out_of_stock>", xml)

    def test_stock_the_shop_counts_itself_or_that_vanished_is_refused_by_name(self):
        gone = self.env["product.product"].create({"name": "Gone", "is_storable": True, "type": "consu"})
        advanced = self.env["product.product"].create({"name": "Advanced", "is_storable": True, "type": "consu"})
        for product, ref in ((self.shirt, "7/0"), (gone, "55/0"), (advanced, "9/0")):
            self.env["stock.quant"]._update_available_quantity(product, self.warehouse.lot_stock_id, 3)
            self.store._set_mapping(product, f"x{ref}", ref)
        self.store._sync_locations([{"external_id": "0", "name": "Stock"}])
        shop = FakeShop()
        with patch(REQUEST, side_effect=shop):
            self.store._push_stock()
            self.env["channel.job"]._run_due_jobs()
        self.assertEqual(len(shop.writes()), 1)
        problem = self.store.job_ids.filtered(lambda j: j.state == "failed")
        self.assertIn("Gone", problem.error)
        self.assertIn("Advanced", problem.error)
        self.assertNotIn("T-shirt", problem.error)

    # -- shipments ----------------------------------------------------------------------------------------

    def _ship(self, order, tracking="TRK1"):
        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.carrier_tracking_ref = tracking
        picking.with_context(skip_sms=True, skip_backorder=True).button_validate()
        return self.store.job_ids.filtered(lambda j: j.job_type == "push_fulfillment")

    def test_delivery_sets_tracking_and_moves_the_order_to_shipped(self):
        order = self.store._process_order_data(self._normalized())
        job = self._ship(order)
        shop = FakeShop()
        with patch(REQUEST, side_effect=shop):
            job._run()
        self.assertEqual(job.state, "done", job.error)
        carrier, history = shop.writes()
        self.assertEqual((carrier[0], carrier[1]), ("PUT", "order_carriers/31"))
        self.assertIn("<tracking_number>TRK1</tracking_number>", carrier[3])
        self.assertIn("<id_carrier>2</id_carrier>", carrier[3])
        self.assertEqual((history[0], history[1]), ("POST", "order_histories"))
        self.assertIn("<id_order>11</id_order>", history[3])
        self.assertIn("<id_order_state>4</id_order_state>", history[3])

    def test_order_already_shipped_on_the_shop_gets_no_second_status_change(self):
        order = self.store._process_order_data(self._normalized())
        job = self._ship(order)
        shipped_order = {**ORDER, "current_state": "4"}
        shop = FakeShop(orders=[shipped_order])
        with patch(REQUEST, side_effect=shop):
            job._run()
        self.assertEqual([w[1] for w in shop.writes()], ["order_carriers/31"])

    # -- API client ---------------------------------------------------------------------------------------

    def _http(self, status, body=None, content=b"{}"):
        response = MagicMock(status_code=status)
        response.json.return_value = body if body is not None else {}
        response.text = str(body or "")
        response.content = content
        return response

    def test_wrong_key_unsafe_resource_and_missing_webservice_say_what_to_do(self):
        with patch(HTTP, return_value=self._http(401)):
            with self.assertRaises(ChannelAuthError) as caught:
                PrestaClient(self.store).request("GET", "orders")
        self.assertIn("Webservice", str(caught.exception))
        with patch(HTTP, return_value=self._http(405)):
            with self.assertRaises(ChannelError) as caught:
                PrestaClient(self.store).request("GET", "order_slips")
        self.assertNotIsInstance(caught.exception, ChannelAuthError)
        self.assertIn("order_slips", str(caught.exception))
        with patch(HTTP, return_value=self._http(404)):
            with self.assertRaisesRegex(ChannelError, "switch it on|Switch it on"):
                PrestaClient(self.store).request("GET", "orders")

    def test_requests_use_the_key_as_user_and_ask_for_json(self):
        with patch(HTTP, return_value=self._http(200, {"orders": []})) as http:
            PrestaClient(self.store).request("GET", "orders", {"display": "full"})
        kwargs = http.call_args.kwargs
        self.assertEqual(kwargs["auth"], ("KEY", ""))
        self.assertEqual(kwargs["params"]["output_format"], "JSON")
        self.assertEqual(http.call_args.args[1], "https://shop.example.com/api/orders")

    def test_answers_that_are_not_prestashop_are_explained(self):
        bad = self._http(200, content=b"<html>")
        bad.json.side_effect = ValueError
        with patch(HTTP, return_value=bad):
            with self.assertRaisesRegex(ChannelError, "did not answer like PrestaShop"):
                PrestaClient(self.store).request("GET", "orders")

    def test_pages_are_followed_until_a_short_page(self):
        pages = [{"orders": [{"id": str(i)} for i in range(100)]}, {"orders": [{"id": "100"}]}]
        with patch(REQUEST, side_effect=pages) as request:
            rows = list(PrestaClient(self.store).list("orders"))
        self.assertEqual(len(rows), 101)
        self.assertEqual(request.call_args_list[1].args[2]["limit"], "100,100")

