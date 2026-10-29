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

## Claude (added 2026-10-29)

Competitors (Odoo 19 listings): Serpent $122, Merkago $88, EASYMOB $112, all "MCP server for Odoo" style apps.
Gaps we design against (what a raw MCP passthrough tends to leave open): one all-powerful token, a full ORM passthrough (any model, any method), no per-model write rights,
no record filters, no audit trail, and no hint to Claude about what it may not do (it finds out by failing).

What we build against: Model Context Protocol over streamable HTTP (JSON-RPC 2.0, `initialize`, `tools/list`, `tools/call`),
bearer access keys, tool annotations (`readOnlyHint`, `destructiveHint`) and `structuredContent` results.
Where we beat the field:
1. Keys start read only; create, change, delete and button presses are separate switches, per model.
2. A filter per model narrows what Claude sees and changes, on top of the user's own access rights and record rules.
3. Tools a key cannot use are not offered, so Claude does not try; refusals say what to ask the administrator for.
4. Credentials and settings models and secret-looking fields are closed whatever the key says.
5. Audit log of every call, expiry, replaceable key stored as a fingerprint, daily limit.
6. Purpose-built tools (search with paging and totals, grouped sums, describe) instead of one raw `execute_kw`.
Not yet: OAuth 2.1 sign-in (needed by claude.ai custom connectors), prompts and resources, an "Ask Claude" button inside Odoo.
