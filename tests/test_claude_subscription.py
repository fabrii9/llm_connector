# -*- coding: utf-8 -*-

import json
import os
import subprocess
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import new_test_user, tagged
from odoo.tests.common import TransactionCase

from ..models.llm_provider import ClaudeSubscriptionError
from ..services import claude_cli

TOKEN = "sk-ant-oat01-test-token-0123456789"


def _completed(payload, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=json.dumps(payload), stderr=stderr)


OK_PAYLOAD = {
    "type": "result", "subtype": "success", "is_error": False,
    "result": "OK", "num_turns": 1, "total_cost_usd": 0.001,
    "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 5},
}


@tagged("post_install", "-at_install")
class TestClaudeSubscription(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Provider = cls.env["llm.provider"]
        cls.provider = cls.Provider.create({
            "name": "Claude test",
            "provider_type": "claude_subscription",
            "model": "sonnet",
            "timeout": 300,
            "sequence": 1,
            "claude_oauth_token_input": TOKEN,
        })

    def _run(self, payload=OK_PAYLOAD, env=None, **kwargs):
        env = {"PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "sk-ant-api-should-not-pass",
               "CLAUDECODE": "1", **(env or {})}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(claude_cli, "find_claude", return_value="/usr/local/bin/claude"), \
                patch.object(claude_cli.subprocess, "run", return_value=_completed(payload)) as run:
            result = self.provider.claude_subscription_run("Hola", **kwargs)
        return result, run.call_args

    def test_token_is_write_only(self):
        self.assertTrue(self.provider.claude_token_set)
        self.assertFalse(self.provider.claude_oauth_token_input)
        self.assertEqual(self.provider.claude_oauth_token, TOKEN)
        saved = self.provider.web_save(
            {"claude_oauth_token_input": "sk-ant-oat01-other-token-000000"},
            {"claude_oauth_token_input": {}, "claude_token_set": {}})
        self.assertFalse(saved[0]["claude_oauth_token_input"])
        self.assertEqual(self.provider.claude_oauth_token, "sk-ant-oat01-other-token-000000")
        with self.assertRaises(ValidationError):
            self.provider.claude_oauth_token_input = "sk-proj-not-a-claude-token"

    def test_command_and_environment(self):
        payload = dict(OK_PAYLOAD, structured_output={"ok": True})
        result, call = self._run(payload, tools="web", json_schema={"type": "object"})
        command, env = call.args[0], call.kwargs["env"]
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDECODE", env)
        self.assertTrue(env["HOME"].endswith(os.path.join("llm_connector", "claude_home")))
        for flag in ("-p", "--no-session-persistence", "--strict-mcp-config", "--json-schema"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        self.assertEqual(command[command.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(command[command.index("--model") + 1], "sonnet")
        self.assertEqual(result["session_source"], "odoo")
        self.assertEqual(result["input_tokens"], 15)
        self.assertEqual(result["num_turns"], 1)

    def test_env_token_fallback(self):
        self.provider.action_clear_claude_token()
        result, call = self._run(env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-from-env"})
        self.assertEqual(call.kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-from-env")
        self.assertEqual(result["session_source"], "entorno")

    def test_without_any_token(self):
        self.provider.action_clear_claude_token()
        with self.assertRaises(ClaudeSubscriptionError):
            self._run()

    def test_structured_output(self):
        payload = dict(OK_PAYLOAD, result="", structured_output={"a": 1})
        result, _call = self._run(payload, json_schema={"type": "object"})
        self.assertEqual(result["data"], {"a": 1})

    def test_usage_limit_flag(self):
        payload = dict(OK_PAYLOAD, is_error=True, subtype="error_during_execution",
                       result="Claude AI usage limit reached|1700000000")
        with self.assertRaises(ClaudeSubscriptionError) as ctx:
            self._run(payload)
        self.assertTrue(ctx.exception.limit)

    def test_dangerous_tools_rejected(self):
        with self.assertRaises(ClaudeSubscriptionError):
            self._run(tools="Bash")

    def test_non_claude_model_falls_back(self):
        _result, call = self._run(model="gpt-4o")
        command = call.args[0]
        self.assertEqual(command[command.index("--model") + 1], "sonnet")

    def test_chat_completion_and_generate(self):
        env = {"PATH": "/usr/bin:/bin"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(claude_cli, "find_claude", return_value="/usr/local/bin/claude"), \
                patch.object(claude_cli.subprocess, "run", return_value=_completed(OK_PAYLOAD)) as run:
            text = self.provider.generate("Hola", system="Sos breve.")
            self.assertEqual(text, "OK")
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--system-prompt") + 1], "Sos breve.")
            self.provider.chat_completion([
                {"role": "user", "content": [{"type": "text", "text": "Mirá"},
                                             {"type": "image_url", "image_url": {"url": "x"}}]},
                {"role": "assistant", "content": "Listo"},
                {"role": "user", "content": "¿Y?"},
            ])
            self.assertIn("Usuario: ¿Y?", run.call_args.kwargs["input"])

    def test_get_provider_compatibility(self):
        # Sin tipo: sigue devolviendo solo proveedores con api_key.
        found = self.Provider.get_provider()
        self.assertNotEqual(found, self.provider)
        self.assertEqual(self.Provider.get_provider("claude_subscription"), self.provider)

    def test_access_requires_manager_or_sudo(self):
        user = new_test_user(self.env, login="llm_plain_user", groups="base.group_user")
        with self.assertRaises(AccessError):
            self.Provider.with_user(user).claude_subscription_run("Hola")
