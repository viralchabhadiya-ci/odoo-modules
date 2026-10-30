# Magento 2 / Adobe Commerce Connector

Uses the Magento REST API with an **integration access token**. No OAuth dance and no Magento extension is needed.

## Connect

1. In Magento: **System > Extensions > Integrations > Add New Integration**. Under **API** allow *Sales > Operations > Orders*
   (view, ship, credit memos), *Catalog > Inventory > Products* and *Stores > Inventory* (sources, if you use MSI), then
   **Activate** and copy the **Access Token**.
2. In Odoo: **Sales Channels > Stores > New**, choose Magento 2, enter the shop address (no `/rest` at the end) and the token.
3. Optionally set the *Store view code* (default `default`) if you run several store views.

If the token is valid but misses a permission, Odoo names the missing resource and does not disconnect the store.

## What is synced

| Direction | What |
|---|---|
| Magento to Odoo | orders every 15 minutes (guest and customer), configurable products booked as the chosen variant, line prices and discounts, shipping with its tax and shipping discount, VAT rate matched to your Odoo taxes, cancellations, credit memos as credit notes (by returned quantity), products with configurable children as variants and their option labels, MSI sources as locations |
| Odoo to Magento | stock per SKU in bulk (MSI `source-items`, 100 per call; plain stock item for shops without MSI), a shipment per delivery with carrier and tracking number (the customer is notified) |

Cash on delivery and other unpaid orders stay quotations until Magento has invoiced them. If Magento refuses a bulk stock
update because of one unknown SKU, Odoo retries one by one so only the bad SKU is reported, by name.

Not included yet: creating or editing catalogue products from Odoo, customer groups and tier prices, several websites in
one store (create one Odoo store per store view), bundle and grouped products as kits, webhooks (orders are polled).
