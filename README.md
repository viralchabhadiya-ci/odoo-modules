# Odoo modules

Connectors that link Odoo 19 (Community and Enterprise) with e-commerce platforms and marketplaces.

| Module | What it does | Status |
|---|---|---|
| [`channel_connector_base`](channel_connector_base/readme/DESCRIPTION.md) | Shared engine: stores, job queue and sync log, order workflows, stock sync, webhooks, setup wizard | ready |
| [`channel_shopify`](channel_shopify/readme/DESCRIPTION.md) | Shopify (Admin GraphQL API) | ready |
| [`channel_woocommerce`](channel_woocommerce/readme/DESCRIPTION.md) | WooCommerce (REST API v3) | ready |
| [`channel_amazon`](channel_amazon/readme/DESCRIPTION.md) | Amazon Selling Partner API (merchant-fulfilled and FBA orders) | ready, not yet run against a live seller account |
| [`channel_prestashop`](channel_prestashop/readme/DESCRIPTION.md) | PrestaShop Webservice (orders, credit slips, combinations, stock, tracking) | ready, not yet run against a live shop |

Planned: Claude, QuickBooks, eBay, Power BI, Magento 2, Brevo, Mailchimp.

## Design

Every sales-channel connector is a thin adapter on top of `channel_connector_base`.
An adapter only knows how to talk to its platform and turn platform data into a small neutral dictionary
(order, product, refund). The base module does everything on the Odoo side: partners, taxes, sale orders,
invoices, payments, credit notes, stock levels, retries and logs. A new platform is therefore a few hundred
lines, and all platforms behave the same for the user.

See [`docs/market-research.md`](docs/market-research.md) for how this compares with existing connectors.

## Development

```bash
git clone --depth 1 -b 19.0 https://github.com/odoo/odoo ../odoo
python ../odoo/odoo-bin --addons-path=../odoo/addons,. -d dev -i channel_shopify --test-enable --test-tags /channel_shopify --stop-after-init
```

Tests mock the platform API, so they run without a shop. Lint with `ruff check .`.
