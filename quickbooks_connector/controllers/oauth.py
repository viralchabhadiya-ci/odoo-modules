import logging

from odoo import http
from odoo.http import request

from ..models.quickbooks_error import QuickBooksError

_logger = logging.getLogger(__name__)


class QuickBooksOAuth(http.Controller):
    @http.route("/quickbooks/oauth/callback", type="http", auth="user", methods=["GET"], readonly=False)
    def callback(self, code=None, realmId=None, state=None, error=None, **_kwargs):
        """Where Intuit sends the user back after they allowed (or refused) the connection."""
        account = request.env["quickbooks.account"]._from_state(state)
        if not account or not request.env.user.has_group("quickbooks_connector.group_quickbooks_manager"):
            return request.make_response("This sign-in link is not valid. Start again from Odoo.", status=400)
        account = account.sudo()
        if error or not code or not realmId:
            account.write(
                {"state": "error", "connection_message": f"QuickBooks was not allowed: {error or 'no code received'}"}
            )
        else:
            try:
                account._complete_oauth(code, realmId)
            except QuickBooksError as exc:
                _logger.info("QuickBooks sign-in failed", exc_info=True)
                account.write({"state": "error", "connection_message": str(exc)})
        return request.redirect(f"/odoo/action-quickbooks_connector.quickbooks_account_action/{account.id}")
