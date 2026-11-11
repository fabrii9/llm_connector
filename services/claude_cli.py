# -*- coding: utf-8 -*-
"""Claude Code (``claude -p``) con la suscripción de Claude, para el tipo
de proveedor ``claude_subscription`` de LLM Connector.

La sesión llega por ``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token``,
empieza con ``sk-ant-oat``): el token cargado en Odoo o, si no hay, el de la
variable de entorno del contenedor (``claude.env``). Comparte cupo con el
resto del uso de esa cuenta.

Claude Code corre aislado:

* solo con las herramientas que pide cada llamada (búsqueda web, o ninguna);
* sin servidores MCP (``--strict-mcp-config``), sin sesión guardada
  (``--no-session-persistence``) y con un HOME propio dentro de ``data_dir``;
* en un directorio temporal vacío;
* sin ``ANTHROPIC_API_KEY`` ni similares en el entorno, para que nunca cobre
  por API en lugar de usar la suscripción.

Modo local (``LLM_CONNECTOR_CLAUDE_LOCAL=1``): para probar en una PC con
Claude Code logueado. Usa la sesión del usuario (sin token ni HOME propio).

Este archivo no depende de Odoo: se puede probar aislado.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

CANDIDATES = ('/usr/local/bin/claude', '/usr/bin/claude')
LOCAL_ENV = 'LLM_CONNECTOR_CLAUDE_LOCAL'
TOKEN_ENV = 'CLAUDE_CODE_OAUTH_TOKEN'
TOKEN_PREFIX = 'sk-ant-oat'

# Si existiera una API key (o un proveedor de nube), Claude Code cobraría por
# API en lugar de usar la suscripción.
HIDDEN_ENV = (
    'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
    'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
    'WA_CONSULTAS_MASTER_KEY', 'WA_CONSULTAS_ANTHROPIC_API_KEY',
)
# Variables de una sesión de Claude Code que esté lanzando a Odoo (p. ej. al
# probar desde una terminal de Claude Code): no se heredan al proceso hijo.
SESSION_ENV_PREFIXES = ('CLAUDE_CODE_', 'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_AGENT_SDK')
LIMIT_HINTS = (
    'usage limit', 'rate limit', 'rate_limit', 'limit reached', 'quota', '429',
    'overloaded',
)
WEB_TOOLS = 'WebSearch,WebFetch'
# Herramientas que un módulo puede pedir. Nada que lea/escriba archivos ni
# ejecute comandos dentro del contenedor de Odoo.
ALLOWED_TOOLS = ('WebSearch', 'WebFetch')
MODEL_ALIASES = ('sonnet', 'opus', 'haiku', 'default', 'opusplan', 'best')
DEFAULT_SYSTEM_PROMPT = (
    'Sos un asistente que responde pedidos de un sistema de gestión (Odoo). '
    'Respondé directo, sin preámbulos, en el idioma del pedido.'
)


class ClaudeCliError(RuntimeError):
    """Claude Code no pudo completar el trabajo.

    ``limit`` es True cuando se alcanzó el límite de uso de la suscripción
    (conviene reintentar más tarde en lugar de marcar el trabajo como fallido).
    """

    def __init__(self, message, limit=False):
        super().__init__(message)
        self.limit = limit


def is_local():
    return os.environ.get(LOCAL_ENV) == '1'


def find_claude():
    for path in CANDIDATES + (shutil.which('claude') or '',):
        if path and os.access(path, os.X_OK):
            return path
    raise ClaudeCliError(
        'No encontré Claude Code (claude) en el servidor. Instalalo en la imagen '
        'del contenedor (ver README de llm_connector).'
    )


def normalize_tools(tools):
    """``''``/None → sin herramientas; ``'web'``/True → WebSearch,WebFetch;
    también acepta una lista o un texto separado por comas."""
    if tools in (None, False, ''):
        return ''
    if tools is True or tools == 'web':
        return WEB_TOOLS
    if isinstance(tools, str):
        names = [t.strip() for t in tools.split(',')]
    else:
        names = [str(t).strip() for t in tools]
    names = [n for n in names if n]
    invalid = [n for n in names if n not in ALLOWED_TOOLS]
    if invalid:
        raise ClaudeCliError(
            'Herramientas no permitidas para Claude (suscripción): %s. '
            'Permitidas: %s.' % (', '.join(invalid), ', '.join(ALLOWED_TOOLS))
        )
    return ','.join(dict.fromkeys(names))


def build_environment(home, token=None):
    """Entorno para el proceso ``claude``. Devuelve ``(env, origen)`` donde
    ``origen`` es ``'local'``, ``'odoo'`` o ``'entorno'`` (de dónde salió la sesión)."""
    env_token = (os.environ.get(TOKEN_ENV) or '').strip()
    env = {
        k: v for k, v in os.environ.items()
        if k not in HIDDEN_ENV and not k.startswith(SESSION_ENV_PREFIXES)
    }
    env.update({'DISABLE_AUTOUPDATER': '1', 'DISABLE_TELEMETRY': '1'})
    if is_local():
        # Prueba en una PC con Claude Code logueado: sesión del usuario, sin
        # token ni HOME propio (respeta CLAUDE_CONFIG_DIR si está definido).
        return env, 'local'
    token = (token or '').strip()
    source = 'odoo' if token else 'entorno'
    token = token or env_token
    if not token:
        raise ClaudeCliError(
            'No hay token de la suscripción de Claude: cargalo en LLM Connector → '
            'Proveedores (tipo "Claude (suscripción)") o definí CLAUDE_CODE_OAUTH_TOKEN '
            'en el contenedor. Se genera con "claude setup-token".'
        )
    env.pop('CLAUDE_CONFIG_DIR', None)
    os.makedirs(home, mode=0o700, exist_ok=True)
    env.update({
        'HOME': home,
        TOKEN_ENV: token,
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
    })
    return env, source


def friendly_error(message, secret=None):
    message = mask(message, secret)
    low = message.lower()
    if any(hint in low for hint in LIMIT_HINTS):
        return ClaudeCliError(
            'Se alcanzó el límite de uso de la suscripción de Claude. ' + message[:300],
            limit=True,
        )
    return ClaudeCliError(message)


def mask(text, secret=None):
    text = text or ''
    if secret:
        text = text.replace(secret, '***')
    return re.sub(r'sk-ant-[A-Za-z0-9_\-]{8,}', 'sk-ant-***', text)


def extract_json(text):
    """Saca el primer objeto JSON de un texto (con o sin ```json)."""
    text = (text or '').strip()
    fence = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find('{'), text.rfind('}')
    if start == -1 or end <= start:
        raise ValueError('sin JSON')
    return json.loads(text[start:end + 1])


def run(prompt, home, system_prompt=None, json_schema=None, tools='', model='sonnet',
        max_turns=None, timeout=300, token=None):
    """Ejecuta ``claude -p --output-format json`` y devuelve un dict:

    * ``result``: texto final de la respuesta;
    * ``data``: dict con el JSON pedido (si hay ``json_schema``; si no, el JSON
      que venga en el texto, o None);
    * ``structured_output``: lo que devolvió ``--json-schema`` tal cual (o None);
    * ``usage`` (dict crudo), ``input_tokens``, ``output_tokens``;
    * ``cost`` (USD equivalentes informados por Claude Code; con suscripción no
      se cobra), ``num_turns``, ``duration`` (s), ``stop_reason`` (subtype),
      ``model``, ``session_source`` (``local``/``odoo``/``entorno``).

    Lanza ``ClaudeCliError`` (con ``limit=True`` si se agotó el cupo).
    """
    if not (prompt or '').strip():
        raise ClaudeCliError('El pedido para Claude está vacío.')
    claude = find_claude()
    tool_list = normalize_tools(tools)
    if max_turns is None:
        max_turns = 30 if tool_list else 4
    env, source = build_environment(home, token=token)
    secret = env.get(TOKEN_ENV)
    base = [
        claude, '-p', '--output-format', 'json', '--model', model or 'sonnet',
        '--system-prompt', system_prompt or DEFAULT_SYSTEM_PROMPT,
        '--tools', tool_list, '--allowedTools', tool_list or 'none',
        '--strict-mcp-config', '--max-turns', str(int(max_turns)),
        '--no-session-persistence',
    ]
    started = time.monotonic()
    workdir = tempfile.mkdtemp(prefix='llm_connector_claude_')
    try:
        if json_schema:
            schema = json_schema if isinstance(json_schema, str) else json.dumps(
                json_schema, ensure_ascii=False)
            proc = _call(base + ['--json-schema', schema], prompt, workdir, env, timeout)
            stderr = proc.stderr or ''
            if proc.returncode and '--json-schema' in stderr and 'unknown option' in stderr:
                # Claude Code viejo, sin salida estructurada: se pide el JSON en el texto.
                proc = _call(
                    base, prompt + '\n\nRespondé solo con el objeto JSON, sin texto antes ni después.',
                    workdir, env, timeout,
                )
        else:
            proc = _call(base, prompt, workdir, env, timeout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    try:
        out = json.loads(proc.stdout)
    except ValueError:
        detail = (proc.stderr or proc.stdout or 'sin mensaje')[-500:]
        raise friendly_error(
            'Claude Code no respondió (código %s): %s' % (proc.returncode, detail), secret)
    if not isinstance(out, dict):
        raise friendly_error('Claude Code devolvió una salida inesperada: %s'
                             % str(proc.stdout)[:300], secret)

    text = (out.get('result') or '').strip()
    subtype = out.get('subtype') or ''
    if out.get('is_error') and not (subtype == 'error_max_turns' and (text or json_schema)):
        raise friendly_error('Claude Code: %s' % (text or subtype or 'error'), secret)
    structured = out.get('structured_output')
    data = structured if isinstance(structured, dict) else None
    if data is None:
        try:
            data = extract_json(text)
        except ValueError:
            if json_schema:
                if subtype == 'error_max_turns':
                    raise ClaudeCliError(
                        'Claude Code usó todas las vueltas (%s) sin terminar. Subí max_turns '
                        'o achicá el pedido.' % max_turns)
                raise ClaudeCliError('Claude Code no devolvió el JSON pedido: %s'
                                     % mask(text, secret)[:400])
            data = None
    usage = out.get('usage') or {}
    return {
        'result': text,
        'data': data,
        'structured_output': structured,
        'usage': usage,
        'input_tokens': (usage.get('input_tokens') or 0)
        + (usage.get('cache_read_input_tokens') or 0)
        + (usage.get('cache_creation_input_tokens') or 0),
        'output_tokens': usage.get('output_tokens') or 0,
        'cost': out.get('total_cost_usd') or 0.0,
        'num_turns': out.get('num_turns') or 0,
        'duration': round(time.monotonic() - started, 2),
        'stop_reason': subtype,
        'model': model or 'sonnet',
        'session_source': source,
    }


def _call(command, prompt, workdir, env, timeout):
    try:
        return subprocess.run(
            command, input=prompt, capture_output=True, text=True, encoding='utf-8',
            errors='replace', cwd=workdir, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(
            'Claude Code tardó más de %s s. Subí el timeout o achicá el pedido.' % timeout
        ) from exc
    except OSError as exc:
        raise ClaudeCliError('No pude ejecutar Claude Code: %s' % exc) from exc
