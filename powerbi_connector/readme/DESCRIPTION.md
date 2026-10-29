# Power BI Connector

Lets Power BI read Odoo sales, invoices, stock and purchases directly. No Azure app, no Python package, no gateway.
Power BI reads a normal **OData feed** from Odoo; for Odoo systems that cannot be reached from the internet there is an
optional second way, sending a copy to a Power BI dataset.

## Connect (the feed)

1. **Power BI > Feeds > New**. Choose the Odoo user the feed reads as: Power BI sees exactly what that user may read, with
   access rights and record rules applied.
2. Click **Add sales tables** (or accounting, inventory, purchases), or add any Odoo model as a table. Pick its columns and
   an optional filter, or leave the columns empty for every ordinary column.
3. **Create access key.** Odoo shows the address and the key once and keeps only a fingerprint.
4. In Power BI Desktop: **Get data > OData feed**, paste the address, choose **Basic**, any user name and the key as password,
   pick the tables. The same address works for scheduled refresh in the Power BI service when Odoo is reachable from the
   internet.

## What Power BI gets

- Relations come as two columns: `partner_id` (the id, for joining tables) and `partner_id_name` (the name, for charts).
- Choices come as the code and the label: `state` and `state_label`.
- Dates arrive as dates, date and times in UTC, amounts as numbers, empty values as empty.
- Power BI's own filters, column choice, sort and paging are answered by Odoo (query folding), so a report over a big table
  only fetches what it needs; the answer is paged (5000 rows by default) and Power BI follows the pages itself.
- A key can expire, be replaced (the old one stops at once) or be revoked. The feed shows when it was last used.

## Send a copy to Power BI (optional)

Tick **Also send a copy to the Power BI service** on the feed and enter an Azure app (tenant ID, application ID, secret) and the
workspace ID. The app must be allowed in the Power BI admin portal and be a Member of the workspace. Odoo creates a push dataset
with the feed's tables and replaces the rows every hour, 6 hours or day, or when you press **Send now**. Changing the columns of
a table updates it in place, so reports stay connected; adding or removing a table creates a new dataset. Power BI limits push
datasets to one million rows added per hour.

Not included: building reports (that is done in Power BI), many-to-many and one-to-many columns, Direct Query.
