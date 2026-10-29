import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class ChannelWebhook(http.Controller):
    @http.route(
        "/channel/<string:channel_type>/webhook/<int:store_id>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def webhook(self, channel_type, store_id, **_kwargs):
        store = request.env["channel.store"].sudo().browse(store_id).exists()
        body = request.httprequest.get_data()
        headers = request.httprequest.headers
        if not store or store.channel_type != channel_type or not store._verify_webhook(body, headers):
            return request.make_response("", status=401)
        try:
            for job_type, name, payload, dedupe_key in store._parse_webhook(body, headers):
                store._queue_job(job_type, name, payload, dedupe_key=dedupe_key)
        except Exception:
            _logger.exception("Could not queue webhook for store %s", store_id)
            return request.make_response("", status=500)
        return request.make_response("", status=200)
