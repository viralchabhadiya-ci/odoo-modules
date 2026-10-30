import hmac
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class MarketingWebhook(http.Controller):
    @http.route(
        "/marketing/<string:provider>/webhook/<int:account_id>/<string:token>",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
    )
    def webhook(self, provider, account_id, token, **_kwargs):
        account = request.env["marketing.account"].sudo().browse(account_id).exists()
        if not account or account.provider != provider or not hmac.compare_digest(account.webhook_token or "", token):
            return request.make_response("", status=401)
        if request.httprequest.method == "POST":  # Mailchimp checks the address with a GET when it is registered
            try:
                account._handle_webhook(request.httprequest.get_data(), request.httprequest.form)
            except Exception:
                _logger.exception("Could not handle webhook of marketing account %s", account_id)
                return request.make_response("", status=500)
        return request.make_response("", status=200)
