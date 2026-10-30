# Email Marketing Connector Base

The shared engine behind the Mailchimp and Brevo connectors. It keeps **audiences / lists** at the provider in step with
Odoo **mailing lists**, and brings **campaign results** into Odoo. It does nothing on its own: install a provider module
(`marketing_mailchimp`, `marketing_brevo`).

## How it works

1. **Email Marketing > Connected accounts > Accounts > New**, choose the provider, paste the API key, click **Connect**.
   The form explains where to find the key.
2. Odoo lists the provider's audiences. Tick **Keep in sync** on the ones you want. Each is matched to the Odoo mailing
   list of the same name (created if needed), or to the list you choose.
3. Every 15 minutes, and on **Sync now**, Odoo reads what changed at the provider and sends what changed in Odoo.
   Only changes travel: a contact that did not change is never sent again.

## Rules that keep you compliant

- An **unsubscribe at the provider always wins**: the contact is opted out in Odoo.
- An **opt-out or blacklist in Odoo** is sent to the provider.
- Odoo never subscribes someone again at the provider. If a contact who left is ticked back in within Odoo, they are listed
  under *Need attention* and must use the provider's sign-up form themselves.
- A contact who signs up again at the provider after opting out in Odoo is subscribed again in Odoo, because that is the
  newer decision.
- Contacts who already left and are not in Odoo are not created.
- Deleting a contact or a list in Odoo never deletes anything at the provider.

## What you see

- The lists page shows how many contacts are on each side and the last problem of each list.
- **Need attention** opens the contacts the provider refused (for example a fake address), with the provider's reason. They
  are retried when the contact is edited.
- **Campaign results** show sent, opened, clicked, unsubscribed and bounced per campaign sent from the provider.
- **Instant updates** (a webhook) bring unsubscribes in within seconds when Odoo is reachable over https; otherwise the
  15 minute run does it.
- A rejected API key disconnects the account and says how to fix it. A busy provider only leaves a message and is retried.

## For developers

A provider adds a `provider` value to `marketing.account` and implements `_<provider>_test_connection`, `_fetch_lists`,
`_fetch_members(link, since)`, `_push_members(link, items)` and optionally `_fetch_member`, `_fetch_campaigns(since)`,
`_register_webhooks`, `_parse_webhook(body, form)` and `_connect_help`. They exchange small neutral dictionaries; the base
module does all the Odoo side. Raise `MarketingError` (plain-language message) or `MarketingAuthError`.
