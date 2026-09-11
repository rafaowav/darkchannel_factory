"""
shopee_client.py – Cliente para a API de Afiliados da Shopee Brasil.

Camada fina sobre a biblioteca oficial `shopee-afflib` (GraphQL
productOfferV2 / generateShortLink). Busca produtos reais, obtém detalhes,
gera links curtos de afiliado com subId e baixa imagens dos produtos.
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests
from dotenv import load_dotenv
from shopee_affiliate import ShopeeAffiliateSync, create_sync_client

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceções específicas da integração Shopee
# ---------------------------------------------------------------------------


class ShopeeAffiliateError(Exception):
    """Exceção base para erros da API Shopee Afiliados."""


class ShopeeAuthError(ShopeeAffiliateError):
    """Erro de autenticação/credenciais com a API."""


class ShopeeTimeoutError(ShopeeAffiliateError):
    """Timeout ao comunicar com a API Shopee."""


class ShopeeRateLimitError(ShopeeAffiliateError):
    """Rate limit excedido na API Shopee."""


class ShopeeProductNotFoundError(ShopeeAffiliateError):
    """Produto não encontrado ou indisponível."""


# ---------------------------------------------------------------------------
# Modelo de produto
# ---------------------------------------------------------------------------

_CURRENCY_RE = re.compile(r"[\d.,]+")


def _parse_brl(value: Union[str, float, int, None]) -> float:
    """Converte 'R$ 89,90' (formato locale da afflib) em float 89.90."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = _CURRENCY_RE.search(str(value))
    if not match:
        return 0.0
    raw = match.group(0)
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _to_float(value: Any, default: float = 0.0) -> float:
    """Conversão segura para float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    """Conversão segura para int."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class ShopeeProduct:
    """Representa um produto real da Shopee com dados de afiliado."""

    def __init__(
        self,
        item_id: str = "",
        shop_id: str = "",
        name: str = "",
        description: str = "",
        price: float = 0.0,
        price_original: float = 0.0,
        discount: float = 0.0,
        rating: float = 0.0,
        sold: int = 0,
        images: Optional[List[str]] = None,
        product_url: str = "",
        offer_link: str = "",
        affiliate_url: str = "",
        shop_name: str = "",
        commission_rate: float = 0.0,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.item_id = item_id
        self.shop_id = shop_id
        self.name = name
        self.description = description
        self.price = price
        self.price_original = price_original
        self.discount = discount
        self.rating = rating
        self.sold = sold
        self.images: List[str] = images or []
        self.product_url = product_url
        self.offer_link = offer_link
        self.affiliate_url = affiliate_url
        self.shop_name = shop_name
        self.commission_rate = commission_rate
        #: node bruto retornado pela afflib (necessário p/ download_product_image)
        self.raw: Dict[str, Any] = raw or {}

    @property
    def price_str(self) -> str:
        """Preço formatado em BRL para uso em copy/descrição."""
        return f"R$ {self.price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    @classmethod
    def from_node(cls, node: Dict[str, Any]) -> "ShopeeProduct":
        """Constrói um ShopeeProduct a partir de um node do productOfferV2."""
        image_url = node.get("imageUrl") or ""
        return cls(
            item_id=str(node.get("itemId", "")),
            shop_id=str(node.get("shopId", "")),
            name=node.get("productName", "") or "",
            description=node.get("description", "") or "",
            price=_parse_brl(node.get("price")),
            price_original=_parse_brl(node.get("originalPrice")),
            discount=_to_float(node.get("priceDiscountRate")),
            rating=_to_float(node.get("ratingStar")),
            sold=_to_int(node.get("sales")),
            images=[image_url] if image_url else [],
            product_url=node.get("productLink", "") or "",
            offer_link=node.get("offerLink", "") or "",
            affiliate_url=node.get("offerLink", "") or node.get("productLink", "") or "",
            shop_name=node.get("shopName", "") or "",
            commission_rate=_to_float(node.get("commissionRate")),
            raw=node,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Converte para dicionário (sem o payload bruto)."""
        data = {
            "item_id": self.item_id,
            "shop_id": self.shop_id,
            "name": self.name,
            "description": self.description,
            "price": self.price,
            "price_original": self.price_original,
            "discount": self.discount,
            "rating": self.rating,
            "sold": self.sold,
            "images": self.images,
            "product_url": self.product_url,
            "affiliate_url": self.affiliate_url,
            "shop_name": self.shop_name,
            "commission_rate": self.commission_rate,
        }
        return data

    def __repr__(self) -> str:
        return (
            f"ShopeeProduct(name='{self.name[:40]}', "
            f"price=R${self.price:.2f}, rating={self.rating}★, "
            f"sold={self.sold})"
        )


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


class ShopeeClient:
    """
    Cliente para a API de Afiliados da Shopee Brasil via shopee-afflib.

    Envolve o cliente síncrono da lib e normaliza as respostas para
    objetos :class:`ShopeeProduct`, com tratamento de erros específico.
    """

    def __init__(self, partner_id: str, partner_key: str) -> None:
        if not partner_id or not partner_key:
            raise ShopeeAuthError(
                "SHOPEE_PARTNER_ID e SHOPEE_PARTNER_KEY são obrigatórios."
            )
        self.partner_id = partner_id
        self.partner_key = partner_key
        self._lib: ShopeeAffiliateSync = create_sync_client(
            partner_id=partner_id, partner_key=partner_key
        )
        logger.info(
            "ShopeeClient inicializado via shopee-afflib (partner_id=%s…)",
            partner_id[:8],
        )

    # -- infra ---------------------------------------------------------------

    @staticmethod
    def _wrap_error(exc: Exception) -> ShopeeAffiliateError:
        """Converte exceções da lib/requests em exceções específicas."""
        if isinstance(exc, ShopeeAffiliateError):
            return exc
        if isinstance(exc, requests.exceptions.Timeout):
            return ShopeeTimeoutError("Timeout ao conectar com a API Shopee.")
        if isinstance(exc, requests.exceptions.HTTPError):
            status = getattr(exc.response, "status_code", 0)
            if status == 429:
                return ShopeeRateLimitError("Rate limit excedido. Aguarde e tente novamente.")
            if status in (401, 403):
                return ShopeeAuthError(
                    f"Credenciais Shopee inválidas/sem permissão (HTTP {status})."
                )
            if status == 404:
                return ShopeeProductNotFoundError("Produto/recurso não encontrado.")
            return ShopeeAffiliateError(f"Erro HTTP {status} na API Shopee.")
        if isinstance(exc, requests.exceptions.ConnectionError):
            return ShopeeAffiliateError("Não foi possível conectar à API Shopee.")
        return ShopeeAffiliateError(f"Erro na API Shopee: {exc}")

    @staticmethod
    def _extract_nodes(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extrai a lista de nodes da resposta GraphQL."""
        errors = data.get("errors")
        if errors:
            msg = str(errors[0].get("message", errors)) if errors else "erro desconhecido"
            if "not found" in msg.lower() or "invalid" in msg.lower():
                raise ShopeeProductNotFoundError(f"API Shopee: {msg}")
            raise ShopeeAffiliateError(f"Erro GraphQL da API Shopee: {msg}")
        nodes = (
            data.get("data", {})
            .get("productOfferV2", {})
            .get("nodes", [])
        )
        return nodes or []

    # -- busca ---------------------------------------------------------------

    def search_products(
        self,
        keyword: str,
        limit: int = 5,
        sort_type: Optional[int] = None,
    ) -> List[ShopeeProduct]:
        """
        Busca produtos reais na Shopee por palavra-chave.

        Args:
            keyword: Termo de busca (ex: 'teclado mecânico').
            limit: Máximo de resultados (padrão 5).
            sort_type: Ordenação da API (1, 2 ou 3); None = relevância padrão.

        Returns:
            Lista de :class:`ShopeeProduct` encontrados.

        Raises:
            ShopeeAffiliateError: Erros de rede/auth/API.
        """
        logger.info(
            "Shopee – buscando '%s' (limit=%d, sort=%s)", keyword, limit, sort_type
        )
        try:
            data = self._lib.get_product_offer(
                keyword=keyword, limit=limit, sortType=sort_type
            )
            nodes = self._extract_nodes(data)
        except Exception as exc:
            wrapped = self._wrap_error(exc)
            logger.error("Falha na busca Shopee ('%s'): %s", keyword, wrapped)
            raise wrapped from exc

        products = [ShopeeProduct.from_node(n) for n in nodes[:limit]]
        logger.info("Shopee – %d produtos encontrados para '%s'.", len(products), keyword)
        for i, p in enumerate(products, 1):
            logger.info(
                "  %d. %s – R$ %.2f – %.1f★ – %d vendidos",
                i, p.name[:50], p.price, p.rating, p.sold,
            )
        return products

    def get_product_details(self, url: str) -> ShopeeProduct:
        """
        Obtém detalhes reais de um produto a partir da URL.

        Aceita URLs completas (shopee.com.br/...-i.SHOP.ITEM) e links
        curtos (s.shopee.com.br/..., shope.ee/...).

        Args:
            url: URL do produto na Shopee.

        Returns:
            :class:`ShopeeProduct` com nome, preço, imagens, rating etc.

        Raises:
            ShopeeProductNotFoundError: Produto não encontrado/indisponível.
            ShopeeAffiliateError: Demais erros da API.
        """
        if not url or not url.startswith("http"):
            raise ShopeeProductNotFoundError(f"URL inválida de produto: {url!r}")

        logger.info("Shopee – buscando detalhes: %s", url[:90])
        try:
            data = self._lib.get_product_offer(url=url)
            nodes = self._extract_nodes(data)
        except ShopeeAffiliateError:
            raise
        except Exception as exc:
            wrapped = self._wrap_error(exc)
            logger.error("Falha ao buscar detalhes (%s): %s", url[:60], wrapped)
            raise wrapped from exc

        if not nodes:
            raise ShopeeProductNotFoundError(
                f"Produto não encontrado ou indisponível: {url[:90]}"
            )

        product = ShopeeProduct.from_node(nodes[0])
        logger.info("Shopee – produto carregado: %s", product)
        logger.info("  Imagens disponíveis: %d", len(product.images))
        return product

    def get_trending_products(
        self,
        category_id: Optional[int] = None,
        limit: int = 10,
    ) -> List[ShopeeProduct]:
        """
        Busca produtos em alta (ordenados por popularidade/vendas).

        Args:
            category_id: ID de categoria da Shopee (None = geral).
            limit: Quantidade de resultados.

        Returns:
            Lista de :class:`ShopeeProduct` mais relevantes/mais vendidos.
        """
        logger.info(
            "Shopee – buscando trending (category=%s, limit=%d)", category_id, limit
        )
        try:
            data = self._lib.get_product_offer(
                limit=limit,
                sortType=1,  # popularidade
                productCatId=category_id,
            )
            nodes = self._extract_nodes(data)
        except Exception as exc:
            wrapped = self._wrap_error(exc)
            logger.error("Falha na busca trending: %s", wrapped)
            raise wrapped from exc

        products = [ShopeeProduct.from_node(n) for n in nodes[:limit]]
        # Garante ordenação pelo mais vendido como desempate/fallback
        products.sort(key=lambda p: p.sold, reverse=True)
        logger.info("Shopee – %d produtos trending encontrados.", len(products))
        return products

    # -- link de afiliado ------------------------------------------------------

    def generate_affiliate_link(
        self,
        url: str,
        sub_id: Optional[str] = None,
    ) -> str:
        """
        Gera o link curto de afiliado (com subId) para uma URL de produto.

        Args:
            url: URL original do produto na Shopee.
            sub_id: Sub-ID de rastreamento (ex: 'video_20260910').

        Returns:
            Link curto de afiliado (s.shopee.com.br/...). Em falha,
            retorna a URL original (com warning no log).
        """
        sub_ids = [sub_id] if sub_id else None
        logger.info(
            "Shopee – gerando link afiliado (sub_id=%s) para %s",
            sub_id, url[:80],
        )
        try:
            short_link = self._lib.generate_short_url(url, sub_ids=sub_ids)
            logger.info("Link afiliado gerado: %s", short_link)
            return short_link
        except Exception as exc:
            # A API rejeita subIds fora do padrão de 5 dígitos → tenta sem subId
            if sub_ids and "sub id" in str(exc).lower():
                logger.warning(
                    "subId %r rejeitado pela API Shopee. Gerando link sem subId.",
                    sub_id,
                )
                try:
                    short_link = self._lib.generate_short_url(url)
                    logger.info("Link afiliado gerado (sem subId): %s", short_link)
                    return short_link
                except Exception as exc2:
                    logger.warning(
                        "Falha ao gerar link de afiliado (%s). Usando URL original.",
                        exc2,
                    )
                    return url
            logger.warning(
                "Falha ao gerar link de afiliado (%s). Usando URL original.", exc
            )
            return url

    # -- imagens ---------------------------------------------------------------

    def download_product_image(
        self,
        product: Union[ShopeeProduct, Dict[str, Any]],
        save_dir: Union[str, Path],
        filename: Optional[str] = None,
    ) -> Optional[str]:
        """
        Baixa a imagem real de um produto usando ``download_product_image``
        da shopee-afflib.

        Args:
            product: ShopeeProduct (com ``raw``) ou dict node da API.
            save_dir: Diretório de destino da imagem.
            filename: Nome do arquivo (opcional; padrão <item_id>.jpg).

        Returns:
            Path local da imagem salva, ou None em caso de erro.
        """
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

        if isinstance(product, ShopeeProduct):
            node = product.raw
            item_id = product.item_id or "produto"
        else:
            node = {"data": {"productOfferV2": {"nodes": [product]}}}
            item_id = str(product.get("itemId", "produto"))

        filepath = save_dir / (filename or f"{item_id}.jpg")
        try:
            result = self._lib.download_product_image(node, save_path=str(filepath))
        except Exception as exc:
            logger.warning("Erro ao baixar imagem do item %s: %s", item_id, exc)
            return None

        if result and Path(result).exists():
            logger.info("Imagem baixada: %s", result)
            return str(result)
        logger.warning("Download de imagem falhou para item %s.", item_id)
        return None

    def download_product_images(
        self,
        product: ShopeeProduct,
        save_dir: Union[str, Path],
        max_images: int = 5,
    ) -> List[str]:
        """
        Baixa até ``max_images`` imagens reais de um produto.

        A API de ofertas expõe a imagem principal; se houver mais URLs em
        ``product.images``, todas são tentadas sequencialmente.

        Args:
            product: Produto com dados/imagens.
            save_dir: Diretório de destino.
            max_images: Máximo de imagens a baixar.

        Returns:
            Lista de paths locais das imagens baixadas.
        """
        downloaded: List[str] = []
        urls = product.images[:max_images]

        for i, img_url in enumerate(urls):
            if not img_url:
                continue
            filename = f"shopee_{product.item_id}_{i}.jpg"
            filepath = Path(save_dir) / filename
            try:
                resp = requests.get(img_url, timeout=30, stream=True)
                resp.raise_for_status()
                with open(filepath, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                downloaded.append(str(filepath))
                logger.info("Imagem %d/%d salva: %s", i + 1, len(urls), filepath)
            except requests.RequestException as exc:
                logger.warning("Falha ao baixar imagem %s: %s", img_url[:80], exc)

        if not downloaded and product.raw:
            # fallback: usa download_product_image da afflib (imagem principal)
            path = self.download_product_image(product, save_dir)
            if path:
                downloaded.append(path)

        logger.info(
            "Shopee – %d imagens baixadas para '%s'.",
            len(downloaded), product.name[:40],
        )
        return downloaded


# ---------------------------------------------------------------------------
# Funções de módulo (interface pública pedida no contrato)
# ---------------------------------------------------------------------------

_default_client: Optional[ShopeeClient] = None


def init_shopee_client(
    partner_id: Optional[str] = None,
    partner_key: Optional[str] = None,
) -> Optional[ShopeeClient]:
    """
    Inicializa o cliente Shopee autenticado.

    Args:
        partner_id: App ID da API Shopee (lê SHOPEE_PARTNER_ID se None).
        partner_key: Chave secreta (lê SHOPEE_PARTNER_KEY se None).

    Returns:
        ShopeeClient autenticado, ou None se credenciais ausentes/inválidas.
    """
    global _default_client
    import os

    partner_id = partner_id or os.getenv("SHOPEE_PARTNER_ID", "")
    partner_key = partner_key or os.getenv("SHOPEE_PARTNER_KEY", "")

    placeholders = {"seu_app_id_da_api_shopee", "sua_chave_secreta_da_api_shopee"}
    if (
        not partner_id or not partner_key
        or partner_id in placeholders
        or partner_key in placeholders
    ):
        logger.warning(
            "SHOPEE_PARTNER_ID / SHOPEE_PARTNER_KEY não configurados. "
            "Integração Shopee desabilitada."
        )
        return None

    if _default_client is None:
        try:
            _default_client = ShopeeClient(partner_id, partner_key)
        except ShopeeAuthError as exc:
            logger.error("Falha ao inicializar ShopeeClient: %s", exc)
            return None
    return _default_client


def _require_client() -> ShopeeClient:
    """Retorna o cliente singleton ou lança erro claro."""
    client = init_shopee_client()
    if client is None:
        raise ShopeeAuthError(
            "Cliente Shopee não configurado. Defina SHOPEE_PARTNER_ID e "
            "SHOPEE_PARTNER_KEY no .env"
        )
    return client


def search_products(keyword: str, limit: int = 5) -> List[ShopeeProduct]:
    """Busca produtos reais na Shopee (função de nível de módulo)."""
    return _require_client().search_products(keyword, limit=limit)


def get_product_details(url: str) -> ShopeeProduct:
    """Detalhes reais de um produto por URL (função de nível de módulo)."""
    return _require_client().get_product_details(url)


def generate_affiliate_link(url: str, sub_id: Optional[str] = None) -> str:
    """Gera link curto de afiliado com subId (função de nível de módulo)."""
    return _require_client().generate_affiliate_link(url, sub_id=sub_id)


# ---------------------------------------------------------------------------
# Teste rápido
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.INFO)
    client = init_shopee_client()

    if client is None:
        print("Configure SHOPEE_PARTNER_ID e SHOPEE_PARTNER_KEY no .env")
        exit(1)

    keyword = os.getenv("TEST_KEYWORD", "teclado mecanico")
    print(f"\nBuscando: {keyword}")
    products = client.search_products(keyword, limit=3)
    for i, p in enumerate(products, 1):
        print(f"{i}. {p.name[:60]} – {p.price_str} – {p.rating}★ – {p.sold} vendidos")

    if products:
        print(f"\nGerando link de afiliado para: {products[0].name[:40]}")
        link = client.generate_affiliate_link(
            products[0].product_url or products[0].affiliate_url,
            sub_id="teste_modulo",
        )
        print(f"Link: {link}")
