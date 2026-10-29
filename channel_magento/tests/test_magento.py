import copy
from unittest.mock import MagicMock, patch

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError
from odoo.tests import tagged

from ..models.channel_store import normalize_credit_memo, normalize_order, normalize_product, sku_path
from ..models.magento_client import MagentoClient, criteria, normalize_site

REQUEST = "odoo.addons.channel_magento.models.magento_client.MagentoClient.request"
HTTP = "odoo.addons.channel_magento.models.magento_client.requests.request"

ADDRESS = {
    "firstname": "Omar",
    "lastname": "Saleh",
    "company": "",
    "street": ["5 Souk St", "Floor 2"],
    "city": "Dubai",
    "region_code": "",
    "region": "",
    "postcode": "00000",
    "country_id": "AE",
    "telephone": "+971501111111",
}
ORDER = {
    "entity_id": 11,
    "increment_id": "000000011",
    "state": "processing",
    "status": "processing",
    "created_at": "2026-10-29 10:00:00",
    "updated_at": "2026-10-29 10:05:00",
    "customer_email": "omar@example.com",
    "customer_id": 5,
    "customer_is_guest": False,
    "order_currency_code": "EUR",
    "total_invoiced": 61.2,
    "shipping_amount": 5.0,
    "shipping_tax_amount": 1.0,
    "shipping_discount_amount": 0,
    "shipping_description": "Flat Rate - Fixed",
    "billing_address": ADDRESS,
    "extension_attributes": {
        "shipping_assignments": [{"shipping": {"address": {**ADDRESS, "street": ["9 Beach Rd"], "city": "Abu Dhabi"}}}]
    },
    "payment": {"method": "stripe_payments", "additional_information": ["Credit Card"]},
    "items": [
        {
            "item_id": 101,
            "product_id": 30,
            "sku": "SHIRT",
            "name": "Shirt",
            "product_type": "configurable",
            "qty_ordered": 2,
            "qty_canceled": 0,
            "price": 20.0,
            "row_total": 40.0,
            "tax_percent": 20.0,
            "discount_amount": 4.0,
        },
        {
            "item_id": 102,
            "parent_item_id": 101,
            "product_id": 31,
            "sku": "SHIRT-RED-M",
            "name": "Shirt",
            "qty_ordered": 2,
            "price": 0,
        },
        {
            "item_id": 103,
            "product_id": 8,
            "sku": "MUG-1",
            "name": "Mug",
            "product_type": "simple",
            "qty_ordered": 1,
            "qty_canceled": 0,
            "price": 10.0,
            "row_total": 10.0,
            "tax_percent": 20.0,
            "discount_amount": 0,
        },
    ],
}
MEMO = {
    "entity_id": 7,
    "increment_id": "000000007",
    "order_id": 11,
    "grand_total": 21.6,
    "updated_at": "2026-10-30 08:00:00",
    "items": [{"order_item_id": 101, "qty": 1}, {"order_item_id": 102, "qty": 1}, {"order_item_id": 103, "qty": 0}],
}
PRODUCTS = [
    {
        "id": 8,
        "sku": "MUG-1",
        "name": "Mug",
        "price": 10.0,
        "type_id": "simple",
        "status": 1,
        "updated_at": "2026-10-01 10:00:00",
        "custom_attributes": [
            {"attribute_code": "image", "value": "/m/u/mug.jpg"},
            {"attribute_code": "description", "value": "<p>Mug</p>"},
        ],
    },
    {
        "id": 30,
        "sku": "SHIRT",
        "name": "Shirt",
        "price": 20.0,
        "type_id": "configurable",
        "status": 1,
        "custom_attributes": [],
        "extension_attributes": {
            "configurable_product_links": [31, 32],
            "configurable_product_options": [{"attribute_id": "93", "label": "Size", "position": 0}],
        },
    },
    {
        "id": 31,
        "sku": "SHIRT-RED-M",
        "name": "Shirt M",
        "price": 20.0,
        "type_id": "simple",
        "status": 1,
        "custom_attributes": [{"attribute_code": "size", "value": "167"}],
    },
    {
        "id": 32,
        "sku": "SHIRT-RED-L",
        "name": "Shirt L",
        "price": 22.0,
        "type_id": "simple",
        "status": 1,
        "custom_attributes": [{"attribute_code": "size", "value": "168"}],
    },
    {"id": 40, "sku": "OLD", "name": "Old", "price": 1, "type_id": "simple", "status": 2, "custom_attributes": []},
]
SOURCES = {
    "items": [
        {"source_code": "default", "name": "Default Source", "enabled": True},
        {"source_code": "dubai", "name": "Dubai WH", "enabled": True},
    ]
}


class FakeShop:
    """Stands in for Magento: answers searchCriteria lists from DATA and records writes."""

    def __init__(self, **data):
        self.data = {
            "orders": [ORDER],
            "creditmemos": [],
            "products": PRODUCTS,
            "inventory/sources": SOURCES,
            "store/storeConfigs": [{"base_currency_code": "EUR", "base_url": "https://shop.example.com/"}],
            **copy.deepcopy(data),
        }
        self.calls = []

    def __call__(self, method, path, params=None, json=None):
        params = params or {}
        self.calls.append((method, path, dict(params), json))
        if method != "GET":
            return {}
        if path.startswith("orders/"):
            return next(o for o in self.data["orders"] if str(o["entity_id"]) == path.split("/")[1])
        if path.startswith("configurable-products/"):
            return [p for p in PRODUCTS if p["id"] in (31, 32)]
        if path == "products/attributes":
            return {
                "items": [
                    {
                        "attribute_code": "size",
                        "options": [{"label": "M", "value": "167"}, {"label": "L", "value": "168"}],
                    }
                ]
            }
        rows = self.data.get(path)
        if rows is None or not isinstance(rows, (list, dict)):
            return {}
        if isinstance(rows, dict):
            if path == "inventory/sources" and rows.get("raise"):
                raise ChannelError("no MSI")
            return rows
        if path == "store/storeConfigs":
            return rows
        for key, value in params.items():
            if key.endswith("[field]") and value == "order_id":
                wanted = set(params[key.replace("[field]", "[value]")].split(","))
                rows = [r for r in rows if str(r["order_id"]) in wanted]
        size = int(params.get("searchCriteria[pageSize]", 100))
        page = int(params.get("searchCriteria[currentPage]", 1))
        return {"items": rows[(page - 1) * size : page * size]}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@tagged("post_install", "-at_install")
class TestMagento(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref("channel_connector_base.group_channel_manager")
        cls.company = cls.env.company
        cls.env.ref("base.EUR").active = True
        cls.env["account.tax"].create(
            {
                "name": "VAT 20%",
                "amount": 20.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": cls.company.id,
            }
        )
        bank = cls.env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", cls.company.id)], limit=1)
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.store = cls.env["channel.store"].create(
            {
                "name": "Magento",
                "channel_type": "magento",
                "shop_url": "https://shop.example.com",
                "access_token": "TOKEN",
                "state": "connected",
                "magento_msi": True,
                "warehouse_id": cls.warehouse.id,
                "import_orders_from": "2020-01-01 00:00:00",
                "workflow_ids": [(0, 0, {"name": "Paid", "payment_journal_id": bank.id})],
            }
        )
        cls.shirt = cls.env["product.product"].create(
            {"name": "Shirt M", "default_code": "SHIRT-RED-M", "is_storable": True, "type": "consu"}
        )
        cls.mug = cls.env["product.product"].create(
            {"name": "Mug", "default_code": "MUG-1", "is_storable": True, "type": "consu"}
        )

    # -- helpers --------------------------------------------------------------------------

    def test_small_helpers(self):
        self.assertEqual(normalize_site("shop.example.com/rest/V1"), "https://shop.example.com")
        self.assertEqual(
            normalize_site("https://shop.example.com/index.php/rest/default/V1"), "https://shop.example.com"
        )
        self.assertEqual(sku_path("A/B C"), "A%252FB%20C")
        params = criteria([("updated_at", "2026-10-29 10:00:00", "gteq")], page=2)
        self.assertEqual(params["searchCriteria[filter_groups][0][filters][0][condition_type]"], "gteq")
        self.assertEqual(params["searchCriteria[currentPage]"], 2)

    # -- order mapping ---------------------------------------------------------------------

    def test_order_is_mapped_with_prices_without_tax_and_the_real_sku_of_configurables(self):
        data = normalize_order(ORDER)
        shirt, mug = data["lines"]
        self.assertEqual(
            (shirt["external_id"], shirt["sku"], shirt["variant_id"], shirt["inventory_ref"]),
            ("101", "SHIRT-RED-M", "31", "SHIRT-RED-M"),
        )
        self.assertEqual((shirt["qty"], shirt["price"], shirt["discounted_price"]), (2.0, 20.0, 18.0))
        self.assertEqual(shirt["taxes"], [{"rate": 20.0}])
        self.assertEqual(mug["variant_id"], "8")
        self.assertEqual(
            data["shipping_lines"], [{"title": "Flat Rate - Fixed", "price": 5.0, "taxes": [{"rate": 20.0}]}]
        )
        self.assertEqual(data["name"], "000000011")
        self.assertEqual(data["customer_id"], "5")
        self.assertEqual(data["billing"]["street2"], "Floor 2")
        self.assertEqual(data["shipping"]["city"], "Abu Dhabi")
        self.assertEqual(data["gateways"], ["stripe_payments", "Credit Card"])
        self.assertTrue(data["paid"])
        self.assertFalse(data["cancelled"])

    def test_states_guests_and_cancelled_quantities(self):
        order = copy.deepcopy(ORDER)
        order.update(state="canceled", customer_is_guest=True, total_invoiced=0)
        data = normalize_order(order)
        self.assertTrue(data["cancelled"])
        self.assertIsNone(data["customer_id"])
        self.assertFalse(data["paid"])
        order["items"][2]["qty_canceled"] = 1
        self.assertEqual(len(normalize_order(order)["lines"]), 1)
        self.assertTrue(normalize_order({**ORDER, "state": "complete"})["fulfilled"])

    def test_credit_memo_keeps_only_the_top_level_rows_of_the_order(self):
        refund = normalize_credit_memo(MEMO, {"101", "103"})
        self.assertEqual(
            refund, {"external_id": "000000007", "amount": 21.6, "lines": [{"line_id": "101", "qty": 1.0}]}
        )

    # -- importing --------------------------------------------------------------------------

    def test_order_import_books_the_configurable_item_and_totals(self):
        order = self.store._process_order_data(normalize_order(ORDER))
        self.assertEqual(order.channel_order_name, "000000011")
        self.assertAlmostEqual(order.amount_total, (36 + 10 + 5) * 1.2)
        shirt_line = order.order_line.filtered(lambda line: line.product_id == self.shirt)
        self.assertEqual((shirt_line.channel_line_id, shirt_line.product_uom_qty), ("101", 2.0))
        self.assertAlmostEqual(shirt_line.discount, 10.0)
        self.assertEqual(order.invoice_ids.payment_state, "paid")
        self.assertEqual(order.partner_shipping_id.city, "Abu Dhabi")

    def test_cash_on_delivery_stays_a_quotation(self):
        order = copy.deepcopy(ORDER)
        order.update(entity_id=12, increment_id="000000012", total_invoiced=0)
        self.assertEqual(self.store._process_order_data(normalize_order(order)).state, "draft")

    def test_scheduled_import_attaches_credit_memos_and_follows_the_pages(self):
        many = [{**ORDER, "entity_id": 100 + i, "increment_id": f"9{i}"} for i in range(100)]
        shop = FakeShop(orders=[ORDER, *many], creditmemos=[MEMO])
        self.store.import_orders_from = "2026-10-29 00:00:00"
        with patch(REQUEST, side_effect=shop):
            self.assertEqual(self.store._import_orders(), 101)
        first = shop.calls[0]
        self.assertEqual(first[1], "orders")
        self.assertEqual(first[2]["searchCriteria[filter_groups][0][filters][0][value]"], "2026-10-29 00:00:00")
        self.assertEqual(first[2]["searchCriteria[filter_groups][0][filters][0][condition_type]"], "gteq")
        job = self.store.job_ids.filtered(lambda j: j.external_ref == "000000011")
        self.assertEqual(job.get_payload()["order"]["refunds"][0]["lines"], [{"line_id": "101", "qty": 1.0}])

    def test_a_credit_memo_on_an_unchanged_order_still_brings_the_order_in(self):
        shop = FakeShop(orders=[ORDER], creditmemos=[MEMO])
        original = shop.__call__

        def memo_only(method, path, params=None, json=None):
            if path == "orders":
                return {"items": []}  # nothing changed on the order itself
            return original(method, path, params, json)

        with patch(REQUEST, side_effect=memo_only):
            orders = list(self.store._fetch_orders(self.env["res.users"].browse(1).create_date))
        self.assertEqual([o["external_id"] for o in orders], ["11"])
        self.assertEqual(orders[0]["refunds"][0]["external_id"], "000000007")

    def test_credit_memo_becomes_a_credit_note_for_the_returned_quantity(self):
        order = self.store._process_order_data(normalize_order(ORDER))
        self.store._process_refund(order, normalize_credit_memo(MEMO, {"101", "103"}))
        credit = order.invoice_ids.filtered(lambda m: m.move_type == "out_refund")
        self.assertAlmostEqual(credit.amount_total, 18.0 * 1.2)

    def test_single_order_fetch_for_retries(self):
        with patch(REQUEST, side_effect=FakeShop(creditmemos=[MEMO])):
            data = self.store._fetch_order("11")
        self.assertEqual(data["external_id"], "11")
        self.assertEqual(len(data["refunds"]), 1)

    # -- connection ---------------------------------------------------------------------------

    def test_connection_creates_one_location_per_source(self):
        self.store.magento_msi = False
        with patch(REQUEST, side_effect=FakeShop()):
            name = self.store._test_connection()
            self.store._test_connection()
        self.assertIn("EUR", name)
        self.assertTrue(self.store.magento_msi)
        self.assertEqual(self.store.location_ids.mapped("external_id"), ["default", "dubai"])
        self.assertEqual(self.store.location_ids.filtered("warehouse_id").mapped("external_id"), ["default"])

    def test_shops_without_multi_source_use_one_stock_location(self):
        shop = FakeShop()
        original = shop.__call__

        def no_msi(method, path, params=None, json=None):
            if path == "inventory/sources":
                raise ChannelError("Request does not match any route")
            return original(method, path, params, json)

        with patch(REQUEST, side_effect=no_msi):
            self.store._test_connection()
        self.assertFalse(self.store.magento_msi)
        self.assertEqual(self.store.location_ids.mapped("external_id"), ["default"])

    def test_magento_has_no_webhooks(self):
        self.assertFalse(self.store.supports_webhooks)

    # -- products -------------------------------------------------------------------------------

    def test_configurable_products_carry_their_children_as_variants(self):
        with patch(REQUEST, side_effect=FakeShop()):
            products = list(self.store._fetch_products())
        self.assertEqual(
            [p["external_id"] for p in products], ["8", "30"]
        )  # children and disabled products are not listed alone
        mug, shirt = products
        self.assertEqual(mug["image_url"], "https://shop.example.com/media/catalog/product/m/u/mug.jpg")
        self.assertEqual(mug["variants"][0]["inventory_ref"], "MUG-1")
        self.assertEqual(shirt["options"], ["Size"])
        self.assertEqual(
            [(v["external_id"], v["sku"], v["options"], v["price"]) for v in shirt["variants"]],
            [("31", "SHIRT-RED-M", ["M"], 20.0), ("32", "SHIRT-RED-L", ["L"], 22.0)],
        )

    def test_simple_product_helper(self):
        data = normalize_product(PRODUCTS[0], "https://shop.example.com")
        self.assertEqual(data["variants"][0]["external_id"], "8")

    def test_product_import_links_by_sku(self):
        with (
            patch(REQUEST, side_effect=FakeShop()),
            patch.object(type(self.store), "_download_image", return_value=False),
        ):
            self.assertEqual(self.store._import_products(), 2)
            self.env["channel.job"]._run_due_jobs()
        mapped = {m.external_id: m for m in self.store.mapping_ids.filtered(lambda m: m.res_model == "product.product")}
        self.assertEqual(mapped["8"].res_id, self.mug.id)
        self.assertEqual(mapped["31"].res_id, self.shirt.id)
        self.assertEqual(mapped["31"].external_ref, "SHIRT-RED-M")

    # -- stock -----------------------------------------------------------------------------------

    def _stock_setup(self):
        self.env["stock.quant"]._update_available_quantity(self.mug, self.warehouse.lot_stock_id, 6)
        self.store._set_mapping(self.mug, "8", "MUG-1")
        self.store._set_mapping(self.shirt, "31", "SHIRT-RED-M")
        self.store._sync_locations([{"external_id": "default", "name": "Default Source"}])

    def test_stock_goes_to_the_source_in_bulk(self):
        self._stock_setup()
        shop = FakeShop()
        with patch(REQUEST, side_effect=shop):
            self.assertEqual(self.store._push_stock(), 2)
            self.env["channel.job"]._run_due_jobs()
        ((method, path, _params, body),) = shop.writes()
        self.assertEqual((method, path), ("POST", "inventory/source-items"))
        self.assertEqual(
            sorted((i["sku"], i["source_code"], i["quantity"], i["status"]) for i in body["sourceItems"]),
            [("MUG-1", "default", 6, 1), ("SHIRT-RED-M", "default", 0, 0)],
        )

    def test_an_unknown_sku_is_found_by_retrying_one_by_one(self):
        self._stock_setup()

        def fake(method, path, params=None, json=None):
            skus = {i["sku"] for i in json["sourceItems"]}
            if "SHIRT-RED-M" in skus:
                raise ChannelError("Could not save Source Item")
            return []

        with patch(REQUEST, side_effect=fake):
            self.store._push_stock()
            self.env["channel.job"]._run_due_jobs()
        problem = self.store.job_ids.filtered(lambda j: j.state == "failed")
        self.assertIn("Shirt M", problem.error)
        self.assertNotIn("Mug", problem.error)
        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.store._push_stock(), 1)

    def test_shops_without_multi_source_get_a_stock_item_update(self):
        self.store.magento_msi = False
        self._stock_setup()
        shop = FakeShop()
        with patch(REQUEST, side_effect=shop):
            self.store._push_stock()
            self.env["channel.job"]._run_due_jobs()
        paths = {w[1]: w[3] for w in shop.writes()}
        self.assertEqual(paths["products/MUG-1/stockItems/1"], {"stockItem": {"qty": 6, "is_in_stock": True}})
        self.assertEqual(paths["products/SHIRT-RED-M/stockItems/1"], {"stockItem": {"qty": 0, "is_in_stock": False}})

    # -- shipments -------------------------------------------------------------------------------

    def _ship(self, order, tracking="TRK7"):
        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.carrier_tracking_ref = tracking
        picking.with_context(skip_sms=True, skip_backorder=True).button_validate()
        return self.store.job_ids.filtered(lambda j: j.job_type == "push_fulfillment")

    def test_delivery_creates_a_shipment_with_tracking(self):
        order = self.store._process_order_data(normalize_order(ORDER))
        job = self._ship(order)
        calls = []
        with patch(
            REQUEST, side_effect=lambda method, path, params=None, json=None: calls.append((method, path, json)) or "5"
        ):
            job._run()
        self.assertEqual(job.state, "done", job.error)
        method, path, body = calls[0]
        self.assertEqual((method, path), ("POST", "order/11/ship"))
        self.assertEqual(sorted((i["order_item_id"], i["qty"]) for i in body["items"]), [(101, 2), (103, 1)])
        self.assertEqual(body["tracks"][0]["track_number"], "TRK7")
        self.assertEqual(body["tracks"][0]["carrier_code"], "custom")
        self.assertTrue(body["notify"])

    def test_shipment_without_tracking_has_no_tracks(self):
        order = self.store._process_order_data(normalize_order(ORDER))
        job = self._ship(order, tracking=False)
        calls = []
        with patch(REQUEST, side_effect=lambda method, path, params=None, json=None: calls.append(json) or "5"):
            job._run()
        self.assertNotIn("tracks", calls[0])

    # -- API client ------------------------------------------------------------------------------

    def _http(self, status, body=None, content=b"{}"):
        response = MagicMock(status_code=status)
        response.json.return_value = body if body is not None else {}
        response.text = str(body or "")
        response.content = content
        return response

    def test_requests_use_the_bearer_token_and_the_store_view(self):
        self.store.magento_store_code = "uk"
        with patch(HTTP, return_value=self._http(200, {"items": []})) as http:
            MagentoClient(self.store).request("GET", "orders")
        self.assertEqual(http.call_args.args[1], "https://shop.example.com/rest/uk/V1/orders")
        self.assertEqual(http.call_args.kwargs["headers"]["Authorization"], "Bearer TOKEN")

    def test_bad_token_and_missing_permissions_are_told_apart(self):
        with patch(
            HTTP,
            return_value=self._http(
                401,
                {
                    "message": "The consumer isn't authorized to access %resources.",
                    "parameters": {"resources": "Magento_Sales::sales"},
                },
            ),
        ):
            with self.assertRaises(ChannelError) as caught:
                MagentoClient(self.store).request("GET", "orders")
        self.assertNotIsInstance(caught.exception, ChannelAuthError)
        self.assertIn("Sales", str(caught.exception))
        with patch(HTTP, return_value=self._http(401, {"message": "Consumer is not authorized."})):
            with self.assertRaisesRegex(ChannelAuthError, "Access Token"):
                MagentoClient(self.store).request("GET", "orders")

    def test_unknown_route_and_html_answers_are_explained(self):
        with patch(HTTP, return_value=self._http(404, {"message": "Request does not match any route."})):
            with self.assertRaisesRegex(ChannelError, "index.php"):
                MagentoClient(self.store).request("GET", "orders")
        html = self._http(200, content=b"<html>")
        html.json.side_effect = ValueError
        with patch(HTTP, return_value=html):
            with self.assertRaisesRegex(ChannelError, "did not answer like Magento"):
                MagentoClient(self.store).request("GET", "orders")

    def test_error_messages_get_their_parameters_filled_in(self):
        body = {"message": "Product %1 not found in %2", "parameters": ["ABC", "default"]}
        with patch(HTTP, return_value=self._http(400, body)):
            with self.assertRaisesRegex(ChannelError, "Product ABC not found in default"):
                MagentoClient(self.store).request("POST", "x")

    def test_credentials_are_required(self):
        with self.assertRaisesRegex(ChannelError, "Integration access token"):
            MagentoClient(self.store.copy({"name": "Empty", "access_token": False}))
