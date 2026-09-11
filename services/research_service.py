"""
services/research_service.py – Briefing de tema via Gemini.

Gera um markdown curto com ângulos, dados sugeridos e estrutura para o
roteirista (worker de pesquisa/briefing).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _gemini(prompt: str) -> str:
    """Chamada direta ao Gemini; retorna '' se indisponível."""
    try:
        from engine import _gemini_generate

        return _gemini_generate(prompt)
    except Exception as exc:
        logger.error("Research: Gemini falhou: %s", exc)
        return ""


def build_briefing(tipo: str, tema: str, produtos_desc: Optional[str] = None) -> str:
    """
    Gera o briefing (research.md) de um job.

    Args:
        tipo: 'global' ou 'brasil'.
        tema: Tema do vídeo.
        produtos_desc: Descrição dos produtos escolhidos (BRASIL).

    Returns:
        Conteúdo markdown do briefing (fallback mínimo se API falhar).
    """
    lang = "in English" if tipo == "global" else "em português do Brasil"
    prompt = f"""You are a YouTube documentary researcher. {lang}.
TOPIC: {tema}
{('PRODUCTS TO FEATURE:\\n' + produtos_desc) if produtos_desc else ''}

Produce a concise research brief in Markdown with these sections:
## Hook angles (3 options)
## Key facts & data to verify (dates, figures, names)
## Suggested narrative structure
## Visual ideas for B-roll (search queries in English for Pexels)
## Sources worth citing

Keep it under 400 words. Facts must be plausible and specific; flag any
uncertain claim with [verify].
"""
    body = _gemini(prompt)
    if not body.strip():
        body = (
            f"# Briefing: {tema}\n\n"
            f"_Falha na geração assistida — briefing mínimo._\n\n"
            f"- Tema: {tema}\n- Tipo: {tipo}\n"
        )
        logger.warning("Briefing fallback usado para '%s'.", tema)
    header = f"---\njob_type: {tipo}\ntema: {tema}\n---\n\n"
    return header + body
