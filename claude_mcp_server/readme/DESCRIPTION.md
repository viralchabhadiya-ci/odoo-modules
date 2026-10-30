# Claude MCP Server

Lets Claude (Claude Code, Claude Desktop, or any program that speaks the Model Context Protocol) look into your Odoo
database and, only if you allow it, change it. Claude asks questions such as "which customers have overdue invoices over
5,000?" or "create a lead for Acme" and Odoo answers as a normal Odoo user, with that user's access rights and record rules.

## Connect

1. **Claude > Access keys > New.** Name it (e.g. "Claude Desktop of Maria") and choose the Odoo user it acts as.
2. List the models Claude may use, or tick *May read any model the user can read*. Optionally narrow each model with a filter
   (for example only the sales team's orders).
3. **Create access key.** Odoo shows the key once, with the ready-made command for Claude Code
   (`claude mcp add --transport http odoo <address> --header "Authorization: Bearer <key>"`) and the configuration for
   Claude Desktop (through `mcp-remote`). Odoo keeps only a fingerprint of the key.

## What Claude can do

| Tool | Needs |
|---|---|
| `odoo_whoami`, `odoo_list_models`, `odoo_describe_model` | a key |
| `odoo_search_read`, `odoo_read`, `odoo_count`, `odoo_group` (totals per customer, per month ...) | the model is listed (or read-all) |
| `odoo_create`, `odoo_write`, `odoo_delete` | key is not read only, and *Create / Change / Delete* ticked for that model |
| `odoo_run_action` (press a button such as Confirm) | key is not read only, *May run actions*, and *Change* on the model |

A key starts **read only**. Claude is only shown the tools its key can use.

## Safety

- Claude acts as the chosen user: Odoo's access rights and record rules always apply on top of the key.
- Models that hold credentials or settings (`ir.*`, users and groups, access keys, payment providers, outgoing mail ...)
  can never be opened, and fields called password, token, secret or api key are never returned.
- Every call is written to the **Audit log** (who, which tool, which model, the request in short, success or the reason it was
  refused). Entries are deleted after 90 days.
- Keys expire on a date you choose, have a daily call limit (2000 by default), can be replaced (the old key stops at once) or
  revoked, and show when they were last used.
- A change that fails leaves nothing half done. Action methods starting with `_` and the generic ORM methods
  (`write`, `unlink`, `sudo` ...) cannot be run.

## Notes

- Odoo must be reachable from where Claude runs. Claude Desktop needs Node.js for `mcp-remote`.
- claude.ai *custom connectors* ask for OAuth sign-in, which this version does not offer; use Claude Code, Claude Desktop or the
  Claude API's MCP connector with the bearer key.
