# LLM Connector

Módulo base para conectar Odoo 19 con proveedores LLM: **Kimi (Moonshot AI)**, OpenAI, Anthropic (Claude), Claude por suscripción (Claude Code), Google Gemini, Groq y cualquier API compatible con OpenAI.

La idea: la conexión se configura **una sola vez** acá, y el resto de los módulos la reutilizan.

## Configuración

Estas APIs no ofrecen login OAuth: la conexión es con **API key**. El flujo es:

1. Instalar el módulo `llm_connector`.
2. Ir a **LLM Connector → Proveedores** (requiere grupo *LLM Connector Manager*).
3. Abrir la plantilla precargada y presionar **Obtener API key**: se abre la consola del proveedor, te logueás, generás la key y la pegás en el campo **API key**.
4. Presionar **Probar conexión**. Si queda en verde, ya está lista para usar.

### Kimi con cuota de membresía (sin pay-as-you-go)

Si tenés membresía Kimi, usá el preset **Kimi Code (membresía)**: generá la key en la [Kimi Code Console](https://www.kimi.com/code/console) y el consumo se descuenta de tu cuota de membresía (la misma que usa la extensión de VS Code). El preset *Kimi Platform (pay-as-you-go)* queda para uso empresarial con saldo cargado.

> Nota: el endpoint de membresía está pensado para escenarios de programación y comparte cuota con tu IDE — si Odoo consume mucho, puede rate-limitar tu VS Code.

## Uso desde otros módulos

Agregar la dependencia en el `__manifest__.py` del módulo consumidor:

```python
"depends": ["llm_connector"],
```

Y llamar al modelo:

```python
# Obtener el proveedor activo (sudo: el usuario final no tiene permisos de config)
provider = self.env["llm.provider"].sudo().get_provider("kimi_code")

# Consulta simple
texto = provider.generate("Resumí este texto en 3 líneas: ...", system="Sos un asistente útil.")

# Conversación completa
texto = provider.chat_completion([
    {"role": "system", "content": "Respondé en español."},
    {"role": "user", "content": "Hola, ¿qué modelo sos?"},
])

# Respuesta cruda (JSON completo de la API)
data = provider.chat_completion(messages, raw=True)
```

Parámetros opcionales de `chat_completion` / `generate`: `model`, `temperature`, `max_tokens`, `raw=True`, y cualquier parámetro extra se pasa directo al body de la API.

Para modelos OpenAI GPT-5 y o-series, el conector normaliza `max_tokens` a `max_completion_tokens` y omite temperaturas personalizadas no admitidas. Esta adaptación no se aplica a Kimi, Groq ni proveedores personalizados.

Si no se pasa tipo a `get_provider()`, devuelve el primer proveedor activo con API key (ordenado por secuencia).

## Claude por API (Anthropic)

La plantilla **Claude API (Anthropic)** viene activa y lista: solo falta pegar la API key de
`console.anthropic.com` (empieza con `sk-ant-api`) y tocar **Probar conexión**, que lista los
modelos y hace un pedido mínimo para confirmar clave y crédito.

| Modelo | Para qué | USD por millón de tokens (entrada / salida) |
|---|---|---|
| `claude-opus-5-5` (por defecto) | La mayoría de las tareas | 4 / 20 |
| `claude-sonnet-5-5` | Volumen alto, más rápido | 2 / 10 |
| `claude-haiku-5-5` | Tareas simples y baratas | 0,10 / 0,50 |
| `claude-fable-5-2` | Lo más difícil | 8 / 40 |

También se aceptan los alias `opus`, `sonnet`, `haiku` y `fable` (los mismos que usa la
suscripción). Con estos modelos el conector no envía `temperature`/`top_p`/`top_k` (la API los
rechaza), sube `max_tokens` a 16000 como mínimo (el razonamiento está siempre activo) y, en
Opus 5.5, Sonnet 5.5 y Fable 5.2, activa el respaldo de Anthropic ante rechazos de seguridad
(`fallbacks: "default"`). Si Claude rechaza el pedido igual, se lanza `UserError`.

## Claude por suscripción (Claude Code)

El tipo **Claude (suscripción, Claude Code)** usa la suscripción de Claude (Pro/Max/Team) en lugar de la API paga: ejecuta `claude -p --output-format json` dentro del contenedor de Odoo.

1. El binario `claude` tiene que estar instalado en la imagen (`/usr/local/bin/claude`; ver el `Dockerfile` del servidor).
2. Generá el token con `claude setup-token` (empieza con `sk-ant-oat`).
3. En **LLM Connector → Proveedores**, activá la plantilla *Claude (suscripción)* y pegá el token en **Token de suscripción**. Se guarda en un campo visible solo para *Administración / Ajustes* y no se vuelve a mostrar. Si lo dejás vacío se usa `CLAUDE_CODE_OAUTH_TOKEN` del contenedor (`claude.env`).
4. **Probar conexión** corre un pedido mínimo con `haiku`.

Cómo corre: sin `ANTHROPIC_API_KEY` en el entorno (nunca cobra por API), con `HOME` propio en `<data_dir>/llm_connector/claude_home`, en un directorio temporal vacío, con `--no-session-persistence` y `--strict-mcp-config` (sin servidores MCP). Solo se permiten las herramientas `WebSearch` y `WebFetch`.

Uso desde otros módulos (con `sudo()`):

```python
res = self.env["llm.provider"].sudo().claude_subscription_run(
    "Investigá la empresa ACME SA y devolvé rubro y web.",
    system_prompt="Sos un analista comercial. Respondé con datos verificables.",
    json_schema={"type": "object", "properties": {"rubro": {"type": "string"},
                 "web": {"type": "string"}}, "required": ["rubro", "web"]},
    tools="WebSearch,WebFetch",   # "" = sin herramientas (por defecto)
    model="sonnet",               # sonnet | opus | haiku | claude-*
    max_turns=20, timeout=600,
)
res["data"]          # dict validado contra json_schema (o el JSON del texto, o None)
res["result"]        # texto final
res["usage"], res["cost"], res["num_turns"], res["duration"], res["stop_reason"]
```

Errores: `ClaudeSubscriptionError` (subclase de `UserError`, en `odoo.addons.llm_connector.models.llm_provider`). Si `err.limit` es `True` se agotó el cupo de la suscripción: reintentá más tarde en lugar de dar el trabajo por fallido.

Un proveedor de este tipo también responde a `generate()` / `chat_completion()` (la conversación se aplana a texto; `temperature` y `max_tokens` no se aplican; las imágenes se omiten). `get_provider()` sin tipo sigue devolviendo solo proveedores con API key; para este usar `get_provider("claude_subscription")`.

Prueba local (Mac con Claude Code logueado): `LLM_CONNECTOR_CLAUDE_LOCAL=1` usa la sesión del usuario, sin token ni HOME propio.

## Notas

- `requests` ya es dependencia de Odoo, no requiere instalar nada extra.
- La API key sólo la ven usuarios del grupo *LLM Connector Manager*. Los módulos consumidores deben llamar con `.sudo()`.
- Para agregar un proveedor compatible con OpenAI (Ollama, DeepSeek, etc.), crear un registro tipo *Otro (API compatible OpenAI)* con su URL base.
