# eBay Connector

Uses the eBay REST **Sell Fulfillment** and **Sell Inventory** APIs with OAuth (App ID, Cert ID and a seller Refresh token).

## Connect

1. On developer.ebay.com create a keyset (**Application Keys**) and copy the **App ID** and **Cert ID**.
2. Under **User Tokens** sign in as the seller with the scopes `sell.fulfillment` and `sell.inventory` and copy the
   **Refresh token** (valid for 18 months; Odoo tells you when eBay refuses it).
3. In Odoo: **Sales Channels > Stores > New**, choose eBay and paste the three values. Switch on *Sandbox* for sandbox keys.

## What is synced

| Direction | What |
|---|---|
| eBay to Odoo | paid orders every 15 minutes (not only the shipped ones), buyer and delivery address, line prices with the item promotions, shipping, order-level and shipping discounts, VAT or sales tax (detected from the order totals), cancellations, refunds as credit notes (by line when a whole line was refunded, otherwise by amount), inventory items |
| Odoo to eBay | stock per SKU in bulk (25 per call), a shipping fulfillment per delivery with carrier and tracking number (tracking is optional) |

Listings created in the old Seller Hub / Trading way (no SKU) still work for orders: the product is found or created from
the order line. Stock for them cannot be sent through the Inventory API, so those SKUs are reported by name as refused
while the rest goes through.

Tax that eBay collects and remits itself is not booked in Odoo, because it never reaches the seller.

Not included yet: creating or editing listings from Odoo, Trading API stock updates for old listings, buyer feedback,
fees and payouts reconciliation, notifications (orders are polled).
