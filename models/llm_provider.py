# -*- coding: utf-8 -*-
import logging
import os

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import config

from ..services import claude_cli

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
        "default_model": "claude-opus-5-5",
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
    "claude_subscription": {
        "base_url": "",
        "default_model": "sonnet",
        "console_url": "",
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
OPENAI_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")

# Claude Code con la suscripción de Claude (claude -p): no usa api_key/base_url.
CLAUDE_SUBSCRIPTION = "claude_subscription"

# Claude por API (Anthropic). Alias -> ID vigente, para que un módulo pueda pedir
# "sonnet" y funcione igual con la API que con la suscripción.
CLAUDE_API_MODELS = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "fable": "claude-fable-5-2",
}
CLAUDE_API_DEFAULT_MODEL = "claude-opus-5-5"
# Modelos con razonamiento siempre activo: necesitan margen de max_tokens para
# pensar y además escribir la respuesta.
CLAUDE_THINKING_PREFIXES = (
    "claude-opus-5", "claude-sonnet-5", "claude-haiku-5", "claude-fable-5",
    "claude-mythos-5",
)
CLAUDE_MIN_MAX_TOKENS = 16000
# Modelos con respaldo del lado de Anthropic si el modelo rechaza un pedido por
# sus filtros de seguridad (el pedido se reintenta en otro modelo automáticamente).
CLAUDE_FALLBACK_PREFIXES = (
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1",
)
CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Los modelos actuales rechazan temperature/top_p/top_k (error 400): no se envían.
CLAUDE_SAMPLING_KEYS = ("temperature", "top_p", "top_k")


class ClaudeSubscriptionError(UserError):
    """Error de Claude (suscripción). ``limit`` es True si se agotó el cupo de
    uso: conviene reintentar más tarde en lugar de dar el trabajo por fallido."""

    def __init__(self, message, limit=False):
        super().__init__(message)
        self.limit = limit


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
            ("claude_subscription", "Claude (suscripción, Claude Code)"),
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
    # --- Claude (suscripción) -----------------------------------------
    # El token real nunca se manda al navegador: se escribe por el campo
    # "_input" (siempre vacío al leer) y se guarda en claude_oauth_token,
    # visible solo para Administración / Ajustes.
    claude_oauth_token = fields.Char(
        string="Token de suscripción (guardado)",
        groups="base.group_system",
        copy=False,
        prefetch=False,
    )
    claude_oauth_token_input = fields.Char(
        string="Token de suscripción",
        compute="_compute_claude_oauth_token_input",
        inverse="_inverse_claude_oauth_token_input",
        groups="base.group_system",
        help="Token de 'claude setup-token' (empieza con sk-ant-oat). Se guarda y "
             "no se vuelve a mostrar. Si queda vacío se usa la variable "
             "CLAUDE_CODE_OAUTH_TOKEN del contenedor.",
    )
    claude_token_set = fields.Boolean(
        string="Token cargado en Odoo",
        compute="_compute_claude_token_set",
        groups="base.group_system",
    )

    @api.depends("provider_type")
    def _compute_console_url(self):
        for rec in self:
            rec.console_url = PROVIDER_DEFAULTS.get(
                rec.provider_type or "", {}
            ).get("console_url", "")

    def _compute_claude_oauth_token_input(self):
        for rec in self:
            rec.claude_oauth_token_input = False

    def _inverse_claude_oauth_token_input(self):
        for rec in self:
            token = (rec.claude_oauth_token_input or "").strip()
            if not token:
                continue  # vacío = no cambiar (para borrar: botón "Borrar token")
            if not token.startswith(claude_cli.TOKEN_PREFIX):
                raise ValidationError(_(
                    "El token de la suscripción de Claude empieza con %s "
                    "(se genera con \"claude setup-token\"). No es una API key."
                ) % claude_cli.TOKEN_PREFIX)
            rec.claude_oauth_token = token

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._claude_forget_token_input()
        return records

    def write(self, vals):
        result = super().write(vals)
        if "claude_oauth_token_input" in vals:
            self._claude_forget_token_input()
        return result

    def _claude_forget_token_input(self):
        # Que el token recién cargado no vuelva al navegador en la respuesta
        # del guardado (web_save hace write + read en la misma transacción).
        self.invalidate_recordset(["claude_oauth_token_input"])

    @api.depends("claude_oauth_token")
    def _compute_claude_token_set(self):
        for rec in self:
            rec.claude_token_set = bool(rec.claude_oauth_token)

    @api.onchange("provider_type")
    def _onchange_provider_type(self):
        defaults = PROVIDER_DEFAULTS.get(self.provider_type or "", {})
        self.base_url = defaults.get("base_url", "")
        if self.provider_type == CLAUDE_SUBSCRIPTION:
            if not self._claude_is_valid_model(self.model):
                self.model = defaults.get("default_model", "")
            if (self.timeout or 0) < 60:
                self.timeout = 300
        elif self.provider_type == "anthropic":
            if not (self.model or "").strip().lower().startswith("claude") \
                    and (self.model or "").strip().lower() not in CLAUDE_API_MODELS:
                self.model = defaults.get("default_model", "")
            if (self.timeout or 0) < 120:
                self.timeout = 300
            if (self.max_tokens or 0) < CLAUDE_MIN_MAX_TOKENS:
                self.max_tokens = CLAUDE_MIN_MAX_TOKENS
        elif not self.model:
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
        domain = [("active", "=", True)]
        if provider_type != CLAUDE_SUBSCRIPTION:
            # Claude (suscripción) no usa api_key (token propio o del entorno).
            # Sin tipo, el comportamiento es el de siempre: solo con api_key.
            domain.append(("api_key", "!=", False))
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
        if self.provider_type == CLAUDE_SUBSCRIPTION:
            result = self._chat_claude_subscription(messages, model=model)
            return result if raw else result["result"]
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
            if self.provider_type == CLAUDE_SUBSCRIPTION:
                message = self._test_claude_subscription()
            elif self.provider_type == "anthropic":
                message = self._test_anthropic()
            else:
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

    def _test_anthropic(self):
        """Lista los modelos y hace un pedido mínimo con el modelo configurado,
        para validar la clave, el crédito y el formato del pedido."""
        self.ensure_one()
        found = self._list_models()
        selected_model = self._claude_api_model()
        result = self._chat_anthropic(
            [{"role": "user", "content": "Respondé exactamente con la palabra OK."}],
            model=selected_model,
            extra={"output_config": {"effort": "low"}},
        )
        answer = "".join(
            block.get("text", "") for block in result.get("content") or []
            if block.get("type") == "text"
        ).strip()
        usage = result.get("usage") or {}
        current = [m for m in CLAUDE_API_MODELS.values() if m in found]
        return _(
            "Conexión correcta con la API de Claude. %(model)s respondió \"%(answer)s\" "
            "(%(tin)s tokens de entrada, %(tout)s de salida). Modelos actuales "
            "disponibles: %(models)s."
        ) % {
            "model": result.get("model") or selected_model,
            "answer": answer[:50],
            "tin": usage.get("input_tokens", "-"),
            "tout": usage.get("output_tokens", "-"),
            "models": ", ".join(current) or ", ".join(found[:8]) or _("(sin listado)"),
        }

    def action_clear_claude_token(self):
        """Borra el token guardado (vuelve a usar el del entorno, si hay)."""
        self.write({"claude_oauth_token": False})
        return True

    def _set_test_result(self, state, message):
        self.write({
            "state": state,
            "last_test_date": fields.Datetime.now(),
            "last_test_message": message,
        })

    # ------------------------------------------------------------------
    # Claude (suscripción): Claude Code en el servidor
    # ------------------------------------------------------------------
    @api.model
    def claude_subscription_run(self, prompt, system_prompt=None, json_schema=None,
                                tools="", model=None, max_turns=None, timeout=None):
        """Ejecuta un pedido con Claude Code usando la suscripción de Claude.

        Se puede llamar sobre un registro de tipo ``claude_subscription``
        (usa su token/modelo/timeout) o sobre el modelo vacío (usa el primer
        proveedor activo de ese tipo y, si no hay, solo el token del entorno)::

            res = self.env["llm.provider"].sudo().claude_subscription_run(
                "Resumí ...", system_prompt="Sos ...",
                json_schema={"type": "object", ...},   # opcional
                tools="WebSearch,WebFetch",             # o "" (ninguna)
                model="sonnet", max_turns=20, timeout=600,
            )
            res["data"]    # dict (con json_schema) o None
            res["result"]  # texto final

        :param tools: ``""`` (ninguna, por defecto), ``"web"`` o
            ``"WebSearch,WebFetch"``. No se permiten herramientas que toquen
            archivos o ejecuten comandos.
        :param model: alias (sonnet, opus, haiku) o ID ``claude-*``. Por
            defecto el del proveedor, o ``sonnet``.
        :return: dict con result, data, structured_output, usage,
            input_tokens, output_tokens, cost, num_turns, duration,
            stop_reason, model, session_source.
        :raises ClaudeSubscriptionError: (UserError) con ``.limit`` = True si
            se agotó el cupo de la suscripción.
        """
        self._check_claude_access()
        provider = self._claude_resolve_provider()
        token = provider.claude_oauth_token if provider else False
        if timeout is None:
            timeout = provider.timeout if provider and (provider.timeout or 0) >= 60 else 300
        selected_model = self._claude_pick_model(model, provider)
        try:
            return claude_cli.run(
                prompt,
                home=self._claude_home(),
                system_prompt=system_prompt,
                json_schema=json_schema,
                tools=tools,
                model=selected_model,
                max_turns=max_turns,
                timeout=timeout,
                token=token,
            )
        except claude_cli.ClaudeCliError as err:
            _logger.warning("LLM connector: Claude (suscripción) falló: %s", err)
            raise ClaudeSubscriptionError(str(err), limit=err.limit) from err

    def _check_claude_access(self):
        # Método público (llamable por RPC): ejecuta un proceso y consume cupo
        # de la suscripción. Solo superusuario (sudo) o el grupo del conector.
        if not (self.env.su or self.env.user.has_group("llm_connector.group_llm_manager")):
            raise AccessError(_(
                "Solo LLM Connector Manager puede usar Claude (suscripción). "
                "Los módulos deben llamarlo con sudo()."
            ))

    def _claude_resolve_provider(self):
        """Registro de proveedor a usar (puede ser vacío: solo token del entorno)."""
        provider = self.sudo()
        if len(provider) == 1 and provider.provider_type == CLAUDE_SUBSCRIPTION:
            return provider
        if len(provider) > 1:
            provider = provider.filtered(
                lambda p: p.provider_type == CLAUDE_SUBSCRIPTION)[:1]
            if provider:
                return provider
        return self.sudo().get_provider(CLAUDE_SUBSCRIPTION)

    @api.model
    def _claude_is_valid_model(self, model):
        name = (model or "").strip().lower()
        return bool(name) and (
            name in claude_cli.MODEL_ALIASES or name.startswith("claude-")
        )

    @api.model
    def _claude_pick_model(self, model, provider=None):
        """Modelo pedido si es de Claude; si no (p. ej. 'gpt-4o' de un módulo
        pensado para otro proveedor), el del proveedor o sonnet."""
        for candidate in (model, provider.model if provider else None):
            if self._claude_is_valid_model(candidate):
                return candidate.strip()
        if model:
            _logger.info("LLM connector: modelo %s no es de Claude, uso el del proveedor.",
                         model)
        return PROVIDER_DEFAULTS[CLAUDE_SUBSCRIPTION]["default_model"]

    @api.model
    def _claude_home(self):
        return os.path.join(config["data_dir"], "llm_connector", "claude_home")

    def _chat_claude_subscription(self, messages, model=None):
        """chat_completion para Claude (suscripción): aplana la conversación en
        un pedido de texto. Las imágenes no se envían (se avisa en el log)."""
        self.ensure_one()
        system_parts, turns, dropped = [], [], 0
        for message in messages:
            text, skipped = self._claude_message_text(message.get("content"))
            dropped += skipped
            role = message.get("role")
            if role == "system":
                if text:
                    system_parts.append(text)
            else:
                turns.append((role, text))
        if dropped:
            _logger.warning("LLM connector: Claude (suscripción) no recibe imágenes; "
                            "se omitieron %s adjunto(s).", dropped)
        if len(turns) == 1:
            prompt = turns[0][1]
        else:
            labels = {"assistant": "Asistente", "user": "Usuario"}
            lines = ["Conversación hasta ahora (respondé el último mensaje del usuario):", ""]
            for role, text in turns:
                lines.append("%s: %s" % (labels.get(role, role or "Usuario"), text))
                lines.append("")
            prompt = "\n".join(lines).strip()
        return self.claude_subscription_run(
            prompt, system_prompt="\n\n".join(system_parts) or None, model=model,
        )

    @api.model
    def _claude_message_text(self, content):
        """Texto de un content (str o lista de partes OpenAI/Anthropic) y
        cantidad de partes no textuales omitidas."""
        if isinstance(content, str) or content is None:
            return content or "", 0
        texts, skipped = [], 0
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text") or "")
            else:
                skipped += 1
        return "\n".join(texts), skipped

    def _test_claude_subscription(self):
        self.ensure_one()
        result = self.claude_subscription_run(
            "Respondé exactamente con la palabra OK.",
            system_prompt="Respondé con una sola palabra.",
            model="haiku", max_turns=1, timeout=120,
        )
        origins = {
            "odoo": _("token cargado en Odoo"),
            "entorno": _("token de CLAUDE_CODE_OAUTH_TOKEN del contenedor"),
            "local": _("sesión local de Claude Code (LLM_CONNECTOR_CLAUDE_LOCAL)"),
        }
        return _(
            "Conexión correcta con la suscripción de Claude (%(origin)s). "
            "haiku respondió \"%(answer)s\" en %(duration)s s "
            "(%(tin)s tokens de entrada, %(tout)s de salida)."
        ) % {
            "origin": origins.get(result["session_source"], result["session_source"]),
            "answer": (result["result"] or "")[:50],
            "duration": result["duration"],
            "tin": result["input_tokens"],
            "tout": result["output_tokens"],
        }

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
        if self.provider_type == CLAUDE_SUBSCRIPTION:
            return list(claude_cli.MODEL_ALIASES[:3])
        raise UserError(_("Proveedor no soportado: %s") % self.provider_type)

    def _chat_openai(self, messages, model=None, temperature=None,
                     max_tokens=None, extra=None):
        selected_model = model or self.model
        token_limit = self.max_tokens if max_tokens is None else max_tokens
        model_name = (selected_model or "").strip().lower()
        uses_max_completion_tokens = (
            self.provider_type == "openai"
            and any(
                model_name == prefix
                or model_name.startswith((prefix + "-", prefix + "."))
                for prefix in OPENAI_REASONING_PREFIXES
            )
        )
        extra_body = dict(extra or {})
        body = {
            "model": selected_model,
            "messages": messages,
        }
        if "max_completion_tokens" in extra_body:
            body["max_completion_tokens"] = extra_body.pop(
                "max_completion_tokens"
            )
            extra_body.pop("max_tokens", None)
        elif uses_max_completion_tokens:
            body["max_completion_tokens"] = extra_body.pop(
                "max_tokens", token_limit
            )
        else:
            body["max_tokens"] = extra_body.pop("max_tokens", token_limit)
        if not uses_max_completion_tokens:
            body["temperature"] = (
                self.temperature if temperature is None else temperature
            )
        body.update(extra_body)
        return self._request("POST", self._url("/chat/completions"), json=body)

    @api.model
    def _claude_api_model(self, model=None):
        """ID de modelo para la API de Anthropic. Acepta los alias opus,
        sonnet, haiku y fable (los mismos que usa la suscripción)."""
        name = (model or "").strip()
        if not name:
            name = (self.model or "").strip() if len(self) == 1 else ""
        name = name or CLAUDE_API_DEFAULT_MODEL
        return CLAUDE_API_MODELS.get(name.lower(), name)

    def _chat_anthropic(self, messages, model=None, temperature=None,
                        max_tokens=None, extra=None):
        """Messages API de Anthropic. ``temperature`` se ignora: los modelos
        actuales la rechazan. Si el modelo rechaza el pedido por seguridad, se
        lanza UserError en lugar de devolver una respuesta vacía."""
        selected_model = self._claude_api_model(model)
        system = "\n".join(
            m["content"] for m in messages
            if m.get("role") == "system" and isinstance(m.get("content"), str)
        )
        extra_body = dict(extra or {})
        for key in CLAUDE_SAMPLING_KEYS:
            extra_body.pop(key, None)
        token_limit = extra_body.pop("max_tokens", None) or max_tokens or self.max_tokens or 1024
        if selected_model.startswith(CLAUDE_THINKING_PREFIXES):
            token_limit = max(token_limit, CLAUDE_MIN_MAX_TOKENS)
        body = {
            "model": selected_model,
            "messages": [m for m in messages if m.get("role") != "system"],
            "max_tokens": token_limit,
        }
        if system:
            body["system"] = system
        headers = {}
        if selected_model.startswith(CLAUDE_FALLBACK_PREFIXES) and "fallbacks" not in extra_body:
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = CLAUDE_FALLBACK_BETA
        body.update(extra_body)
        result = self._request("POST", self._url("/v1/messages"), json=body,
                               headers=headers)
        stop_reason = result.get("stop_reason")
        has_text = any(
            block.get("type") == "text" and block.get("text")
            for block in result.get("content") or []
        )
        if stop_reason == "refusal":
            details = result.get("stop_details") or {}
            raise UserError(_(
                "Claude (%(model)s) no respondió este pedido por sus filtros de "
                "seguridad (%(category)s). Reformulá el pedido o probá con otro modelo."
            ) % {"model": result.get("model") or selected_model,
                 "category": details.get("category") or _("sin categoría")})
        if stop_reason == "max_tokens" and not has_text:
            raise UserError(_(
                "Claude (%(model)s) usó todo el máximo de tokens (%(tokens)s) sin llegar "
                "a responder. Subí el máximo de tokens del proveedor."
            ) % {"model": selected_model, "tokens": token_limit})
        return result

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
