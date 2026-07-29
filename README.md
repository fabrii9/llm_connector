# LLM Connector

Módulo base para conectar Odoo 19 con proveedores LLM: **Kimi (Moonshot AI)**, OpenAI, Anthropic (Claude), Google Gemini, Groq y cualquier API compatible con OpenAI.

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

Si no se pasa tipo a `get_provider()`, devuelve el primer proveedor activo con API key (ordenado por secuencia).

## Notas

- `requests` ya es dependencia de Odoo, no requiere instalar nada extra.
- La API key sólo la ven usuarios del grupo *LLM Connector Manager*. Los módulos consumidores deben llamar con `.sudo()`.
- Para agregar un proveedor compatible con OpenAI (Ollama, DeepSeek, etc.), crear un registro tipo *Otro (API compatible OpenAI)* con su URL base.
