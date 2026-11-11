# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


class _Resp:
    def __init__(self, data):
        self._data = data
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


@tagged("post_install", "-at_install")
class TestAnthropicPayload(TransactionCase):

    def setUp(self):
        super().setUp()
        self.provider = self.env["llm.provider"].create({
            "name": "Claude test", "provider_type": "anthropic",
            "base_url": "https://api.anthropic.com", "api_key": "sk-ant-api-test",
            "model": "claude-opus-5-5", "max_tokens": 1024, "temperature": 0.7,
        })

    def _call(self, response, **kwargs):
        calls = []

        def fake(method, url, **kw):
            calls.append((method, url, kw))
            return _Resp(response)
        with patch("odoo.addons.llm_connector.models.llm_provider.requests.request", side_effect=fake):
            out = self.provider.chat_completion(
                [{"role": "system", "content": "Sos breve."}, {"role": "user", "content": "Hola"}], **kwargs)
        return out, calls

    def test_body_actual(self):
        out, calls = self._call({"content": [{"type": "thinking", "thinking": ""},
                                             {"type": "text", "text": "Hola!"}],
                                 "stop_reason": "end_turn"})
        self.assertEqual(out, "Hola!")
        method, url, kw = calls[0]
        body, headers = kw["json"], kw["headers"]
        self.assertTrue(url.endswith("/v1/messages"))
        self.assertNotIn("temperature", body)
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["max_tokens"], 16000)
        self.assertEqual(body["system"], "Sos breve.")
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(headers["anthropic-beta"], "server-side-fallback-2026-07-01")
        self.assertEqual(headers["x-api-key"], "sk-ant-api-test")

    def test_alias_y_haiku_sin_fallback(self):
        _out, calls = self._call({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"},
                                 model="haiku", temperature=0.2, extra={"top_p": 0.9})
        body, headers = calls[0][2]["json"], calls[0][2]["headers"]
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertNotIn("fallbacks", body)
        self.assertNotIn("anthropic-beta", headers)
        self.assertNotIn("temperature", body)
        self.assertNotIn("top_p", body)

    def test_rechazo(self):
        with self.assertRaises(UserError):
            self._call({"content": [], "stop_reason": "refusal", "stop_details": {"category": "cyber"}})

    def test_sin_texto_por_max_tokens(self):
        with self.assertRaises(UserError):
            self._call({"content": [{"type": "thinking", "thinking": ""}], "stop_reason": "max_tokens"})
