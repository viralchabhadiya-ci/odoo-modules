# WooCommerce Connector

Uses the **WooCommerce REST API v3** with a consumer key and secret (https only).

## Connect

1. In WordPress open **WooCommerce > Settings > Advanced > REST API**, click **Add key**, choose **Read/Write**
   permission and copy the Consumer key and Consumer secret.
2. Make sure **Settings > Permalinks** is not set to *Plain*; otherwise WooCommerce hides its REST API.
3. In Odoo: **Sales Channels > Stores > New**, choose WooCommerce, paste the website address and both keys.

## What is synced

| Direction | What |
|---|---|
| WooCommerce to Odoo | orders (webhooks and every 15 min), customers and addresses, taxes, discounts, shipping, cancellations, refunds as credit notes (by line or by amount), simple and variable products with images, SKU |
| Odoo to WooCommerce | stock (batched, changed quantities only), tracking number as a customer note and the order set to *Completed* once every delivery is done |

WooCommerce has a single stock pool, so the store shows one location mapped to one warehouse.

Not included yet: creating or editing WooCommerce products from Odoo, coupons as separate records, WPML translations.
