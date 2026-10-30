import json
from datetime import date, timedelta
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools import mute_logger

from ..models.mcp_token import hash_token, is_blocked, is_sensitive, token_from_header


class McpCase:
    def make_token(self, models=("res.partner",), **vals):
        vals.setdefault("user_id", self.env.ref("base.user_admin").id)
        token = self.env["claude.mcp.token"].create({"name": "Maria's Claude", **vals})
        for name in models:
            self.env["claude.mcp.access"].create({"token_id": token.id, "model_id": self.env["ir.model"]._get(name).id})
        self.secret = token.action_new_token()["context"]["default_token"]
        return token

    def rpc(self, token, method, params=None, id=1):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if id is not None:
            message["id"] = id
        return self.env["claude.mcp.server"].handle(token, message)

    def call(self, token, tool, **arguments):
        _status, answer = self.rpc(token, "tools/call", {"name": tool, "arguments": arguments})
        result = answer["result"]
        text = result["content"][0]["text"]
        return result, (text if result.get("isError") else json.loads(text))

    def partner(self, name, **vals):
        return self.env["res.partner"].create({"name": name, **vals})


@tagged("post_install", "-at_install")
class TestMcpProtocol(McpCase, TransactionCase):
    def test_initialize_answers_with_a_known_protocol_version(self):
        token = self.make_token()
        _status, answer = self.rpc(token, "initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(answer["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", answer["result"]["capabilities"])
        _status, answer = self.rpc(token, "initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(answer["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(answer["id"], 1)

    def test_notifications_get_no_answer(self):
        token = self.make_token()
        self.assertEqual(self.rpc(token, "notifications/initialized", id=None), (202, None))

    def test_ping_and_unknown_methods(self):
        token = self.make_token()
        self.assertEqual(self.rpc(token, "ping")[1]["result"], {})
        self.assertEqual(self.rpc(token, "resources/list")[1]["error"]["code"], -32601)

    def test_malformed_messages_are_refused(self):
        token = self.make_token()
        server = self.env["claude.mcp.server"]
        self.assertEqual(server.handle(token, {"method": "ping", "id": 1})[1]["error"]["code"], -32600)
        self.assertEqual(server.handle(token, "ping")[1]["error"]["code"], -32600)
        _status, answer = server.handle(token, {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": [1]})
        self.assertEqual(answer["error"]["code"], -32602)

    def test_batches_answer_only_requests(self):
        token = self.make_token()
        status, answers = self.env["claude.mcp.server"].handle(
            token,
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            ],
        )
        self.assertEqual((status, [a["id"] for a in answers]), (200, [1, 2]))

    def test_read_only_key_is_offered_only_reading_tools(self):
        token = self.make_token()
        tools = {t["name"] for t in self.rpc(token, "tools/list")[1]["result"]["tools"]}
        self.assertEqual(
            tools,
            {
                "odoo_whoami",
                "odoo_list_models",
                "odoo_describe_model",
                "odoo_search_read",
                "odoo_count",
                "odoo_read",
                "odoo_group",
            },
        )

    def test_a_key_that_may_change_things_is_offered_only_what_it_can_use(self):
        token = self.make_token(read_only=False)
        token.access_ids.write({"can_create": True})
        tools = {t["name"] for t in self.rpc(token, "tools/list")[1]["result"]["tools"]}
        self.assertIn("odoo_create", tools)
        self.assertNotIn("odoo_write", tools)
        self.assertNotIn("odoo_delete", tools)
        self.assertNotIn("odoo_run_action", tools)
        token.write({"allow_actions": True})
        token.access_ids.write({"can_write": True})
        tools = {t["name"] for t in self.rpc(token, "tools/list")[1]["result"]["tools"]}
        self.assertIn("odoo_run_action", tools)

    def test_unknown_tool_and_unknown_argument_are_reported_not_raised(self):
        token = self.make_token()
        result, text = self.call(token, "odoo_hack")
        self.assertTrue(result["isError"])
        self.assertIn("Unknown tool", text)
        result, text = self.call(token, "odoo_count", model="res.partner", nope=1)
        self.assertTrue(result["isError"])
        self.assertIn("Unknown argument 'nope'", text)
        result, text = self.call(token, "odoo_count")
        self.assertTrue(result["isError"])
        self.assertIn("arguments do not fit", text)

    def test_every_call_is_logged(self):
        token = self.make_token()
        self.call(token, "odoo_count", model="res.partner")
        self.call(token, "odoo_count", model="res.users")
        ok, refused = token.log_ids.sorted("id")
        self.assertTrue(ok.success)
        self.assertEqual((ok.tool, ok.model_name), ("odoo_count", "res.partner"))
        self.assertFalse(refused.success)
        self.assertIn("may not read res.users", refused.error)
        self.assertTrue(token.last_used)

    def test_daily_limit(self):
        token = self.make_token(daily_limit=2)
        self.call(token, "odoo_whoami")
        self.call(token, "odoo_whoami")
        result, text = self.call(token, "odoo_whoami")
        self.assertTrue(result["isError"])
        self.assertIn("daily limit", text)
        token.daily_limit = 0
        result, _text = self.call(token, "odoo_whoami")
        self.assertFalse(result.get("isError"))

    def test_old_log_entries_are_deleted(self):
        token = self.make_token()
        self.call(token, "odoo_whoami")
        old = self.env["claude.mcp.log"].create({"token_id": token.id, "tool": "odoo_whoami"})
        self.env.cr.execute(
            "UPDATE claude_mcp_log SET create_date = now() - interval '100 days' WHERE id = %s", [old.id]
        )
        self.env["claude.mcp.log"]._cron_cleanup()
        self.assertFalse(old.exists())
        self.assertEqual(len(token.log_ids), 1)

    def test_long_answers_are_cut(self):
        token = self.make_token()
        with patch("odoo.addons.claude_mcp_server.models.mcp_server.MAX_TEXT", 50):
            result, text = self.call_raw(token, "odoo_list_models")
        self.assertTrue(text.endswith("fewer rows or fields]"))
        self.assertNotIn("structuredContent", result)

    def call_raw(self, token, tool, **arguments):
        result = self.rpc(token, "tools/call", {"name": tool, "arguments": arguments})[1]["result"]
        return result, result["content"][0]["text"]


@tagged("post_install", "-at_install")
class TestMcpKeys(McpCase, TransactionCase):
    def test_helpers(self):
        self.assertEqual(token_from_header("Bearer abc"), "abc")
        self.assertEqual(token_from_header("bearer  abc "), "abc")
        self.assertEqual(token_from_header("Basic abc"), "")
        self.assertEqual(token_from_header(None), "")
        self.assertTrue(is_blocked("ir.config_parameter"))
        self.assertTrue(is_blocked("res.users.apikeys"))
        self.assertTrue(is_blocked("claude.mcp.token"))
        self.assertFalse(is_blocked("sale.order"))
        self.assertTrue(is_sensitive("signup_token"))
        self.assertTrue(is_sensitive("API_KEY"))
        self.assertFalse(is_sensitive("name"))

    def test_the_key_is_stored_as_a_fingerprint_and_shown_once(self):
        token = self.make_token()
        self.assertEqual(token.sudo().token_hash, hash_token(self.secret))
        self.assertNotIn(self.secret, token.token_hint)
        self.assertTrue(token.token_hint.endswith(self.secret[-4:]))
        action = token.action_new_token()
        context = action["context"]
        self.assertIn(token.url, context["default_command"])
        self.assertIn("claude mcp add", context["default_command"])
        config = json.loads(context["default_config"])
        self.assertIn("mcp-remote", config["mcpServers"][token.name]["args"])
        self.assertTrue(context["default_token"] != self.secret)

    def test_authentication(self):
        token = self.make_token()
        auth = self.env["claude.mcp.token"]._authenticate
        self.assertEqual(auth(f"Bearer {self.secret}"), token)
        self.assertFalse(auth(f"Bearer {self.secret}x"))
        self.assertFalse(auth("Bearer "))
        self.assertFalse(auth(None))
        token.token_expiry = date.today() - timedelta(days=1)
        self.assertFalse(auth(f"Bearer {self.secret}"))
        token.token_expiry = date.today()
        self.assertEqual(auth(f"Bearer {self.secret}"), token)
        token.active = False
        self.assertFalse(auth(f"Bearer {self.secret}"))
        token.active = True
        token.user_id.active = False
        self.assertFalse(auth(f"Bearer {self.secret}"))
        token.user_id.active = True
        token.action_revoke_token()
        self.assertFalse(auth(f"Bearer {self.secret}"))

    def test_replacing_the_key_ends_the_old_one(self):
        token = self.make_token()
        old = self.secret
        new = token.action_new_token()["context"]["default_token"]
        auth = self.env["claude.mcp.token"]._authenticate
        self.assertFalse(auth(f"Bearer {old}"))
        self.assertEqual(auth(f"Bearer {new}"), token)

    def test_internal_credentials_can_never_be_opened(self):
        token = self.make_token(models=())
        for name in ("ir.config_parameter", "res.users.apikeys", "claude.mcp.token", "payment.provider"):
            if name not in self.env:
                continue
            with self.assertRaises(ValidationError), self.env.cr.savepoint():
                self.env["claude.mcp.access"].create(
                    {"token_id": token.id, "model_id": self.env["ir.model"]._get(name).id}
                )

    def test_a_filter_must_be_valid(self):
        token = self.make_token()
        with self.assertRaises(ValidationError):
            token.access_ids.domain = "[('name', '=')"

    def test_a_model_is_listed_once_per_key(self):
        token = self.make_token()
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env["claude.mcp.access"].create(
                {"token_id": token.id, "model_id": self.env["ir.model"]._get("res.partner").id}
            )


@tagged("post_install", "-at_install")
class TestMcpTools(McpCase, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.alice = cls.env["res.partner"].create({"name": "MCP Alice", "email": "alice@x.com", "is_company": True})
        cls.bob = cls.env["res.partner"].create({"name": "MCP Bob", "email": "bob@x.com"})
        cls.carol = cls.env["res.partner"].create({"name": "MCP Carol", "email": "carol@x.com", "is_company": True})

    def test_whoami(self):
        token = self.make_token()
        _result, data = self.call(token, "odoo_whoami")
        self.assertEqual(data["login"], "admin")
        self.assertTrue(data["read_only"])
        self.assertFalse(data["may_run_actions"])
        self.assertEqual(data["key"], token.name)

    def test_list_models_only_shows_what_the_key_has(self):
        token = self.make_token()
        _result, data = self.call(token, "odoo_list_models")
        self.assertEqual([m["model"] for m in data["models"]], ["res.partner"])
        self.assertFalse(data["models"][0]["write"])
        token.access_all_models = True
        _result, data = self.call(token, "odoo_list_models")
        names = {m["model"] for m in data["models"]}
        self.assertIn("res.partner", names)
        self.assertIn("res.company", names)
        self.assertFalse({n for n in names if is_blocked(n)})

    def test_describe_model_hides_secrets_and_binary_fields(self):
        token = self.make_token()
        _result, data = self.call(token, "odoo_describe_model", model="res.partner")
        by_name = {f["name"]: f for f in data["fields"]}
        self.assertEqual(by_name["email"]["type"], "char")
        self.assertEqual(by_name["parent_id"]["relation"], "res.partner")
        self.assertNotIn("image_1920", by_name)
        self.assertFalse([n for n in by_name if is_sensitive(n)])

    def test_search_read(self):
        token = self.make_token()
        _result, data = self.call(
            token,
            "odoo_search_read",
            model="res.partner",
            domain=[["name", "like", "MCP "], ["is_company", "=", True]],
            fields=["name", "email"],
            order="name desc",
        )
        self.assertEqual(data["count"], 2)
        self.assertEqual([r["name"] for r in data["records"]], ["MCP Carol", "MCP Alice"])
        self.assertEqual(set(data["records"][0]), {"id", "name", "email"})

    def test_search_read_paging_and_default_fields(self):
        token = self.make_token()
        _result, data = self.call(
            token,
            "odoo_search_read",
            model="res.partner",
            domain=[["name", "like", "MCP "]],
            limit=1,
            offset=1,
            order="name",
        )
        self.assertEqual((data["count"], data["returned"]), (3, 1))
        self.assertEqual(data["records"][0]["name"], "MCP Bob")
        self.assertIn("email", data["records"][0])
        self.assertNotIn("message_ids", data["records"][0])

    def test_domains_may_be_json_text_and_use_or(self):
        token = self.make_token()
        domain = json.dumps(["|", ["name", "=", "MCP Alice"], ["name", "=", "MCP Bob"]])
        _result, data = self.call(token, "odoo_count", model="res.partner", domain=domain)
        self.assertEqual(data["count"], 2)

    def test_bad_input_is_explained(self):
        token = self.make_token()
        for arguments, fragment in [
            ({"model": "res.partner", "domain": "nope"}, "JSON list"),
            ({"model": "res.partner", "domain": [["name"]]}, "[field, operator, value]"),
            ({"model": "res.partner", "domain": [["nope", "=", 1]]}, "not valid"),
            ({"model": "res.partner", "fields": ["nope"]}, "no readable field 'nope'"),
            ({"model": "res.partner", "fields": ["image_1920"]}, "no readable field"),
            ({"model": "res.partner", "limit": -1}, "whole number"),
            ({"model": "no.such.model"}, "no model"),
        ]:
            result, text = self.call(token, "odoo_search_read", **arguments)
            self.assertTrue(result.get("isError"), arguments)
            self.assertIn(fragment, text)

    def test_limit_is_capped(self):
        token = self.make_token()
        _result, data = self.call(token, "odoo_search_read", model="res.partner", limit=100000, fields=["name"])
        self.assertLessEqual(data["returned"], 200)

    def test_read_by_id(self):
        token = self.make_token()
        _result, data = self.call(token, "odoo_read", model="res.partner", ids=[self.alice.id], fields=["name"])
        self.assertEqual(data["records"], [{"id": self.alice.id, "name": "MCP Alice"}])
        result, text = self.call(token, "odoo_read", model="res.partner", ids=[self.alice.id, 999999999])
        self.assertTrue(result["isError"])
        self.assertIn("999999999", text)
        result, text = self.call(token, "odoo_read", model="res.partner", ids=[])
        self.assertTrue(result["isError"])

    def test_group(self):
        token = self.make_token()
        _result, data = self.call(
            token,
            "odoo_group",
            model="res.partner",
            domain=[["name", "like", "MCP "]],
            group_by="is_company",
            measures=["id:count"],
        )
        counts = {row["group"]: row["id:count"] for row in data["rows"]}
        self.assertEqual(counts, {True: 2, False: 1})
        result, text = self.call(token, "odoo_group", model="res.partner", group_by="nope")
        self.assertTrue(result["isError"])

    def test_a_filter_on_the_key_limits_what_claude_sees_and_touches(self):
        token = self.make_token(read_only=False)
        token.access_ids.write({"can_write": True, "domain": "[('is_company', '=', True)]"})
        _result, data = self.call(
            token, "odoo_search_read", model="res.partner", domain=[["name", "like", "MCP "]], fields=["name"]
        )
        self.assertEqual({r["name"] for r in data["records"]}, {"MCP Alice", "MCP Carol"})
        result, _text = self.call(token, "odoo_read", model="res.partner", ids=[self.bob.id])
        self.assertTrue(result["isError"])
        result, _text = self.call(token, "odoo_write", model="res.partner", ids=[self.bob.id], values={"phone": "1"})
        self.assertTrue(result["isError"])
        self.assertFalse(self.bob.phone)

    def test_the_users_own_record_rules_still_apply(self):
        user = self.env["res.users"].create(
            {"name": "Limited", "login": "mcp_limited", "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]}
        )
        self.env["ir.rule"].create(
            {
                "name": "MCP test: only Alice",
                "model_id": self.env["ir.model"]._get("res.partner").id,
                "domain_force": "[('name', '=', 'MCP Alice')]",
                "groups": [(4, self.env.ref("base.group_user").id)],
            }
        )
        token = self.make_token(user_id=user.id)
        _result, data = self.call(
            token, "odoo_search_read", model="res.partner", domain=[["name", "like", "MCP "]], fields=["name"]
        )
        self.assertEqual([r["name"] for r in data["records"]], ["MCP Alice"])

    def test_read_only_key_cannot_change_anything(self):
        token = self.make_token()
        token.access_ids.write({"can_create": True, "can_write": True, "can_delete": True})
        for tool, arguments in [
            ("odoo_create", {"model": "res.partner", "values": {"name": "X"}}),
            ("odoo_write", {"model": "res.partner", "ids": [self.bob.id], "values": {"phone": "1"}}),
            ("odoo_delete", {"model": "res.partner", "ids": [self.bob.id]}),
        ]:
            result, text = self.call(token, tool, **arguments)
            self.assertTrue(result["isError"])
            self.assertIn("not allowed", text)
        self.assertTrue(self.bob.exists())

    def test_create_write_delete(self):
        token = self.make_token(read_only=False)
        token.access_ids.write({"can_create": True, "can_write": True})
        _result, created = self.call(
            token, "odoo_create", model="res.partner", values={"name": "MCP Dave", "email": "dave@x.com"}
        )
        dave = self.env["res.partner"].browse(created["id"])
        self.assertEqual(dave.email, "dave@x.com")
        _result, changed = self.call(token, "odoo_write", model="res.partner", ids=[dave.id], values={"phone": "123"})
        self.assertEqual(changed["changed"], [dave.id])
        self.assertEqual(dave.phone, "123")
        result, text = self.call(token, "odoo_delete", model="res.partner", ids=[dave.id])
        self.assertTrue(result["isError"])  # delete is not offered: "Delete" was not ticked
        token.access_ids.write({"can_delete": True})
        _result, deleted = self.call(token, "odoo_delete", model="res.partner", ids=[dave.id])
        self.assertEqual(deleted["deleted"], [dave.id])
        self.assertFalse(dave.exists())

    def test_unknown_or_forbidden_fields_cannot_be_written(self):
        token = self.make_token(read_only=False)
        token.access_ids.write({"can_write": True})
        for values in ({"nope": 1}, {"image_1920": "x"}, {}):
            result, _text = self.call(token, "odoo_write", model="res.partner", ids=[self.bob.id], values=values)
            self.assertTrue(result["isError"], values)

    def test_a_failed_change_leaves_nothing_behind(self):
        token = self.make_token(read_only=False)
        token.access_ids.write({"can_create": True})
        result, text = self.call(
            token, "odoo_create", model="res.partner", values={"name": "MCP Eve", "company_type": "nonsense"}
        )
        self.assertTrue(result["isError"])
        self.assertIn("Wrong value", text)
        self.assertFalse(self.env["res.partner"].search([("name", "=", "MCP Eve")]))

    def test_models_without_a_line_cannot_be_changed_even_if_readable(self):
        token = self.make_token(read_only=False, access_all_models=True)
        _result, data = self.call(token, "odoo_search_read", model="res.company", fields=["name"])
        self.assertTrue(data["records"])
        result, text = self.call(
            token, "odoo_write", model="res.company", ids=[self.env.company.id], values={"name": "Hacked"}
        )
        self.assertTrue(result["isError"])

    def test_blocked_models_stay_closed_even_with_access_to_everything(self):
        token = self.make_token(access_all_models=True)
        for name in ("res.users", "ir.config_parameter", "claude.mcp.token", "ir.model"):
            result, text = self.call(token, "odoo_search_read", model=name)
            self.assertTrue(result["isError"], name)
            self.assertIn("may not read", text)

    def test_run_action(self):
        token = self.make_token(read_only=False, allow_actions=True)
        token.access_ids.write({"can_write": True})
        model = type(self.env["res.partner"])
        with patch.object(model, "action_mcp_test", lambda records: records.write({"comment": "pressed"}), create=True):
            _result, data = self.call(
                token, "odoo_run_action", model="res.partner", ids=[self.bob.id], method="action_mcp_test"
            )
        self.assertTrue(data["done"])
        self.assertEqual(self.bob.comment, "<p>pressed</p>")

    def test_run_action_refuses_private_and_generic_methods(self):
        token = self.make_token(read_only=False, allow_actions=True)
        token.access_ids.write({"can_write": True})
        for method in (
            "_compute_display_name",
            "unlink",
            "write",
            "with_user",
            "sudo",
            "nothing_here",
            "action_archive",
        ):
            result, text = self.call(token, "odoo_run_action", model="res.partner", ids=[self.bob.id], method=method)
            self.assertTrue(result["isError"], method)
            self.assertIn("not a method Claude may run", text)
        self.assertTrue(self.bob.exists())
        self.assertTrue(self.bob.active)

    def test_actions_need_the_switch(self):
        token = self.make_token(read_only=False)
        token.access_ids.write({"can_write": True})
        result, text = self.call(
            token, "odoo_run_action", model="res.partner", ids=[self.bob.id], method="action_mcp_test"
        )
        self.assertTrue(result["isError"])
        self.assertIn("not allowed", text)


@tagged("post_install", "-at_install")
class TestMcpRoute(McpCase, HttpCase):
    def post(self, body, secret=None, raw=None):
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        data = raw if raw is not None else json.dumps(body)
        return self.url_open(f"/mcp/{self.env.cr.dbname}", data=data, headers=headers)

    def test_claude_can_talk_to_odoo_over_http(self):
        token = self.make_token()
        self.env["res.partner"].create({"name": "MCP Http Zoe", "email": "zoe@x.com"})
        self.env.cr.flush()

        refused = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(refused.status_code, 401)
        self.assertIn("Bearer", refused.headers["WWW-Authenticate"])
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, secret="wrong").status_code, 401)
        self.assertEqual(
            self.url_open(f"/mcp/{self.env.cr.dbname}", headers={"Authorization": f"Bearer {self.secret}"}).status_code,
            405,
        )
        self.assertEqual(
            self.url_open("/mcp/nodb", data="{}", headers={"Authorization": f"Bearer {self.secret}"}).status_code, 404
        )

        hello = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            self.secret,
        )
        self.assertEqual(hello.status_code, 200)
        self.assertIn("application/json", hello.headers["Content-Type"])
        self.assertEqual(hello.json()["result"]["serverInfo"]["name"], "odoo-claude-mcp")

        note = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.secret)
        self.assertEqual((note.status_code, note.text), (202, ""))

        answer = self.post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "odoo_search_read",
                    "arguments": {"model": "res.partner", "domain": [["email", "=", "zoe@x.com"]], "fields": ["name"]},
                },
            },
            self.secret,
        )
        self.assertEqual(answer.status_code, 200)
        result = answer.json()["result"]
        self.assertEqual(result["structuredContent"]["records"][0]["name"], "MCP Http Zoe")
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])

        self.assertEqual(self.post(None, self.secret, raw="{not json").status_code, 400)
        token.invalidate_recordset()
        self.assertTrue(token.sudo().last_used)
        self.assertEqual(self.env["claude.mcp.log"].search_count([("token_id", "=", token.id)]), 1)
