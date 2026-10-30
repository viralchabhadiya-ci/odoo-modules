import logging

from odoo import api, http
from odoo.http import request
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

HEADERS = {"OData-Version": "4.0", "Cache-Control": "no-store"}


def answer(status, content_type="application/json", body="", extra=None):
    headers = {"Content-Type": content_type, **HEADERS, **(extra or {})}
    return request.make_response(body, headers=list(headers.items()), status=status)


class PowerBIOData(http.Controller):
    @http.route(
        [
            "/powerbi/<string:db>/odata/<int:feed_id>",
            "/powerbi/<string:db>/odata/<int:feed_id>/<path:path>",
        ],
        type="http",
        auth="none",
        methods=["GET", "HEAD"],
        csrf=False,
        save_session=False,
        readonly=False,  # the feed notes when it was last used
    )
    def odata(self, db, feed_id, path="", **_kwargs):
        try:
            registry = Registry(db)
        except Exception:
            return answer(404)
        with registry.cursor() as cr:
            env = api.Environment(cr, api.SUPERUSER_ID, {})
            feed = env["powerbi.feed"]._authenticate(feed_id, request.httprequest.headers.get("Authorization"))
            if not feed:
                return answer(401, extra={"WWW-Authenticate": 'Basic realm="Odoo Power BI feed"'})
            base = f"{request.httprequest.host_url.rstrip('/')}/powerbi/{db}/odata/{feed_id}"
            args = {key: value for key, value in request.httprequest.args.items()}
            try:
                status, content_type, body = feed._odata(path, args, base)
            except Exception:
                _logger.exception("Power BI feed %s failed", feed_id)
                return answer(
                    500,
                    "application/json",
                    '{"error": {"code": "500", "message": "The feed failed, see the Odoo log."}}',
                )
            feed._touch()
        return answer(status, content_type, body)
