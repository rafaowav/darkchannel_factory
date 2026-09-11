"""
services/metadata_service.py – Título/descrição/tags + thumbnail por job.

Encapsula metadata_generator e produz também o review.md para aprovação
humana antes do agendamento/publicação manual.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def generate_metadata(
    tipo: str,
    tema_ou_produto: str,
    roteiro: str,
    produtos: Optional[List[Dict[str, str]]] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Gera título SEO, descrição com FTC/capítulos e tags (via Gemini)."""
    from metadata_generator import generate_video_metadata

    return generate_video_metadata(tipo, tema_ou_produto, roteiro, produtos, extras)


def generate_thumbnail(
    tipo: str,
    titulo: str,
    imagem_fundo: Optional[str],
    output_path: str,
    preco_str: str = "",
    tema_ou_produto: str = "",
) -> str:
    """Thumbnail 1280x720 específica do tipo de vídeo."""
    from metadata_generator import generate_thumbnail as _gen

    return _gen(tipo, titulo, imagem_fundo, output_path, preco_str, tema_ou_produto)


def build_review_markdown(
    job_id: str,
    meta: Dict[str, Any],
    video_path: str,
    audio_seconds: float,
    word_count: int,
    product_snapshots: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Monta o review.md que acompanha o job para conferência humana.

    Args:
        job_id: ID do job.
        meta: Retorno de generate_metadata().
        video_path: Caminho do MP4 final.
        audio_seconds: Duração real da narração.
        word_count: Palavras do roteiro.
        product_snapshots: Snapshots de produto (com horário de consulta).

    Returns:
        Conteúdo markdown.
    """
    lines = [
        f"# Review — {job_id}",
        "",
        f"- **Título:** {meta.get('titulo', '')}",
        f"- **Duração do áudio:** {audio_seconds / 60:.1f} min",
        f"- **Palavras do roteiro:** {word_count}",
        f"- **Categoria:** {meta.get('categoria', '')} | **Idioma:** {meta.get('idioma', '')}",
        f"- **Tags ({len(meta.get('tags', []))}):** {', '.join(meta.get('tags', []))}",
        f"- **Arquivo:** `{video_path}`",
        "",
        "## Descrição",
        "",
        meta.get("descricao", ""),
        "",
    ]
    if product_snapshots:
        lines += ["## Produtos (snapshot no momento da consulta)", ""]
        for p in product_snapshots:
            lines.append(
                f"- {p.get('name', '?')[:70]} — R$ {p.get('price', 0):.2f}"
                f" ⭐{p.get('rating', 0):.1f} 🛒{p.get('sold', 0)}"
                f" (consulta: {p.get('checked_at', '?')})"
            )
        lines.append("")
    lines += [
        "## Checklist humano",
        "",
        "- [ ] Conferir preço/disponibilidade atual do produto na Shopee",
        "- [ ] Vincular produto/sacolinha manualmente no app da Shopee (vídeos verticais)",
        "- [ ] Revisar claims do roteiro antes de publicar",
        "- [ ] Publicação é SEMPRE manual — não há post automático neste projeto",
        "",
    ]
    return "\n".join(lines)
