# Brevo Connector

Uses the Brevo (formerly Sendinblue) API v3 with an **API key**. Built on `marketing_connector_base`, which explains the sync
rules.

## Connect

1. In Brevo: your name (top right) > **SMTP & API > API keys > Generate a new API key**. If you limit access by IP address
   (Security > Authorised IPs), add the address of the Odoo server.
2. In Odoo: **Email Marketing > Connected accounts > Accounts > New**, choose Brevo, paste the key and click **Connect**. Tick the
   lists to keep in sync.

## What is synced

| Direction | What |
|---|---|
| Brevo to Odoo | contacts of the ticked lists with first and last name, unsubscribes (Brevo calls them *blacklisted*), campaign results |
| Odoo to Brevo | new and changed contacts imported into the list in one call, an opt-out as *removed from that list*, a blacklisted address as *blacklisted* so no list reaches it |

Brevo checks an import after it is accepted, so an address it dislikes shows up in Brevo's import log, not in Odoo. Taking a
contact off a list in Brevo is not read as an unsubscribe (it may simply not have been sent yet); blacklisting is. One webhook for
the account (when Odoo is on https) brings unsubscribes within seconds.

Not synced yet: custom attributes, folders, segments, transactional email, SMS, templates.
