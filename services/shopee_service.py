"""
services/shopee_service.py – Consultas à API de afiliados com validação.

Regras de negócio: rejeita produtos sem imagem, sem link ou indisponíveis;
produz snapshots datados para auditoria de preço/conteúdo.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ProductValidationError(Exception):
    """Produto não atende aos requisitos mínimos para gerar vídeo."""


def _client():  # type: ignore[no-untyped-def]
    from shopee_client import init_shopee_client

    client = init_shopee_client()
    if client is None:
        raise RuntimeError(
            "API Shopee não configurada (SHOPEE_PARTNER_ID/KEY no .env)."
        )
    return client


def validate_product(product: Any) -> None:
    """
    Rejeita produto sem imagem, sem URL ou indisponível.

    Raises:
        ProductValidationError: dados insuficientes para produção.
    """
    problems = []
    if not product.name or len(product.name.strip()) < 3:
        problems.append("nome ausente")
    if not product.images:
        problems.append("sem imagem")
    if not (product.product_url or product.offer_link):
        problems.append("sem link do produto")
    if getattr(product, "sold", 0) == 0 and getattr(product, "rating", 0) == 0 \
            and getattr(product, "price", 0) == 0:
        problems.append("dados vazios (possivelmente indisponível)")
    if problems:
        raise ProductValidationError(
            f"Produto rejeitado ({', '.join(problems)}): {product.name[:40]!r}"
        )


def search(keyword: str, limit: int = 5) -> List[Any]:
    """Busca produtos reais e retorna apenas os válidos."""
    client = _client()
    raw = client.search_products(keyword, limit=limit)
    valid = []
    for p in raw:
        try:
            validate_product(p)
            valid.append(p)
        except ProductValidationError as exc:
            logger.warning("Filtro de busca: %s", exc)
    logger.info("Shopee: %d/%d produtos válidos para '%s'.", len(valid), len(raw), keyword)
    return valid


def details(url: str) -> Any:
    """Detalhes de um produto por URL, com validação."""
    client = _client()
    product = client.get_product_details(url)
    validate_product(product)
    return product


def trending(limit: int = 10) -> List[Any]:
    """Produtos em alta válidos, ordenados por vendas."""
    client = _client()
    raw = client.get_trending_products(limit=limit)
    valid = []
    for p in raw:
        try:
            validate_product(p)
            valid.append(p)
        except ProductValidationError as exc:
            logger.warning("Filtro trending: %s", exc)
    valid.sort(key=lambda p: p.sold, reverse=True)
    return valid


def affiliate_link(url: str, sub_id: Optional[str] = None) -> str:
    """Gera o link curto de afiliado (nunca inventa: fallback = URL original)."""
    client = _client()
    return client.generate_affiliate_link(url, sub_id=sub_id)


def snapshot(product: Any, affiliate_url: str = "") -> Dict[str, Any]:
    """
    Snapshot auditável do produto no momento da consulta.

    Inclui horário de consulta para justificar divergência de preço futura.
    """
    from models import utcnow_iso

    return {
        "item_id": product.item_id,
        "shop_id": product.shop_id,
        "name": product.name,
        "price": product.price,
        "price_original": product.price_original,
        "discount": product.discount,
        "rating": product.rating,
        "sold": product.sold,
        "images": product.images,
        "product_url": product.product_url,
        "affiliate_url": affiliate_url or product.affiliate_url,
        "available": True,
        "checked_at": utcnow_iso(),
    }
