import base64
import logging

import requests

from odoo import Command, _, models
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)


class ChannelStore(models.Model):
    _inherit = "channel.store"

    # -- import ------------------------------------------------------------------------

    def _import_products(self):
        self.ensure_one()
        count = 0
        for data in self._fetch_products():
            job = self._queue_job(
                "import_product",
                _("Import product %s", data.get("title")),
                {"product": data},
                dedupe_key=f"product:{data['external_id']}:{data.get('updated_at')}",
                external_ref=data["external_id"],
            )
            count += bool(job)
        return count

    def _job_import_product(self, job):
        return self._process_product_data(job.get_payload()["product"])

    def _find_product(self, line):
        """Find the Odoo variant for an order line: mapping, then SKU, then barcode."""
        self.ensure_one()
        Product = self.env["product.product"].with_context(active_test=False)
        product = self.env["product.product"]
        if line.get("variant_id"):
            product = self._get_mapped("product.product", line["variant_id"])
        sku = (line.get("sku") or "").strip()
        if not product and sku:
            product = Product.search([("default_code", "=", sku)], limit=1)
        barcode = (line.get("barcode") or "").strip()
        if not product and barcode:
            product = Product.search([("barcode", "=", barcode)], limit=1)
        if product and line.get("variant_id"):
            self._set_mapping(product, line["variant_id"], line.get("inventory_ref"))
        return product

    def _create_product_from_line(self, line):
        product = self.env["product.product"].create(
            {
                "name": line.get("name") or line.get("sku") or _("Channel product"),
                "default_code": line.get("sku") or False,
                "barcode": line.get("barcode") or False,
                "list_price": float(line.get("price") or 0.0),
                "type": "consu",
                "is_storable": True,
                "company_id": self.company_id.id,
            }
        )
        if line.get("variant_id"):
            self._set_mapping(product, line["variant_id"], line.get("inventory_ref"))
        return product

    def _process_product_data(self, data):
        """Create or link an Odoo product from a normalised channel product."""
        self.ensure_one()
        variants = data.get("variants") or []
        template = self._get_mapped("product.template", data["external_id"])
        if not template:
            for variant in variants:
                product = self._find_product(
                    {"sku": variant.get("sku"), "barcode": variant.get("barcode")}
                )
                if product:
                    template = product.product_tmpl_id
                    break
        if not template:
            template = self._create_template(data)
        self._set_mapping(template, data["external_id"])
        self._map_variants(template, variants)
        return template

    def _create_template(self, data):
        variants = data.get("variants") or []
        option_names = [o for o in (data.get("options") or []) if o]
        single = len(variants) <= 1
        vals = {
            "name": data.get("title") or _("Channel product"),
            "type": "consu",
            "is_storable": True,
            "sale_ok": True,
            "company_id": self.company_id.id,
            "description_sale": html2plaintext(data.get("description") or "") or False,
            "list_price": min((float(v.get("price") or 0.0) for v in variants), default=0.0),
            "categ_id": self._get_category(data.get("product_type")),
        }
        image = self._download_image(data.get("image_url"))
        if image:
            vals["image_1920"] = image
        if single:
            first = variants[0] if variants else {}
            vals.update(
                default_code=first.get("sku") or False,
                barcode=first.get("barcode") or False,
                weight=float(first.get("weight") or 0.0),
            )
            return self.env["product.template"].create(vals)
        vals["attribute_line_ids"] = [
            Command.create(
                {
                    "attribute_id": self._get_attribute(name).id,
                    "value_ids": [
                        Command.set(
                            [
                                self._get_attribute_value(name, value).id
                                for value in self._ordered_values(variants, index)
                            ]
                        )
                    ],
                }
            )
            for index, name in enumerate(option_names)
        ]
        return self.env["product.template"].create(vals)

    @staticmethod
    def _ordered_values(variants, index):
        values = []
        for variant in variants:
            options = variant.get("options") or []
            if index < len(options) and options[index] not in values:
                values.append(options[index])
        return values

    def _get_category(self, name):
        if not name:
            return self.env.ref("product.product_category_goods", raise_if_not_found=False).id or False
        category = self.env["product.category"].search([("name", "=", name)], limit=1)
        return (category or self.env["product.category"].create({"name": name})).id

    def _get_attribute(self, name):
        attribute = self.env["product.attribute"].search([("name", "=ilike", name)], limit=1)
        return attribute or self.env["product.attribute"].create(
            {"name": name, "create_variant": "always"}
        )

    def _get_attribute_value(self, attribute_name, value):
        attribute = self._get_attribute(attribute_name)
        found = self.env["product.attribute.value"].search(
            [("attribute_id", "=", attribute.id), ("name", "=ilike", value)], limit=1
        )
        return found or self.env["product.attribute.value"].create(
            {"attribute_id": attribute.id, "name": value}
        )

    def _map_variants(self, template, variants):
        """Link each channel variant to the Odoo variant with the same options (or SKU)."""
        if len(variants) <= 1 and len(template.product_variant_ids) == 1:
            variant = variants[0] if variants else None
            if variant and variant.get("external_id"):
                self._set_mapping(
                    template.product_variant_id, variant["external_id"], variant.get("inventory_ref")
                )
            return
        for variant in variants:
            product = self._find_product(
                {
                    "variant_id": None,
                    "sku": variant.get("sku"),
                    "barcode": variant.get("barcode"),
                }
            )
            if not product or product.product_tmpl_id != template:
                wanted = {(o or "").strip().lower() for o in variant.get("options") or []}
                product = next(
                    (
                        p
                        for p in template.product_variant_ids
                        if {
                            v.name.strip().lower()
                            for v in p.product_template_attribute_value_ids
                        }
                        == wanted
                    ),
                    self.env["product.product"],
                )
            if not product:
                continue
            values = {}
            if variant.get("sku") and not product.default_code:
                values["default_code"] = variant["sku"]
            if variant.get("barcode") and not product.barcode:
                values["barcode"] = variant["barcode"]
            if values:
                product.write(values)
            self._set_mapping(product, variant["external_id"], variant.get("inventory_ref"))

    def _download_image(self, url):
        if not url:
            return False
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            return base64.b64encode(response.content)
        except requests.RequestException:
            _logger.info("Could not download product image %s", url)
            return False
