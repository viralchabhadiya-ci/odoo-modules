# Shopify + WooCommerce Odoo connectors: market research and plan

Date: 2026-10-29. Target: Odoo 19.0 (Community + Enterprise).

## Why these two first
Shopify and WooCommerce are the two most-listed and most-reviewed ecommerce connectors on the Odoo Apps store
(every big vendor — Emipro, Webkul, VentorTech, Techspawn, Cybrosys — sells both, and they are the top sellers
in the eCommerce category). Amazon comes next.

## Competitors

| Vendor | Shopify price | Woo price | Strong points | Weak points |
|---|---|---|---|---|
| Emipro (`shopify_ept`, `woo_commerce_ept`) | ~$630 | ~$616–640 | Most complete: 2-way products, stock by location, BoM/kit stock, payouts reconciliation, refunds both ways, auto workflows, queues + retry, dashboard | Most expensive, long configuration, per-version purchase, no Odoo Online |
| Webkul (Multi Channel Sale) | ~$170–199 | ~$220 | Cheap, many Odoo versions, GraphQL, locations→warehouses | Needs separate Multi Channel base, reviews complain about sync not working, stock not importing, weak support |
| VentorTech PRO | ~$573 | ~$582 or €499/yr | Setup wizard, webhooks, fraud risk, metafields, logs with retry | No Odoo Online, Woo refunds "coming soon" |
| Techspawn | ~$217 | ~$201 | Webhooks, multi-store, credit notes from refunds | No payouts, no metafields |
| Odoo IN / Cybrosys | ~$90–197 | | Cheap, simple | Manual product/stock imports, thin features, unclear multi-store/refunds |

## Common complaints we design against
1. Sync silently fails, nobody knows why → every operation is a visible job with a plain-language error and a Retry button.
2. Setup takes days → a guided onboarding wizard with "Test connection" and smart defaults (warehouse, pricelist, journal auto-picked).
3. Stock mismatch → stock pushed from stock moves (only changed products), choice of On hand / Free / Forecast, per-location mapping.
4. Duplicate orders/customers → idempotent order import by external id + customer matching by email/phone.
5. Needs extra paid base module → our shared base ships in the same repo and installs automatically.

## Feature checklist (both connectors)
- Multi-store per company, credentials test, onboarding wizard
- Products: import (with variants, images, SKU/barcode matching), export/update, price sync from pricelist
- Inventory: scheduled + on-change push, location mapping, quantity type choice
- Orders: webhook (real-time) + scheduled import, status filter, date-range historical import, taxes, shipping, discounts
- Auto workflow per payment method: confirm order, create + post invoice, register payment, validate delivery
- Fulfillment: tracking number + carrier pushed back when delivery is validated
- Cancellations and refunds: imported as cancellations / credit notes
- Customers: import with billing + shipping addresses, matching
- Payment gateway → journal mapping, tax mapping
- Webhooks with signature verification (HMAC-SHA256) and auto-registration
- Job queue with retries, failed-job view, logs, chatter
- Dashboard per store (orders today, revenue, failed jobs, last sync)

## Architecture
- `channel_connector_base`: stores, job queue, mappings, auto-workflow, dashboard, webhook endpoint, wizard framework.
- `channel_shopify`: Shopify Admin GraphQL API (access token or client-credentials app), HMAC webhooks.
- `channel_woocommerce`: WooCommerce REST v3 (consumer key/secret), HMAC webhooks.
The same base will be reused for Amazon, PrestaShop, eBay, Magento 2, etc.

Sources: webkul.com/blog/best-odoo-shopify-connector, webkul.com/blog/best-odoo-woocommerce-connectors,
apps.odoo.com woo_commerce_ept listing, appnavigator.io Webkul Shopify connector reviews.

## Power BI (added 2026-10-29)

Competitors (Odoo 19 listings): ECOSIRE $481 (push datasets with delta mode, a custom pull API, Azure AD service principal,
blueprints; needs `msal`, Pro workspace and five Odoo apps), Creyox $395 (Power BI Desktop data source with a token, Fabric
services, embedding, pipelines; Premium for pipelines), Weblytic Labs $99 (workspace, dataset and tables from Odoo, push on a
schedule, importing reports into Odoo; needs Sales, Website, eCommerce and Invoicing).
Typical gaps: every one needs an Azure app registration even to read data, a custom connector or API instead of the
standard OData feed Power BI already understands, "delta" push cannot update a changed row (push datasets only append), and
one-off blueprints that dump all columns.

What we build against: OData v4 JSON (`$metadata`, `$filter`, `$select`, `$orderby`, `$top`, `$skip`, `$count`, server paging)
read by Power BI's built-in OData connector; for push, `datasets`, `tables/{name}/rows` with an Azure AD client-credentials token.
Where we beat the field:
1. Nothing to register in Azure to read data: an address and an access key (stored hashed, shown once, expiring, revocable).
2. The standard OData feed, so Power BI folds filters and columns to Odoo and refreshes on its own schedule.
3. Rights are the feed user's: ACL and record rules, unreadable columns are never exposed.
4. Relations and choices arrive as id plus name and code plus label, ready for joins and charts.
5. Push replaces rows (no duplicates) and updates table columns in place so reports stay connected.
Not yet: Direct Query, building reports, embedding reports inside Odoo.
