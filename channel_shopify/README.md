# Shopify Connector

Uses the Shopify **Admin GraphQL API** (default version `2026-07`, change it with the system parameter
`channel_shopify.api_version`).

## Connect

1. In the Shopify Dev Dashboard create an app and give it these scopes: `read_orders`, `read_all_orders`,
   `read_products`, `read_customers`, `read_inventory`, `write_inventory`, `read_locations`,
   `read_merchant_managed_fulfillment_orders`, `write_merchant_managed_fulfillment_orders`.
2. Install it on your shop. The app and the shop must belong to the same organization.
3. In Odoo: **Sales Channels > Stores > New**, choose Shopify, paste shop address, Client ID and Client secret.
   Odoo fetches a 24 hour token by itself and renews it before it expires.
   An older custom app can use its Admin API access token instead.

## What is synced

| Direction | What |
|---|---|
| Shopify to Odoo | orders (webhooks and every 15 min), customers and addresses, taxes, discounts, shipping, cancellations, refunds as credit notes, products with variants, images, SKU and barcode |
| Odoo to Shopify | stock per location (changed quantities only), shipments with tracking number |

Not included yet: creating or editing Shopify products from Odoo, payout reconciliation, B2B companies.
