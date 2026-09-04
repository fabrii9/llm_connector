# -*- coding: utf-8 -*-
{
    "name": "LLM Connector",
    "summary": "Conector base a modelos LLM (Kimi, OpenAI, Anthropic, Gemini, Groq) para usar desde otros módulos",
    "version": "19.0.1.0.3",
    "category": "Tools",
    "author": "AfterMoves",
    "website": "https://aftermoves.com",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/llm_provider_data.xml",
        "views/llm_provider_views.xml",
        "views/menu.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
