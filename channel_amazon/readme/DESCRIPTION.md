# Amazon Connector

Uses the **Amazon Selling Partner API**: Orders API v2026-01-01, Listings Items API and Orders shipment confirmation.
No AWS account or request signing is needed, only a Login with Amazon app.

## Connect

1. In **Seller Central > Apps and Services > Develop Apps** register a private developer profile and add an app.
   Tick the roles *Inventory and Order Tracking*, *Product Listing* and *Direct-to-Consumer Shipping (Restricted)*
   (the last one is what lets Amazon hand over the buyer's name and delivery address).
2. Copy the **Client ID** and **Client secret**, then choose **Authorize app** to get the **Refresh token**.
3. Your **Seller ID** is under *Account Info > Your Merchant Token*.
4. In Odoo: **Sales Channels > Stores > New**, choose Amazon, pick the region and paste the four values. Odoo reads your
   marketplaces from Amazon.

## What is synced

| Direction | What |
|---|---|
| Amazon to Odoo | orders every 15 minutes (merchant-fulfilled and FBA), customers and addresses, tax rates worked out from Amazon's tax amounts, discounts, shipping and gift-wrap charges, cancellations, listings (SKU, title, image, price) |
| Odoo to Amazon | merchant-fulfilled stock (changed quantities only, minus safety stock), shipment confirmation with carrier and tracking number when a delivery is validated |

## FBA

Orders with *Fulfilled by Amazon* are taken from the warehouse set in **Stock > Shipments > Warehouse for orders the
channel ships**, are delivered automatically and send nothing back. Listings that Amazon stores are never sent Odoo stock.

## Good to know

- Amazon lists buyer data only while an order is open, and its policy asks you to delete it later. The buyer e-mail is
  an Amazon relay address that changes with every order, so each order gets its own contact.
- Prices are imported without tax when Amazon lists the tax separately. When it lists none (for example US marketplace
  facilitator tax) the amount is used as it is.
- Pending orders are imported once Amazon moves them to *Unshipped*.

Not included yet: refunds and returns, FBA inventory pulled into Odoo, settlement reports, creating listings from Odoo.
