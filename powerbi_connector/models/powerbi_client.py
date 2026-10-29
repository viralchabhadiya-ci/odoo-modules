import logging
import time

import requests

_logger = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com"
API = "https://api.powerbi.com/v1.0/myorg"
SCOPE = "https://analysis.windows.net/powerbi/api/.default"
TIMEOUT = 120
RETRIES = 2


class PowerBIError(Exception):
    """A problem the user can fix. The message is shown as it is."""


class PowerBIClient:
    """The Power BI REST API with an Azure AD app (service principal): client credentials, no extra library."""

    def __init__(self, feed):
        self.env = feed.env
        feed = feed.sudo()
        self.tenant, self.client_id, self.secret = feed.pbi_tenant_id, feed.pbi_client_id, feed.pbi_client_secret
        self.workspace = (feed.pbi_workspace_id or "").strip()
        if not (self.tenant and self.client_id and self.secret and self.workspace):
            raise PowerBIError(self.env._("Fill in the tenant ID, application ID, secret and workspace ID first."))
        self._token = None

    def token(self, force=False):
        if self._token and not force:
            return self._token
        try:
            response = requests.post(
                f"{AUTHORITY}/{self.tenant.strip()}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id.strip(),
                    "client_secret": self.secret,
                    "scope": SCOPE,
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise PowerBIError(self.env._("Could not reach Microsoft sign-in: %s", exc)) from exc
        if response.status_code != 200:
            try:
                reason = (response.json().get("error_description") or "").splitlines()[0]
            except (ValueError, AttributeError, IndexError):
                reason = response.text[:200]
            raise PowerBIError(
                self.env._("Microsoft refused the sign-in: %s Check the tenant ID, application ID and secret.", reason)
            )
        self._token = response.json()["access_token"]
        return self._token

    def request(self, method, path, json=None):
        url = f"{API}/groups/{self.workspace}/{path.lstrip('/')}"
        refreshed = False
        for attempt in range(RETRIES + 1):
            try:
                response = requests.request(
                    method, url, json=json, headers={"Authorization": f"Bearer {self.token()}"}, timeout=TIMEOUT
                )
            except requests.RequestException as exc:
                raise PowerBIError(self.env._("Could not reach Power BI: %s", exc)) from exc
            if response.status_code == 401 and not refreshed:
                refreshed = True
                self.token(force=True)
                continue
            if response.status_code in (429, 502, 503, 504) and attempt < RETRIES:
                time.sleep(min(int(response.headers.get("Retry-After") or 0), 30) or 2 * (attempt + 1))
                continue
            break
        self._check(response)
        return response.json() if response.content else {}

    def _check(self, response):
        code = response.status_code
        if code < 400:
            return
        try:
            error = response.json().get("error") or {}
            message = error.get("message") or error.get("code") or response.text[:200]
        except (ValueError, AttributeError):
            message = response.text[:200]
        if code in (401, 403):
            raise PowerBIError(
                self.env._(
                    "Power BI refused access (%s). The Azure app must be allowed in the Power BI admin portal "
                    "(Tenant settings > Developer settings > Service principals can use Power BI APIs) and added to "
                    "the workspace as a Member or Contributor.",
                    message,
                )
            )
        if code == 404:
            raise PowerBIError(self.env._("Power BI could not find it (%s). Check the workspace ID.", message))
        if code == 429:
            raise PowerBIError(self.env._("Power BI says there are too many requests. Odoo tries again later."))
        raise PowerBIError(self.env._("Power BI answered %(code)s: %(message)s", code=code, message=message))

    # -- datasets ---------------------------------------------------------------------------

    def create_dataset(self, name, tables):
        return self.request(
            "POST", "datasets?defaultRetentionPolicy=None", {"name": name, "defaultMode": "Push", "tables": tables}
        )["id"]

    def delete_dataset(self, dataset_id):
        self.request("DELETE", f"datasets/{dataset_id}")

    def update_table(self, dataset_id, table):
        self.request("PUT", f"datasets/{dataset_id}/tables/{table['name']}", table)

    def clear_table(self, dataset_id, table):
        self.request("DELETE", f"datasets/{dataset_id}/tables/{table}/rows")

    def add_rows(self, dataset_id, table, rows):
        self.request("POST", f"datasets/{dataset_id}/tables/{table}/rows", {"rows": rows})
