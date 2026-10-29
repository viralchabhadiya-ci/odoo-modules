# Shopify, WooCommerce and Amazon Odoo connectors: market research and plan

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

## Amazon (added 2026-10-29)

Competitor: Emipro `amazon_ept` v19, about $614 plus IAP packs (free for 3 months, then 99 EUR a month or 999 EUR a year).
It covers FBM, FBA and mixed accounts, many marketplaces, product import, FBM stock export, FBA live inventory,
returns and refunds, inbound shipments, removal orders and settlement reports. Common complaints: billing that is hard
to predict (IAP fees), slow support, long configuration.

What we build against, from the Selling Partner API documentation:
- **Auth**: Login with Amazon refresh token (client id + secret + refresh token). Self-authorised private apps need no AWS
  account, IAM user or request signing any more, so setup is three pasted values and a Seller ID.
- **Orders**: Orders API v2026-01-01 (`searchOrders`, `getOrder`). v0 is being replaced; the new API says explicitly
  whether an item amount includes tax, which v0 did not. Orders are polled (Amazon only pushes via SQS/EventBridge, not
  HTTPS) with `lastUpdatedAfter`; the sustained limit is one `searchOrders` call every ~3 minutes, so the 15 minute cron
  never throttles and a 429 is handled without waiting.
- **Listings and stock**: Listings Items API (`searchListingsItems`, `patchListingsItem` on `fulfillment_availability`).
- **Shipments**: `shipmentConfirmation` (one call per package, tracking number required).

How we beat the field:
1. Marketplaces are read from Amazon when you press Test connection; nobody types marketplace ids.
2. FBA done right: orders Amazon ships are taken from a separate FBA warehouse and booked as delivered, and FBA listings
   are flagged so Odoo's stock can never overwrite them.
3. A refused SKU (closed listing, wrong product type) is reported by name as one visible problem while the rest of the
   stock update goes through.
4. A missing app role gives a message saying which role to tick; it does not disconnect the store.
5. No per-order fees: it is a normal module.

Not in this version: refunds/returns (Finances API), FBA inventory pulled into Odoo, settlement reconciliation, creating
listings from Odoo, B2B/VAT invoicing data, SQS notifications.

## PrestaShop (added 2026-10-29)

Competitors (Odoo 19 listings): Webkul $199 (two modules; credit slips only on 19/20), VentorTech PRO $568
(refunds "coming soon", webhooks need an extra PrestaShop module, mrp dependency), Globalteckz $248, Jupical $233,
TeqStars $250. Only Webkul syncs credit slips; none of the others do, and all of them sync on a schedule.

What we build against: the PrestaShop Webservice (basic auth with the key as user, `output_format=JSON`, `filter`,
`display=full`, `limit=offset,count`; writes are XML only). We beat the field on:
1. Credit slips become credit notes, by returned quantity, and a slip on an otherwise unchanged order is still picked up.
2. Shop time zone read from the shop (PrestaShop dates carry no offset) so order sync windows are right.
3. Bulk lookups per batch of orders (customers, addresses) instead of three calls per order.
4. Stock refused by name (advanced stock management, deleted product) without blocking the rest.
5. Tracking number plus order status in one step, and no second status change when the order is already shipped.
6. Key permissions are explained resource by resource in the error message.

## eBay (added 2026-10-29)

Competitors (Odoo 19 listings): Emipro `ebay_ept` $705 (imports only shipped orders of the last 90 days, no new-listing
export in v19, no refunds mentioned; reviewers report variation quantities wrong, exports failing silently, older listings
without SKU not importable, outdated authentication), TeqStars `ebay` $414 (Trading API for listings and stock, refunds and
legacy listings not addressed, refresh token needs re-authorising every ~18 months).

What we build against: Sell Fulfillment API (`getOrders` with `lastmodifieddate` filter, 200 per page; `createShippingFulfillment`)
and Sell Inventory API (`inventory_item`, `bulk_update_price_quantity`), OAuth refresh token -> 2 hour access token.
Where we beat the field:
1. Paid orders are imported straight away, not only after shipping.
2. VAT-inclusive (UK/EU) and tax-on-top (US) orders are told apart from the order totals; marketplace-collected tax is left out.
3. Lines of old listings without SKU still import (product found or created from the order line).
4. Refunds become credit notes, by line when a whole line was refunded.
5. Stock refusals are named; a dead refresh token says how long they last and how to renew.

## Magento 2 (added 2026-10-29)

Competitors (Odoo 19 listings): Webkul $199 (MSI and multi-store only through paid extensions), VentorTech Connector PRO
$582 (configurable products, MSI, queues; some real-time flows need a Magento extension), Emipro $625 (MSI, refunds and
credit notes, webhooks), Odoo IN $199 (SKU mapping, admin token), SDLC (price on request, multi-store, queues).
Typical gaps: extra Magento-side modules for live sync, paid extensions for MSI, one purchase per Odoo version.

What we build against: REST `/rest/{store_code}/V1` with an integration token; `orders` and `creditmemos` searched by
`updated_at`/`created_at`; MSI `inventory/source-items` (legacy `stockItems/1` when MSI is off); `order/{id}/ship`.
Where we beat the field:
1. Nothing to install in Magento; MSI sources become Odoo locations automatically.
2. Configurable products are booked as the chosen variant, with discount, shipping tax and shipping discount.
3. Credit memos become credit notes for the returned quantity only, even when the order itself did not change.
4. One unknown SKU no longer fails a whole stock batch: it is found by retrying one by one and named.
5. Missing API permissions are named in plain words (which resource) instead of "not authorized".
