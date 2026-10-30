# Mailchimp Connector

Uses the Mailchimp Marketing API v3 with an **API key** (no Python package to install). Built on `marketing_connector_base`,
which explains the sync rules.

## Connect

1. In Mailchimp: profile > **Extras > API keys > Create A Key**.
2. In Odoo: **Email Marketing > Connected accounts > Accounts > New**, choose Mailchimp, paste the key (it ends with a code such as
   `-us21`) and click **Connect**. Tick the audiences to keep in sync.

## What is synced

| Direction | What |
|---|---|
| Mailchimp to Odoo | subscribers, first and last name, unsubscribes, bounced addresses (as opted out), campaign results (sent, opened, clicked, unsubscribed, bounced) |
| Odoo to Mailchimp | new and changed contacts in batches of 500, opt-outs and blacklisted addresses as *unsubscribed* |

Members who have not confirmed a double opt-in (*pending*) are not mailed from Odoo until they confirm. Addresses Mailchimp
refuses are listed with Mailchimp's reason. A webhook per audience (when Odoo is on https) brings changes within seconds; what
Odoo sends itself is not echoed back.

Works on the free plan (one audience). Mailchimp tags, segments, e-commerce stores and templates are not synced yet.
