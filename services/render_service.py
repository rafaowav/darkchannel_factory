"""
services/render_service.py – Renderizações assíncronas (sem bloquear o loop).

Envolve as funções de FFmpeg do engine com asyncio.create_subprocess_exec /
run_in_executor, validação de mídia de saída e geração de assets.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """Falha de renderização/FFmpeg."""


class InvalidMediaError(RenderError):
    """Arquivo de mídia gerado é inválido ou curto demais."""


async def _to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


def _validate_output(video_path: str, min_seconds: float = 3.0) -> None:
    """Confere existência e duração mínima do MP4 renderizado."""
    from engine import get_media_duration

    path = Path(video_path)
    if not path.exists() or path.stat().st_size < 10_000:
        raise InvalidMediaError(f"Arquivo de vídeo ausente/corrompido: {path.name}")
    duration = get_media_duration(str(path))
    if duration < min_seconds:
        raise InvalidMediaError(
            f"Duração do vídeo ({duration:.1f}s) abaixo do esperado "
            f"(mín {min_seconds:.0f}s)."
        )
    logger.info("Mídia validada: %s (%.1fs)", path.name, duration)


async def synthesize(script: str, voice: str, out_path: str) -> str:
    """TTS edge-tts em thread separada."""
    from engine import synthesize_speech

    try:
        return await _to_thread(synthesize_speech, script, voice, out_path)
    except Exception as exc:
        raise RenderError(f"Falha na síntese de voz: {exc}") from exc


async def download_background_clips(
    queries: List[str], count: int = 3, orientation: str = "landscape"
) -> List[str]:
    """Baixa clipes do Pexels sem bloquear o event loop."""
    from engine import download_brolls

    return await _to_thread(download_brolls, queries, count, orientation)


async def download_single_clip(query: str, orientation: str = "landscape") -> Optional[str]:
    from engine import download_broll

    return await _to_thread(download_broll, query, orientation)


async def render_horizontal(audio_path: str, broll: Optional[str], output_path: str) -> str:
    """Vídeo 16:9 (documentário longo/short)."""
    from engine import render_horizontal as _render

    try:
        result = await _to_thread(_render, audio_path, broll, output_path, "job")
    except RuntimeError as exc:
        raise RenderError(f"FFmpeg falhou: {exc}") from exc
    _validate_output(result, min_seconds=5.0)
    return result


async def render_shopee_reel(
    card_paths: List[str], clips: List[str], audio_path: str,
    output_path: str, product_name: str, price_str: str,
    discount_pct: float, rating: float, duration: Optional[float] = None,
) -> str:
    """Vídeo vertical 9:16 cinematográfico (multi-cena real + texto animado)."""
    from engine import render_cinematic_shopee

    try:
        result = await _to_thread(
            render_cinematic_shopee, card_paths, clips, audio_path, output_path,
            product_name, price_str, discount_pct, rating, duration,
        )
    except RuntimeError as exc:
        raise RenderError(f"FFmpeg falhou: {exc}") from exc
    _validate_output(result, min_seconds=8.0)
    return result


async def build_cards(image_paths: List[str]) -> List[str]:
    """Converte fotos reais em cartões RGBA para as cenas do reel."""
    from engine import build_clean_product_card

    cards: List[str] = []
    for img in image_paths[:4]:
        try:
            cards.append(await _to_thread(build_clean_product_card, img))
        except Exception as exc:
            logger.warning("Falha ao gerar cartão (%s): %s", img, exc)
    return cards


async def download_product_images(product, dest_dir: str, max_images: int = 5) -> List[str]:
    """Baixa imagens reais do anúncio via cliente Shopee."""
    from shopee_client import init_shopee_client

    client = init_shopee_client()
    if client is None:
        return []
    return await _to_thread(client.download_product_images, product, dest_dir, max_images)
