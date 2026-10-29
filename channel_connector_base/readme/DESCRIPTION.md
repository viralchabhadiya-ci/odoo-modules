# Sales Channel Connector Base

The engine behind every sales-channel connector. On its own it connects nothing; install it together with
`channel_shopify` (or another connector).

## What the user gets

- **Stores dashboard**: one card per shop with orders and revenue today, linked products, waiting jobs and a red
  "need attention" strip.
- **Two-step setup wizard**: connect and test, then choose warehouse and automation. A wrong password never leaves a
  half-created store behind.
- **Sync log**: every order import, stock update and shipment is a job with a plain-language error and a Retry button.
  Failed jobs retry by themselves (2, 4, 8... minutes) before asking for attention.
- **Order workflows** per payment method: confirm, invoice, validate, register payment, validate delivery.
- **Stock sync**: only changed quantities are sent; choose available / on hand / forecasted, hold back a safety
  quantity, map each channel location to a warehouse.
- **Refunds and cancellations** from the channel become credit notes (draft or posted) and cancelled orders.
- **Shipments**: validating a delivery in Odoo queues the shipment and tracking number for the channel.
- Multi-store, multi-company, groups (`Sales Channels / User` and `Administrator`; credentials are admin only).

## For connector authors

Subclass `channel.store` with `selection_add` on `channel_type` and implement the hooks below, named
`_<channel_type>_<hook>` (for example `_shopify_fetch_orders`), so several connectors can live side by side:

| Hook | Returns |
|---|---|
| `_test_connection()` | shop name (and refreshes locations) |
| `_fetch_orders(since)` / `_fetch_order(id)` | normalised order dicts |
| `_fetch_products()` | normalised product dicts |
| `_push_stock_levels(items)` | nothing, raise `ChannelError` on failure |
| `_push_fulfillment(picking)` | nothing |
| `_register_webhooks()`, `_verify_webhook()`, `_parse_webhook()` | webhook support |

Raise `ChannelError` for anything the user can fix (the message is shown as is) and `ChannelAuthError` when the
credentials are rejected. Everything else is logged with its traceback on the job.

Normalised order: `external_id, name, date, updated_at, cancelled, currency, email, phone, note, customer_id,
billing, shipping, taxes_included, gateways, paid, fulfilled, lines[{external_id, name, sku, barcode, variant_id,
inventory_ref, qty, price, discounted_price, taxes[{rate}]}], shipping_lines[{title, price, taxes}],
refunds[{external_id, amount, lines[{line_id, sku, qty}]}]`. See `channel_shopify/models/channel_store.py`.
