import json

from odoo import _, fields, models

CHUNK = 100


class ChannelStore(models.Model):
    _inherit = "channel.store"

    def _stock_quantity(self, products, warehouse):
        """{product id: quantity to publish} for one warehouse."""
        quantities = products.with_context(warehouse_id=warehouse.id).read(
            [self.stock_quantity_type]
        )
        return {
            row["id"]: max(0, int(row[self.stock_quantity_type]) - self.stock_safety)
            for row in quantities
        }

    def _sync_locations(self, locations):
        """Create the channel locations we do not know yet: [{"external_id", "name"}].

        One location gets the store warehouse; mapping several to it would count the stock twice."""
        self.ensure_one()
        existing = {location.external_id: location for location in self.location_ids}
        first_free = not self.location_ids.filtered("warehouse_id")
        for data in locations:
            if data["external_id"] in existing:
                existing[data["external_id"]].name = data["name"]
                continue
            self.env["channel.location"].create(
                {
                    "store_id": self.id,
                    "name": data["name"],
                    "external_id": data["external_id"],
                    "warehouse_id": self.warehouse_id.id if first_free else False,
                }
            )
            first_free = False

    def _push_stock(self):
        """Queue stock updates for the variants whose quantity changed. Returns how many."""
        self.ensure_one()
        locations = self.location_ids.filtered("warehouse_id")
        mappings = self.env["channel.mapping"].search(
            [
                ("store_id", "=", self.id),
                ("res_model", "=", "product.product"),
                ("push_stock", "=", True),
                ("external_ref", "!=", False),
            ]
        )
        products = self.env["product.product"].browse(mappings.mapped("res_id")).exists()
        products = products.filtered(lambda p: p.is_storable)
        items = []
        for location in locations:
            quantities = self._stock_quantity(products, location.warehouse_id)
            for mapping in mappings:
                if mapping.res_id not in quantities:
                    continue
                qty = quantities[mapping.res_id]
                last = mapping.get_last_stock().get(location.external_id)
                if last == qty:
                    continue
                items.append(
                    {
                        "mapping_id": mapping.id,
                        "inventory_ref": mapping.external_ref,
                        "location": location.external_id,
                        "qty": qty,
                    }
                )
        for start in range(0, len(items), CHUNK):
            chunk = items[start : start + CHUNK]
            self._queue_job(
                "push_stock",
                _("Send %s stock level(s)", len(chunk)),
                {"items": chunk},
            )
        self.last_stock_sync = fields.Datetime.now()
        return len(items)

    def _job_push_stock(self, job):
        items = job.get_payload()["items"]
        self._push_stock_levels(items)
        for item in items:
            mapping = self.env["channel.mapping"].browse(item["mapping_id"]).exists()
            if not mapping:
                continue
            last = mapping.get_last_stock()
            last[item["location"]] = item["qty"]
            mapping.last_stock = json.dumps(last)

    # -- fulfillment ------------------------------------------------------------------

    def _job_push_fulfillment(self, job):
        picking = self.env["stock.picking"].browse(job.get_payload()["picking_id"]).exists()
        if not picking:
            return None
        self._push_fulfillment(picking)
        picking.sale_id.message_post(body=_("Shipment %s sent to the sales channel.", picking.name))
        return picking
