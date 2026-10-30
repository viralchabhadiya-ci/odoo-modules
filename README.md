# Odoo modules

Connectors that link Odoo 19 (Community and Enterprise) with e-commerce platforms and marketplaces.

| Module | What it does | Status |
|---|---|---|
| [`channel_connector_base`](channel_connector_base/readme/DESCRIPTION.md) | Shared engine: stores, job queue and sync log, order workflows, stock sync, webhooks, setup wizard | ready |
| [`channel_shopify`](channel_shopify/readme/DESCRIPTION.md) | Shopify (Admin GraphQL API) | ready |
| [`channel_woocommerce`](channel_woocommerce/readme/DESCRIPTION.md) | WooCommerce (REST API v3) | ready |
| [`channel_amazon`](channel_amazon/readme/DESCRIPTION.md) | Amazon Selling Partner API (merchant-fulfilled and FBA orders) | ready, not yet run against a live seller account |
| [`channel_prestashop`](channel_prestashop/readme/DESCRIPTION.md) | PrestaShop Webservice (orders, credit slips, combinations, stock, tracking) | ready, not yet run against a live shop |
| [`channel_ebay`](channel_ebay/readme/DESCRIPTION.md) | eBay REST (Sell Fulfillment + Inventory): orders, refunds, stock, shipments | ready, not yet run against a live account |
| [`channel_magento`](channel_magento/readme/DESCRIPTION.md) | Magento 2 / Adobe Commerce REST: orders, credit memos, products, MSI stock, shipments | ready, not yet run against a live shop |

Email marketing (independent of the sales channel modules):

| Module | What it does | Status |
|---|---|---|
| [`marketing_connector_base`](marketing_connector_base/readme/DESCRIPTION.md) | Shared engine: audiences to mailing lists, contacts, unsubscribes both ways, campaign results, webhooks | ready |
| [`marketing_mailchimp`](marketing_mailchimp/readme/DESCRIPTION.md) | Mailchimp Marketing API v3 | ready, not yet run against a live account |
| [`marketing_brevo`](marketing_brevo/readme/DESCRIPTION.md) | Brevo API v3 | ready, not yet run against a live account |

Reporting:

| Module | What it does | Status |
|---|---|---|
| [`powerbi_connector`](powerbi_connector/readme/DESCRIPTION.md) | Power BI reads Odoo through an OData feed with an access key; optional push dataset | ready, not yet run against Power BI itself |

Planned: Claude, QuickBooks.

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
