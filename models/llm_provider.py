# -*- coding: utf-8 -*-
import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PROVIDER_DEFAULTS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "console_url": "https://platform.openai.com/api-keys",
    },
    "kimi": {
        "base_url": "https://api.moonshot.ai/v1",
        "default_model": "kimi-k2-0905-preview",
        "console_url": "https://platform.moonshot.ai/console/api-keys",
    },
    "kimi_code": {
        "base_url": "https://api.kimi.com/coding/v1",
        "default_model": "kimi-for-coding",
        "console_url": "https://www.kimi.com/code/console",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-sonnet-4-5",
        "console_url": "https://console.anthropic.com/settings/keys",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.5-flash",
        "console_url": "https://aistudio.google.com/app/apikey",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "console_url": "https://console.groq.com/keys",
    },
    "custom": {
        "base_url": "",
        "default_model": "",
        "console_url": "",
    },
}

# Proveedores que usan la API compatible con OpenAI (/chat/completions)
OPENAI_COMPATIBLE = ("openai", "kimi", "kimi_code", "groq", "custom")

# OpenAI dejó max_tokens obsoleto para sus modelos de razonamiento y GPT-5.
# Los proveedores compatibles conservan el parámetro histórico.
OPENAI_MAX_COMPLETION_PREFIXES = ("gpt-5", "o1", "o3", "o4")


class LlmProvider(models.Model):
    _name = "llm.provider"
    _description = "Conexión a proveedor LLM"
    _order = "sequence, id"

    name = fields.Char(string="Nombre", required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    provider_type = fields.Selection(
        selection=[
            ("kimi_code", "Kimi Code (membresía)"),
            ("kimi", "Kimi Platform (pay-as-you-go)"),
            ("openai", "OpenAI"),
            ("anthropic", "Anthropic (Claude)"),
            ("gemini", "Google Gemini"),
            ("groq", "Groq"),
            ("custom", "Otro (API compatible OpenAI)"),
        ],
        string="Proveedor",
        required=True,
        default="kimi_code",
    )
    api_key = fields.Char(string="API Key")
    base_url = fields.Char(
        string="URL base",
        help="URL base de la API. Se completa automáticamente según el proveedor.",
    )
    model = fields.Char(
        string="Modelo",
        help="Nombre del modelo a usar, p. ej. kimi-k2-0905-preview, gpt-4o-mini.",
    )
    temperature = fields.Float(default=0.7)
    max_tokens = fields.Integer(default=1024)
    timeout = fields.Integer(string="Timeout (s)", default=30)

    state = fields.Selection(
        selection=[
            ("draft", "Sin probar"),
            ("ok", "Conexión OK"),
            ("fail", "Error de conexión"),
        ],
        string="Estado",
        default="draft",
        readonly=True,
        copy=False,
    )
    last_test_date = fields.Datetime(string="Última prueba", readonly=True, copy=False)
    last_test_message = fields.Text(string="Resultado de la prueba", readonly=True, copy=False)
    company_id = fields.Many2one(
        "res.company",
        string="Compañía",
        default=lambda self: self.env.company,
    )
    console_url = fields.Char(
        string="Consola del proveedor",
        compute="_compute_console_url",
    )

    @api.depends("provider_type")
    def _compute_console_url(self):
        for rec in self:
            rec.console_url = PROVIDER_DEFAULTS.get(
                rec.provider_type or "", {}
            ).get("console_url", "")

    @api.onchange("provider_type")
    def _onchange_provider_type(self):
        defaults = PROVIDER_DEFAULTS.get(self.provider_type or "", {})
        self.base_url = defaults.get("base_url", "")
        if not self.model:
            self.model = defaults.get("default_model", "")

    # ------------------------------------------------------------------
    # API pública para otros módulos
    # ------------------------------------------------------------------
    @api.model
    def get_provider(self, provider_type=None):
        """Devuelve el primer proveedor activo (con API key), opcionalmente
        filtrado por tipo. Pensado como punto de entrada para otros módulos:

            provider = self.env["llm.provider"].sudo().get_provider("kimi_code")
        """
        domain = [("active", "=", True), ("api_key", "!=", False)]
        if provider_type:
            domain.append(("provider_type", "=", provider_type))
        return self.search(domain, limit=1)

    def chat_completion(self, messages, model=None, temperature=None,
                        max_tokens=None, raw=False, **extra):
        """Envía una conversación al modelo y devuelve el texto de respuesta.

        :param messages: lista de dicts [{"role": "system"|"user"|"assistant",
                                          "content": "..."}]
        :param model: sobreescribe el modelo configurado
        :param temperature: sobreescribe la temperatura configurada
        :param max_tokens: sobreescribe el máximo de tokens configurado
        :param raw: si es True devuelve el JSON completo de la respuesta
        :param extra: parámetros adicionales para el body de la request
        :return: str con el contenido de la respuesta (o dict si raw=True)
        """
        self.ensure_one()
        if self.provider_type in OPENAI_COMPATIBLE:
            result = self._chat_openai(messages, model=model,
                                       temperature=temperature,
                                       max_tokens=max_tokens, extra=extra)
            if raw:
                return result
            return result["choices"][0]["message"]["content"]
        if self.provider_type == "anthropic":
            result = self._chat_anthropic(messages, model=model,
                                          temperature=temperature,
                                          max_tokens=max_tokens, extra=extra)
            if raw:
                return result
            return "".join(
                block.get("text", "") for block in result.get("content", [])
                if block.get("type") == "text"
            )
        if self.provider_type == "gemini":
            result = self._chat_gemini(messages, model=model,
                                       temperature=temperature,
                                       max_tokens=max_tokens, extra=extra)
            if raw:
                return result
            parts = (result.get("candidates") or [{}])[0] \
                .get("content", {}).get("parts", [])
            return "".join(part.get("text", "") for part in parts)
        raise UserError(_("Proveedor no soportado: %s") % self.provider_type)

    def generate(self, prompt, system=None, **kwargs):
        """Atajo para una consulta simple de un solo mensaje."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat_completion(messages, **kwargs)

    # ------------------------------------------------------------------
    # Prueba de conexión
    # ------------------------------------------------------------------
    def action_open_console(self):
        """Abre la consola del proveedor para generar/copiar la API key."""
        self.ensure_one()
        if not self.console_url:
            raise UserError(
                _("Este proveedor no tiene consola web conocida. "
                  "Generá la API key manualmente.")
            )
        return {
            "type": "ir.actions.act_url",
            "url": self.console_url,
            "target": "new",
        }

    def action_test_connection(self):
        self.ensure_one()
        try:
            found = self._list_models()
            message = _("Conexión correcta. Modelos disponibles: %s") % (
                ", ".join(found[:10]) or _("(sin listado)")
            )
            self._set_test_result("ok", message)
            notification = {
                "type": "success",
                "title": _("Conexión exitosa"),
                "message": message,
                "sticky": False,
            }
        except Exception as err:  # noqa: BLE001 - se muestra al usuario
            _logger.warning("LLM connector: fallo de conexión con %s: %s",
                            self.name, err)
            message = str(err)
            self._set_test_result("fail", message)
            notification = {
                "type": "danger",
                "title": _("Error de conexión"),
                "message": message,
                "sticky": True,
            }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": notification,
        }

    def _set_test_result(self, state, message):
        self.write({
            "state": state,
            "last_test_date": fields.Datetime.now(),
            "last_test_message": message,
        })

    # ------------------------------------------------------------------
    # Implementaciones por proveedor
    # ------------------------------------------------------------------
    def _list_models(self):
        """Devuelve la lista de IDs de modelos disponibles (para la prueba)."""
        self.ensure_one()
        if self.provider_type in OPENAI_COMPATIBLE:
            data = self._request("GET", self._url("/models"))
            return [m.get("id", "") for m in data.get("data", [])]
        if self.provider_type == "anthropic":
            data = self._request("GET", self._url("/v1/models"))
            return [m.get("id", "") for m in data.get("data", [])]
        if self.provider_type == "gemini":
            data = self._request("GET", self._url("/models"))
            return [m.get("name", "") for m in data.get("models", [])]
        raise UserError(_("Proveedor no soportado: %s") % self.provider_type)

    def _chat_openai(self, messages, model=None, temperature=None,
                     max_tokens=None, extra=None):
        selected_model = model or self.model
        token_limit = max_tokens or self.max_tokens
        model_name = (selected_model or "").strip().lower()
        uses_max_completion_tokens = (
            self.provider_type == "openai"
            and model_name.startswith(OPENAI_MAX_COMPLETION_PREFIXES)
        )
        body = {
            "model": selected_model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            (
                "max_completion_tokens"
                if uses_max_completion_tokens
                else "max_tokens"
            ): token_limit,
        }
        body.update(extra or {})
        return self._request("POST", self._url("/chat/completions"), json=body)

    def _chat_anthropic(self, messages, model=None, temperature=None,
                        max_tokens=None, extra=None):
        system = "\n".join(
            m["content"] for m in messages if m.get("role") == "system"
        )
        body = {
            "model": model or self.model,
            "messages": [m for m in messages if m.get("role") != "system"],
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if system:
            body["system"] = system
        body.update(extra or {})
        return self._request("POST", self._url("/v1/messages"), json=body)

    def _chat_gemini(self, messages, model=None, temperature=None,
                     max_tokens=None, extra=None):
        contents = []
        system_parts = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                system_parts.append({"text": message.get("content", "")})
                continue
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": message.get("content", "")}],
            })
        body = {
            "contents": contents,
            "generationConfig": {
                "temperature": self.temperature if temperature is None else temperature,
                "maxOutputTokens": max_tokens or self.max_tokens,
            },
        }
        if system_parts:
            body["systemInstruction"] = {"parts": system_parts}
        body.update(extra or {})
        url = self._url("/models/%s:generateContent" % (model or self.model))
        return self._request("POST", url, json=body)

    # ------------------------------------------------------------------
    # Helpers HTTP
    # ------------------------------------------------------------------
    def _url(self, path):
        self.ensure_one()
        if not self.base_url:
            raise UserError(
                _("El proveedor %s no tiene URL base configurada.") % self.name
            )
        return self.base_url.rstrip("/") + path

    def _headers(self):
        self.ensure_one()
        api_key = (self.api_key or "").strip()
        if not api_key:
            raise UserError(
                _("El proveedor %s no tiene API key configurada.") % self.name
            )
        if self.provider_type == "anthropic":
            return {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
        if self.provider_type == "gemini":
            return {"Content-Type": "application/json"}
        return {
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "application/json",
        }

    def _request(self, method, url, **kwargs):
        self.ensure_one()
        headers = kwargs.pop("headers", {})
        headers = {**self._headers(), **headers}
        params = kwargs.pop("params", {})
        if self.provider_type == "gemini":
            params = {**params, "key": (self.api_key or "").strip()}
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                timeout=self.timeout or 30,
                **kwargs,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as err:
            detail = ""
            if err.response is not None:
                detail = err.response.text[:500]
            raise UserError(
                _("Error al conectar con %(provider)s: %(error)s %(detail)s")
                % {"provider": self.name, "error": err, "detail": detail}
            ) from err
        return response.json()
