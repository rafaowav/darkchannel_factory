"""
services/script_service.py – Geração de roteiros com validação e SRT.

Reaproveita engine.generate_scripts (prompts long/short/reel + expansão
automática quando abaixo do mínimo de palavras).
"""

from __future__ import annotations

import logging
import re
from typing import Tuple

logger = logging.getLogger(__name__)


def generate_script(tipo_engine: str, tema: str, duracao: str) -> str:
    """
    Gera a narração final de um job.

    Args:
        tipo_engine: 'global', 'brasil' ou 'shopee'.
        tema: Tema + dados reais embutidos no prompt.
        duracao: 'long', 'short' ou 'reel'.

    Returns:
        Texto da narração já validado por contagem de palavras.

    Raises:
        ValueError: roteiro abaixo do mínimo mesmo após expansão.
        RuntimeError: falha na API Gemini.
    """
    from engine import generate_scripts

    return generate_scripts(tipo_engine, tema, duracao=duracao)


def script_stats(script: str) -> Tuple[int, float]:
    """Retorna (palavras, duração estimada em segundos @150 wpm)."""
    words = len(script.split())
    return words, words / 150 * 60


def format_duration(seconds: float) -> str:
    """Segundos → '10 min 18s'."""
    m, s = divmod(int(round(seconds)), 60)
    return f"{m} min {s:02d}s"


def build_srt(script: str, total_duration: float) -> str:
    """
    Gera um SRT simples distribuído proporcionalmente às frases do roteiro.

    Args:
        script: Texto da narração.
        total_duration: Duração real do áudio em segundos.

    Returns:
        Conteúdo .srt.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", script) if s.strip()]
    if not sentences or total_duration <= 0:
        return ""
    weights = [max(len(s), 1) for s in sentences]
    total_w = sum(weights)
    cursor = 0.0
    lines = []
    for i, (sentence, w) in enumerate(zip(sentences, weights), start=1):
        span = total_duration * w / total_w
        start, end = cursor, min(cursor + span, total_duration)
        lines.append(str(i))
        lines.append(f"{_srt_ts(start)} --> {_srt_ts(end)}")
        lines.append(sentence)
        lines.append("")
        cursor = end
    return "\n".join(lines)


def _srt_ts(seconds: float) -> str:
    """Segundos → HH:MM:SS,mmm."""
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
