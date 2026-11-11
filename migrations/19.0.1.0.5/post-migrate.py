# -*- coding: utf-8 -*-
"""Pone al día la plantilla "Anthropic (Claude)" (noupdate) con los modelos
actuales de Claude, solo si nadie la configuró todavía (sin API key)."""
from odoo import SUPERUSER_ID, api

MODELOS_VIEJOS = {
    "", "claude-sonnet-4-5", "claude-3-5-sonnet-latest", "claude-3-5-sonnet-20241022",
    "claude-3-opus-latest", "claude-3-haiku-20240307",
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    rec = env.ref("llm_connector.provider_anthropic", raise_if_not_found=False)
    if not rec or not rec.exists() or rec.api_key:
        return
    vals = {"active": True}
    if (rec.model or "").strip() in MODELOS_VIEJOS:
        vals["model"] = "claude-opus-5-5"
    if rec.name == "Anthropic (Claude)":
        vals["name"] = "Claude API (Anthropic)"
    if (rec.timeout or 0) < 300:
        vals["timeout"] = 300
    if (rec.max_tokens or 0) < 16000:
        vals["max_tokens"] = 16000
    rec.write(vals)
