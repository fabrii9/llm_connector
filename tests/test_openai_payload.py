# -*- coding: utf-8 -*-

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestOpenAIPayload(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["llm.provider"].create({
            "name": "OpenAI payload test",
            "provider_type": "openai",
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "model": "gpt-4o-mini",
            "temperature": 0.4,
            "max_tokens": 256,
        })
        cls.messages = [{"role": "user", "content": "Hola"}]

    def _payload(self, **kwargs):
        response = {"choices": [{"message": {"content": "ok"}}]}
        with patch.object(type(self.provider), "_request", return_value=response) as request:
            self.provider.chat_completion(self.messages, raw=True, **kwargs)
        return request.call_args.kwargs["json"]

    def test_gpt_5_uses_completion_limit_without_temperature(self):
        body = self._payload(
            model="gpt-5.6-terra",
            temperature=0.3,
            max_tokens=123,
        )
        self.assertEqual(body["max_completion_tokens"], 123)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

    def test_legacy_openai_keeps_max_tokens_and_temperature(self):
        body = self._payload(
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=123,
        )
        self.assertEqual(body["max_tokens"], 123)
        self.assertEqual(body["temperature"], 0.3)
        self.assertNotIn("max_completion_tokens", body)

    def test_compatible_provider_does_not_receive_openai_adaptation(self):
        self.provider.provider_type = "custom"
        body = self._payload(model="gpt-5.6-terra", max_tokens=123)
        self.assertEqual(body["max_tokens"], 123)
        self.assertIn("temperature", body)
        self.assertNotIn("max_completion_tokens", body)

    def test_model_prefix_requires_slug_boundary(self):
        for model in ("gpt-50", "o10"):
            with self.subTest(model=model):
                body = self._payload(model=model, max_tokens=123)
                self.assertEqual(body["max_tokens"], 123)
                self.assertIn("temperature", body)

    def test_explicit_completion_limit_wins_without_duplicate(self):
        body = self._payload(
            model="gpt-5.6-terra",
            max_tokens=123,
            max_completion_tokens=777,
        )
        self.assertEqual(body["max_completion_tokens"], 777)
        self.assertNotIn("max_tokens", body)
