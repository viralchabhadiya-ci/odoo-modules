from datetime import datetime, timezone

from odoo import fields, models

from .mailchimp_client import MailchimpClient, MailchimpNotFound, subscriber_hash

SUBSCRIBED = "subscribed"
WEBHOOK_EVENTS = {
    "subscribe": True,
    "unsubscribe": True,
    "profile": True,
    "cleaned": True,
    "upemail": True,
    "campaign": False,
}
# `api: False` so what Odoo itself sends is not echoed back
WEBHOOK_SOURCES = {"user": True, "admin": True, "api": False}


def parse_time(value):
    """'2026-10-29T10:00:00+00:00' -> naive UTC datetime, as Odoo stores it."""
    if not value:
        return False
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def normalize_member(member):
    merge = member.get("merge_fields") or {}
    return {
        "email": member.get("email_address"),
        "first_name": merge.get("FNAME") or "",
        "last_name": merge.get("LNAME") or "",
        # pending = has not confirmed the double opt-in yet, so nothing may be sent from Odoo
        "subscribed": member.get("status") == SUBSCRIBED,
        "remote_id": member.get("id"),
        "updated": parse_time(member.get("last_changed")),
    }


def normalize_report(report):
    bounces = report.get("bounces") or {}
    return {
        "id": report["id"],
        "name": report.get("campaign_title") or report.get("subject_line") or report["id"],
        "subject": report.get("subject_line"),
        "list_name": report.get("list_name"),
        "sent_date": parse_time(report.get("send_time")),
        "emails_sent": report.get("emails_sent"),
        "opens": (report.get("opens") or {}).get("unique_opens"),
        "clicks": (report.get("clicks") or {}).get("unique_subscriber_clicks"),
        "unsubscribes": report.get("unsubscribed"),
        "bounces": (bounces.get("hard_bounces") or 0) + (bounces.get("soft_bounces") or 0),
    }


class MarketingAccount(models.Model):
    _inherit = "marketing.account"

    provider = fields.Selection(selection_add=[("mailchimp", "Mailchimp")], ondelete={"mailchimp": "cascade"})

    def _mailchimp_connect_help(self):
        return self.env._(
            "1. In Mailchimp open your profile > Extras > API keys.\n"
            "2. Click Create A Key and paste it above (it ends with a code such as -us21).\n"
            "3. Click Connect, then tick the audiences to keep in sync."
        )

    def _mailchimp_test_connection(self):
        return MailchimpClient(self).request("GET", "", {"fields": "account_name"}).get("account_name") or "Mailchimp"

    def _mailchimp_fetch_lists(self):
        params = {"fields": "lists.id,lists.name,lists.stats.member_count"}
        for item in MailchimpClient(self).paginate("lists", "lists", params):
            yield {"id": item["id"], "name": item["name"], "count": (item.get("stats") or {}).get("member_count")}

    def _mailchimp_fetch_members(self, link, since):
        params = {"fields": "members.id,members.email_address,members.status,members.merge_fields,members.last_changed"}
        if since:
            params["since_last_changed"] = since.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        for member in MailchimpClient(self).paginate(f"lists/{link.remote_id}/members", "members", params):
            yield normalize_member(member)

    def _mailchimp_fetch_member(self, link, email):
        try:
            member = MailchimpClient(self).request("GET", f"lists/{link.remote_id}/members/{subscriber_hash(email)}")
        except MailchimpNotFound:
            return None
        return normalize_member(member)

    def _mailchimp_push_members(self, link, items):
        """Batch upsert. Existing members keep their status unless they are unsubscribed in Odoo, so a contact who
        left Mailchimp is never subscribed again behind their back."""
        members = []
        for item in items:
            merge = {"FNAME": item["first_name"], "LNAME": item["last_name"]}
            member = {
                "email_address": item["email"],
                "status_if_new": SUBSCRIBED if item["subscribed"] else "unsubscribed",
                "merge_fields": {key: value for key, value in merge.items() if value},
            }
            if not item["subscribed"]:
                member["status"] = "unsubscribed"
            members.append(member)
        result = MailchimpClient(self).request(
            "POST",
            f"lists/{link.remote_id}",
            json={"members": members, "update_existing": True, "skip_merge_validation": True},
        )
        return {
            e["email_address"].lower(): e.get("error") or "Mailchimp refused this address"
            for e in result.get("errors") or []
        }

    def _mailchimp_fetch_campaigns(self, since):
        params = {
            "since_send_time": since.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "fields": "reports.id,reports.campaign_title,reports.subject_line,reports.list_name,reports.send_time,"
            "reports.emails_sent,reports.opens.unique_opens,reports.clicks.unique_subscriber_clicks,"
            "reports.unsubscribed,reports.bounces",
        }
        for report in MailchimpClient(self).paginate("reports", "reports", params):
            yield normalize_report(report)

    def _mailchimp_register_webhooks(self):
        """One webhook per synced audience (Mailchimp has no account-wide one)."""
        client = MailchimpClient(self)
        pending = self.link_ids.filtered(lambda link: link.sync and not link.missing and not link.webhook_ref)
        for link in pending:
            hook = client.request(
                "POST",
                f"lists/{link.remote_id}/webhooks",
                json={"url": self.webhook_url, "events": WEBHOOK_EVENTS, "sources": WEBHOOK_SOURCES},
            )
            link.webhook_ref = hook.get("id")
        self.webhooks_registered = bool(self.link_ids.filtered("webhook_ref"))

    def _mailchimp_parse_webhook(self, body, form):
        """Mailchimp posts a form: type, data[list_id], data[email] (data[new_email] after an address change)."""
        email = form.get("data[new_email]") or form.get("data[email]")
        return [(form.get("data[list_id]"), email)] if email else []
