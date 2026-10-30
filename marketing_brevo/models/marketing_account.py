import json
from datetime import datetime, timezone

from odoo import fields, models, tools

from .brevo_client import BrevoClient, BrevoNotFound, contact_path

REMOVE_CHUNK = 150  # emails per "remove from list" call
CONTACT_PAGE = 500
CAMPAIGN_PAGE = 100
WEBHOOK_EVENTS = ["unsubscribed", "listAddition", "contactUpdated"]


def parse_time(value):
    """'2026-10-29T10:00:00.000+02:00' -> naive UTC datetime, as Odoo stores it."""
    if not value:
        return False
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def iso_time(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def normalize_contact(contact):
    attributes = contact.get("attributes") or {}
    return {
        "email": contact.get("email"),
        "first_name": attributes.get("FIRSTNAME") or "",
        "last_name": attributes.get("LASTNAME") or "",
        # Brevo calls an unsubscribe "blacklisted": the contact is kept but never mailed again
        "subscribed": not contact.get("emailBlacklisted"),
        "remote_id": str(contact["id"]) if contact.get("id") else False,
        "updated": parse_time(contact.get("modifiedAt")),
    }


def normalize_campaign(campaign, list_names):
    stats = (campaign.get("statistics") or {}).get("globalStats") or {}
    lists = (campaign.get("recipients") or {}).get("lists") or []
    return {
        "id": campaign["id"],
        "name": campaign.get("name") or campaign.get("subject") or str(campaign["id"]),
        "subject": campaign.get("subject"),
        "list_name": ", ".join(list_names.get(str(i), "") for i in lists if str(i) in list_names),
        "sent_date": parse_time(campaign.get("sentDate")),
        "emails_sent": stats.get("sent"),
        "opens": stats.get("uniqueViews"),
        "clicks": stats.get("uniqueClicks"),
        "unsubscribes": stats.get("unsubscriptions"),
        "bounces": (stats.get("hardBounces") or 0) + (stats.get("softBounces") or 0),
    }


class MarketingAccount(models.Model):
    _inherit = "marketing.account"

    provider = fields.Selection(selection_add=[("brevo", "Brevo")], ondelete={"brevo": "cascade"})

    def _brevo_connect_help(self):
        return self.env._(
            "1. In Brevo open your name (top right) > SMTP & API > API keys.\n"
            "2. Click Generate a new API key and paste it above.\n"
            "3. If you restrict IP addresses (Security > Authorised IPs), add the address of this Odoo server.\n"
            "4. Click Connect, then tick the lists to keep in sync."
        )

    def _brevo_test_connection(self):
        account = BrevoClient(self).request("GET", "account")
        return account.get("companyName") or account.get("email") or "Brevo"

    def _brevo_fetch_lists(self):
        for item in BrevoClient(self).paginate("contacts/lists", "lists"):
            yield {"id": item["id"], "name": item["name"], "count": item.get("uniqueSubscribers")}

    def _brevo_fetch_members(self, link, since):
        params = {"sort": "asc"}
        if since:
            params["modifiedSince"] = iso_time(since)
        for contact in BrevoClient(self).paginate(
            f"contacts/lists/{link.remote_id}/contacts", "contacts", params, page=CONTACT_PAGE
        ):
            yield normalize_contact(contact)

    def _brevo_fetch_member(self, link, email):
        """Blacklisted anywhere counts as unsubscribed here. A contact that is merely not on this list is left
        alone: it may just not have been sent yet."""
        try:
            contact = BrevoClient(self).request("GET", contact_path(email))
        except BrevoNotFound:
            return None
        if contact.get("emailBlacklisted") or int(link.remote_id) in (contact.get("listIds") or []):
            return normalize_contact(contact)
        return None

    def _brevo_push_members(self, link, items):
        """Subscribed contacts are imported in one call. Someone who opted out of this list is taken off it; someone
        blacklisted in Odoo is blacklisted at Brevo too, so no campaign reaches them from any list."""
        client = BrevoClient(self)
        errors = {}
        subscribed = [item for item in items if item["subscribed"]]
        if subscribed:
            rows = []
            for item in subscribed:
                attributes = {"FIRSTNAME": item["first_name"], "LASTNAME": item["last_name"]}
                rows.append({"email": item["email"], "attributes": {k: v for k, v in attributes.items() if v}})
            client.request(
                "POST",
                "contacts/import",
                json={
                    "jsonBody": rows,
                    "listIds": [int(link.remote_id)],
                    "updateExistingContacts": True,
                    "emptyContactsAttributes": False,
                },
            )
        left = [item for item in items if not item["subscribed"]]
        for chunk in tools.split_every(REMOVE_CHUNK, [item["email"] for item in left]):
            result = client.request(
                "POST", f"contacts/lists/{link.remote_id}/contacts/remove", json={"emails": list(chunk)}
            )
            for email in ((result or {}).get("contacts") or {}).get("failure") or []:
                errors[email.lower()] = self.env._("Brevo could not take this address off the list.")
        for item in left:
            if item.get("blacklisted"):
                try:
                    client.request("PUT", contact_path(item["email"]), json={"emailBlacklisted": True})
                except BrevoNotFound:
                    pass  # not a Brevo contact, nothing to blacklist
        return errors

    def _brevo_fetch_campaigns(self, since):
        list_names = {link.remote_id: link.name for link in self.link_ids}
        params = {
            "status": "sent",
            "statistics": "globalStats",
            "startDate": iso_time(since),
            "endDate": iso_time(datetime.now(timezone.utc)),
            "sort": "desc",
        }
        for campaign in BrevoClient(self).paginate("emailCampaigns", "campaigns", params, page=CAMPAIGN_PAGE):
            yield normalize_campaign(campaign, list_names)

    def _brevo_register_webhooks(self):
        """One account-wide webhook for contact events."""
        if self.webhooks_registered:
            return
        BrevoClient(self).request(
            "POST",
            "webhooks",
            json={
                "url": self.webhook_url,
                "description": "Odoo",
                "type": "marketing",
                "events": WEBHOOK_EVENTS,
            },
        )
        self.webhooks_registered = True

    def _brevo_parse_webhook(self, body, form):
        """Brevo posts JSON with the contact's address; it names no list, so every synced list is refreshed."""
        try:
            email = (json.loads(body or b"{}") or {}).get("email")
        except ValueError:
            return []
        return [(None, email)] if email else []
