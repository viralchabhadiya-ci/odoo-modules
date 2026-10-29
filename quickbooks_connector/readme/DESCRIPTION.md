# QuickBooks Online Connector

Keeps QuickBooks Online up to date with what you invoice, buy and collect in Odoo, and brings back customers, vendors and
payments entered in QuickBooks. It needs only **Invoicing** (no Sales, Purchase, Inventory, HR or Project apps).

## Connect

1. At developer.intuit.com create an app (a sandbox company is free). Add the **Redirect URI** that Odoo shows on the connection
   as an allowed redirect, and copy the Client ID and Client secret.
2. **QuickBooks > Connections > New.** Paste the keys and save. Press **Connect to QuickBooks**: you sign in at Intuit, choose the
   company and come back to Odoo connected. Odoo reads the QuickBooks accounts and tax codes and matches them with yours by
   number, name or rate.
3. Choose the *Send documents dated from* date (today by default, so history is never sent by accident) and you are done.
   Every 15 minutes, or when you press **Sync now**, Odoo sends what changed and brings in what changed in QuickBooks.

## What moves

| Odoo | QuickBooks | Direction |
|---|---|---|
| Customer invoice, credit note | Invoice, Credit memo | to QuickBooks. Taken back to draft or cancelled: voided there |
| Vendor bill, vendor credit | Bill, Vendor credit | to QuickBooks. Taken back to draft: deleted there |
| Customer payment, vendor payment | Payment, Bill payment (against the invoices or bills it settles) | to QuickBooks |
| Payment entered in QuickBooks against an invoice or bill that came from Odoo | Payment, Bill payment | brought into Odoo, registered and reconciled on the journal you choose |
| Customers and vendors | Customer, Vendor | both ways. Created in QuickBooks when a document needs them; QuickBooks contacts are brought in |
| Products | Items | to QuickBooks when an invoice uses them |

Details that save work:

- **No duplicate names.** QuickBooks keeps one name list for customers and vendors. A name that already exists is linked, not
  duplicated; a company that is both customer and vendor in Odoo gets "Name (Vendor)" on the vendor side. Contacts coming from
  QuickBooks are matched with existing Odoo contacts by email, then name.
- **One round never stops on one record.** A record that QuickBooks refuses gets its reason on the **Sync log** (and on the
  connection), is retried up to three times, and waits for **Retry** or for a change. Everything else carries on.
- **Unchanged records are not sent again.** Each sent record keeps a fingerprint, so a round costs few API calls.
- **Totals are checked.** If QuickBooks computes a different total than Odoo (a tax set-up difference) the record gets a warning.
- **Taxes.** US companies: lines are marked taxable or not and QuickBooks works out the tax. Elsewhere: each Odoo tax points at a
  QuickBooks tax code (matched by name or rate, adjustable on the tax); lines without tax use the "tax code for lines without
  tax" you choose.
- Discounts are folded into the unit price, notes and sections travel as description lines, foreign currency documents carry
  their currency and rate. Bill lines go to the QuickBooks account matched with the Odoo account.
- **Send to QuickBooks** on an invoice or bill sends it at once, whatever its date.
- If both Odoo and QuickBooks changed the same contact between two rounds, Odoo wins.

## Safety

- The Intuit refresh token changes on every use; Odoo saves each new one in its own transaction so a failed round never loses it.
  Tokens sit in a table nobody can read through the interface.
- Connection and sync log are visible to the QuickBooks administrators only.

Not included: inventory quantities, journal entries, purchase and sales orders, applying credit memos to invoices inside
QuickBooks, QuickBooks webhooks (changes are picked up by the 15 minute round), and a historical import wizard (set an earlier
*dated from* date instead).
