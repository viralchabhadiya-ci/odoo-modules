# PrestaShop Connector

Uses the **PrestaShop Webservice** (PrestaShop 1.7, 8 and 9; JSON answers, XML for writes). The key is the only credential.

## Connect

1. In the back office open **Advanced Parameters > Webservice**, switch the webservice on and **Add new webservice key**.
2. Generate the key and give it *View* on orders, order_slips, order_states, customers, addresses, countries, states,
   currencies, carriers, products, combinations, product_options, product_option_values, stock_availables and
   configurations, *Modify* on stock_availables and order_carriers, and *Add* on order_histories.
3. In Odoo: **Sales Channels > Stores > New**, choose PrestaShop, paste the shop address (without /api) and the key.
   Odoo reads the shop name, language and **time zone** from the shop.

## What is synced

| Direction | What |
|---|---|
| PrestaShop to Odoo | orders (every 15 minutes), customers and invoice / delivery addresses, tax rates, cart-rule discounts, shipping and gift wrapping, cancelled orders, **credit slips as credit notes** (by returned quantity), products with combinations (options, reference, EAN, price, picture) |
| Odoo to PrestaShop | stock (changed quantities only, per product and combination), tracking number on the order carrier and the order moved to the *Shipped* state when every delivery is done |

Dates are converted from the shop's time zone, so orders are never missed or shifted around daylight-saving changes.
A stock line the shop counts itself (advanced stock management) or a product deleted in the shop is reported by name and
skipped; everything else still goes through.

PrestaShop has no webhooks, so orders arrive on the schedule (or with the *Import orders* button).

Not included yet: creating or editing PrestaShop products from Odoo, multistore (one store record per shop works),
cart rules as separate records, customer groups and specific prices.
