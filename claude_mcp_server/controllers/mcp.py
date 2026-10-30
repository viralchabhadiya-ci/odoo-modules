import json
import logging

from odoo import api, http
from odoo.http import request
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


def answer(status, body=None, extra=None):
    headers = {"Cache-Control": "no-store", **(extra or {})}
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body, ensure_ascii=False)
    return request.make_response(body or "", headers=list(headers.items()), status=status)


def rpc_error(code, message):
    return {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}


class ClaudeMcp(http.Controller):
    @http.route(
        "/mcp/<string:db>",
        type="http",
        auth="none",
        methods=["GET", "POST", "DELETE"],
        csrf=False,
        save_session=False,
        readonly=False,  # every call is written to the audit log
    )
    def mcp(self, db, **_kwargs):
        if request.httprequest.method != "POST":  # no server-sent stream and no sessions to end
            return answer(405, extra={"Allow": "POST"})
        try:
            registry = Registry(db)
        except Exception:
            return answer(404)
        with registry.cursor() as cr:
            env = api.Environment(cr, api.SUPERUSER_ID, {})
            token = env["claude.mcp.token"]._authenticate(request.httprequest.headers.get("Authorization"))
            if not token:
                return answer(401, extra={"WWW-Authenticate": 'Bearer realm="Odoo Claude MCP"'})
            try:
                body = json.loads(request.httprequest.get_data(as_text=True))
            except ValueError:
                return answer(400, rpc_error(-32700, "The request is not valid JSON."))
            try:
                status, reply = env["claude.mcp.server"].handle(token, body)
            except Exception:
                _logger.exception("Claude MCP request failed")
                return answer(500, rpc_error(-32603, "Odoo failed to handle the request, see the Odoo log."))
        return answer(status, reply)
