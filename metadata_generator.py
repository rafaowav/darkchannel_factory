"""
metadata_generator.py – Geração automática de metadados YouTube e miniaturas.

Produz títulos otimizados para SEO, descrições com links de afiliado e
declaração FTC, tags, categoria/idioma e thumbnails 1280x720 via Pillow,
para os três tipos de vídeo do projeto: GLOBAL, BRASIL e SHOPEE.
"""

import json
import logging
import re
import textwrap
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from dateutil import tz as dateutil_tz
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
ASSETS_DIR = BASE_DIR / "assets"

THUMB_W: int = 1280
THUMB_H: int = 720

# Palavras vazias (EN + PT) removidas em slugs de arquivo/thumbnail
_STOPWORDS: frozenset = frozenset({
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "with",
    "from", "by", "is", "are", "was", "were", "be", "been", "this", "that",
    "it", "its", "as", "but", "not", "no", "so", "if", "than",
    "o", "os", "um", "uma", "uns", "umas", "de", "da", "do", "dos", "das",
    "em", "na", "no", "nas", "nos", "e", "ou", "que", "para", "por", "com",
    "sem", "ao", "aos", "se", "como", "mais", "muito", "isso", "este", "esta",
})

FTC_DISCLAIMER_PT: str = (
    "⚠️ Este vídeo contém links de afiliado. Se você comprar através deles, "
    "podemos receber uma comissão sem custo extra para você. Isso ajuda o "
    "canal a continuar produzindo conteúdo."
)
FTC_DISCLAIMER_EN: str = (
    "⚠️ This video contains affiliate links. If you purchase through them, "
    "we may earn a commission at no extra cost to you."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    """Carrega fonte TTF do sistema com fallbacks."""
    candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ] if bold else [
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _title_case(text: str) -> str:
    """Title Case em inglês preservando palavras pequenas."""
    small = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "behind"}
    words = text.split()
    out: List[str] = []
    for i, w in enumerate(words):
        lw = w.lower()
        out.append(lw if (i != 0 and lw in small) else w.capitalize())
    return " ".join(out)


def _truncate_title(title: str, limit: int = 60) -> str:
    """Garante título dentro do limite de caracteres sem cortar palavra no meio."""
    if len(title) <= limit:
        return title
    cut = title[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,:-")


def _short_phrase(titulo: str, max_words: int = 4) -> str:
    """Versão curta do título (max 4 palavras) para a thumbnail."""
    stop = {"the", "a", "an", "of", "for", "how", "why", "to", "in", "on", "e", "de", "do", "da", "por", "que"}
    words = [w for w in re.findall(r"[\wÀ-ÿ]+", titulo)]
    key = [w for w in words if w.lower() not in stop] or words
    return " ".join(key[:max_words]).upper()


def _next_schedule_slot(now: Optional[datetime] = None) -> str:
    """Próximo slot de publicação: amanhã às 15:00 horário de Brasília (-03:00)."""
    now = now or datetime.now(dateutil_tz.tzlocal())
    brt = dateutil_tz.gettz("America/Sao_Paulo")
    target = (now.astimezone(brt) + timedelta(days=1)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    return target.isoformat()


# ---------------------------------------------------------------------------
# Metadados por tipo
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Gemini – geração real de metadados (título, descrição, capítulos, tags)
# ---------------------------------------------------------------------------

_gemini_client = None  # lazy init
GEMINI_MODEL: str = "gemini-3.5-flash"


def _get_gemini():  # type: ignore[no-untyped-def]
    """Inicializa o cliente Gemini sob demanda (usa GEMINI_API_KEY do .env)."""
    global _gemini_client
    if _gemini_client is None:
        import os

        from dotenv import load_dotenv
        from google import genai

        load_dotenv()
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key or api_key == "sua_chave_aqui":
            return None
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def _gemini_text(prompt: str) -> str:
    """Chama o Gemini e retorna texto puro (string vazia se indisponível)."""
    client = _get_gemini()
    if client is None:
        logger.warning("Gemini indisponível para metadados – usando templates.")
        return ""
    try:
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return (response.text or "").strip()
    except Exception as exc:
        logger.error("Erro no Gemini ao gerar metadados: %s", exc)
        return ""


def _parse_json_block(raw: str) -> Optional[Dict[str, Any]]:
    """Extrai o primeiro objeto JSON válido de uma resposta do modelo."""
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _clean_sentence(text: str) -> str:
    """Normaliza uma frase vinda do roteiro (remove quebras/excesso de espaços)."""
    return re.sub(r"\s+", " ", text).strip()


def _split_sentences(script: str) -> List[str]:
    """Divide o roteiro em frases completas."""
    parts = re.split(r"(?<=[.!?])\s+", script.strip())
    return [p.strip() for p in parts if len(p.strip()) > 15]


def _clean_hashtag(tag: str) -> str:
    """Converte tag em hashtag válida (sem espaços): 'supply chain' → 'supplychain'."""
    cleaned = re.sub(r"[^a-z0-9à-ÿ]", "", tag.lower())
    return cleaned or "tech"


def _extract_keywords(script: str, limit: int = 8) -> List[str]:
    """Extrai palavras-chave simples do roteiro (sem stopwords relevantes)."""
    stop = set("""
    the a an and or but of in on at to for from with without that this these those
    it its is are was were be been being you your we our they their he she his her
    what when how why who which not no yes so as by if then than there here about
    e a o de da do que para com por se no na um uma mais como ou até se
    """.split())
    words = re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ']+", script.lower())
    seen: List[str] = []
    freq: Dict[str, int] = {}
    for w in words:
        if w in stop or len(w) < 4:
            continue
        freq[w] = freq.get(w, 0) + 1
    for w in sorted(freq, key=freq.get, reverse=True):
        if w not in seen:
            seen.append(w)
        if len(seen) >= limit:
            break
    return seen


def _gemini_metadata(
    tipo: str,
    tema: str,
    roteiro: str,
    contexto_extra: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Pede ao Gemini um pacote de metadados YouTube em JSON.

    Args:
        tipo: 'global', 'brasil' ou 'shopee'.
        tema: Tema/nome do produto.
        roteiro: Narração completa (truncada para caber no prompt).
        contexto_extra: Dados reais adicionais (preço, links etc.).

    Returns:
        Dict com titulo/descricao/tags/capitulos, ou None se falhar.
    """
    script_cut = roteiro[:6000]
    lang_rule = (
        "Write title and description in ENGLISH."
        if tipo == "global"
        else "Escreva título e descrição em PORTUGUÊS DO BRASIL."
    )
    tone = {
        "global": "Netflix/Vox documentary channel, curiosity-driven, zero clickbait lie",
        "brasil": "tech reviewer speaking to Brazilian geeks/devs, direct and honest",
        "shopee": "viral achadinho/deals channel, urgent but truthful",
    }[tipo]

    prompt = f"""You are a YouTube SEO expert for the channel below. Based ONLY on the
video narration provided, produce metadata that is specific to THIS video — never generic.

VIDEO TYPE: {tipo}
TOPIC/PRODUCT: {tema}
CHANNEL STYLE: {tone}
{contexto_extra}

NARRATION (transcript):
\"\"\"{script_cut}\"\"\"

Return ONLY a valid JSON object (no markdown fences) with exactly these keys:
- "titulo": max 60 chars, {lang_rule} Include the core keyword; curiosity hook allowed but must be true to the content. No emojis, no ALL CAPS, no trailing period.
- "descricao": {lang_rule} 3 paragraphs separated by \\n\\n:
  P1: 2-3 sentences summarizing THIS specific video using concrete facts from the narration (numbers, names, dates).
  P2: timestamps/chapters of the actual structure of this video, one per line, format "00:00 Short chapter name" (5-8 chapters derived from the narration flow).
  P3: one engaging closing line inviting comments (ask a real question raised by the video) + subscribe reminder.
- "tags": array of 12-15 lowercase keyword phrases genuinely present or implied in the narration (mix short-tail and long-tail), no duplicates, no topic-unrelated filler.

JSON only."""
    raw = _gemini_text(prompt)
    data = _parse_json_block(raw)
    if not data or not data.get("titulo") or not data.get("descricao"):
        logger.warning("Gemini não retornou metadados válidos (%s).", tipo)
        return None
    return data


def _metadata_global(tema: str, roteiro: str) -> Dict[str, Any]:
    """Metadados para vídeo GLOBAL (inglês, documentário)."""
    frases = _split_sentences(roteiro)
    kws = _extract_keywords(roteiro, limit=4)

    ai = _gemini_metadata("global", tema, roteiro)
    if ai:
        titulo = _truncate_title(str(ai["titulo"]).strip().rstrip("."), 60)
        descricao = str(ai["descricao"]).strip()
        tags = [str(t).strip().lower() for t in ai.get("tags", []) if str(t).strip()]
    else:
        hooks = [
            f"The Secret {tema.title()} Nobody Talks About",
            f"Why {tema.title()} Changes Everything",
            f"The Hidden Truth Behind {tema.title()}",
        ]
        if kws:
            hooks.append(f"{_title_case(kws[0])}: The Untold Story of {tema.title()}")
        titulo = _truncate_title(max(hooks, key=len), 60)
        resumo = frases[0] if frases else f"{tema} explained."
        contexto = " ".join(frases[1:3])
        descricao = (
            f"{resumo}\n"
            f"In this documentary-style video, we break down {tema.lower()} — what it "
            f"means, why it matters, and what most people never realize about it.\n\n"
            f"{contexto}\n\n"
            f"Subscribe for weekly deep dives into technology, finance and geopolitics."
        )
        tags = ["documentary", "technology", tema.lower()] + kws + ["ai", "geopolitics"]

    # normalização final garantida
    unique_tags: List[str] = []
    for t in tags + kws:
        t = re.sub(r"\s+", " ", t.strip().lower())
        if t and len(t) > 2 and t not in unique_tags:
            unique_tags.append(t)

    hashtags = "#" + " #".join(_clean_hashtag(t) for t in unique_tags[:3])
    descricao = (
        f"{descricao}\n\n"
        f"{FTC_DISCLAIMER_EN}\n\n"
        f"{hashtags} #technology #documentary"
    )

    return {
        "titulo": titulo,
        "descricao": descricao,
        "tags": unique_tags[:15],
        "categoria": "Science & Technology",
        "idioma": "en",
    }


def _metadata_brasil(
    tema: str,
    roteiro: str,
    produtos: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Metadados para vídeo BRASIL (português técnico, com CTA de afiliados)."""
    frases = _split_sentences(roteiro)
    kws = _extract_keywords(roteiro, limit=4)
    produtos = produtos or []

    contexto_extra = ""
    if produtos:
        contexto_extra = "PRODUCTS/AFFILIATE LINKS TO INCLUDE IN DESCRIPTION:\n" + "\n".join(
            f"- {p['nome']}: {p.get('link', '')}" for p in produtos
        )

    ai = _gemini_metadata("brasil", tema, roteiro, contexto_extra)
    if ai:
        titulo = _truncate_title(str(ai["titulo"]).strip().rstrip("."), 60)
        descricao = str(ai["descricao"]).strip()
        tags = [str(t).strip().lower() for t in ai.get("tags", []) if str(t).strip()]
    else:
        hooks = [
            f"Como Montar {tema.title()} Sem Gastar Muito",
            f"{tema.title()}: O Guia Definitivo de 2026",
            f"Vale a Pena? {tema.title()} na Prática",
        ]
        if kws:
            hooks.append(f"{tema.title()} Bom e Barato: Dicas de {kws[0].title()}")
        titulo = _truncate_title(max(hooks, key=len), 60)
        problema = frases[0] if frases else f"Tudo sobre {tema.lower()}."
        descricao = (
            f"{problema}\n"
            f"Nesse vídeo eu mostro tudo sobre {tema.lower()} na prática — testes, "
            f"especificações e comparativos diretos pra você não errar na compra.\n\n"
            f"Inscreva-se no canal para mais reviews técnicos toda semana."
        )
        tags = ["review", "tecnologia", tema.lower(), "shopee", "custo beneficio"] + kws

    # Bloco de produtos/links SEMPRE montado por código (link nunca é inventado)
    if produtos:
        bloco = "\n\n📦 Links dos produtos mencionados:\n"
        for p in produtos:
            nome_p = p["nome"][:70]
            link_p = p.get("link", "").strip()
            bloco += f"• {nome_p}" + (f": {link_p}" if link_p else "") + "\n"
        descricao += bloco

    unique_tags: List[str] = []
    for t in tags + kws:
        t = re.sub(r"\s+", " ", t.strip().lower())
        if t and len(t) > 2 and t not in unique_tags:
            unique_tags.append(t)

    hashtags = "#" + " #".join(_clean_hashtag(t) for t in unique_tags[:3])
    descricao = (
        f"{descricao}\n\n"
        f"{FTC_DISCLAIMER_PT}\n\n"
        f"{hashtags} #setup #shopee #tecnologia #custobeneficio #comprasonline #techbrasil"
    )

    return {
        "titulo": titulo,
        "descricao": descricao,
        "tags": unique_tags[:15],
        "categoria": "Science & Technology",
        "idioma": "pt-BR",
    }


def _metadata_shopee(tema: str, roteiro: str, extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Metadados para vídeo SHOPEE (achadinho vertical, link nas 3 primeiras linhas)."""
    extras = extras or {}
    price = float(extras.get("preco", 0) or 0)
    discount = float(extras.get("desconto", 0) or 0)
    rating = float(extras.get("rating", 0) or 0)
    sold = int(extras.get("vendidos", 0) or 0)
    link = str(extras.get("link_afiliado", "") or "")
    original_price = float(extras.get("preco_original", 0) or 0)

    def brl(v: float) -> str:
        return f"R$ {v:.2f}".replace(".", ",").replace(",", "X").replace(".", ",").replace("X", ".")

    # Título: Gemini cria o gancho; fallback garante o padrão "Achadinho Shopee:"
    contexto_extra = "REAL PRODUCT DATA: " + ", ".join(filter(None, [
        brl(price) if price > 0 else "",
        f"{discount:.0f}% OFF" if discount > 0 else "",
        f"rating {rating:.1f}/5" if rating > 0 else "",
        f"{sold} sold" if sold > 0 else "",
    ]))
    ai = _gemini_metadata("shopee", tema, roteiro, contexto_extra)

    preco_fmt = f" {brl(price)}" if price > 0 else ""
    if ai and str(ai["titulo"]).strip():
        titulo = _truncate_title(str(ai["titulo"]).strip().rstrip("."), 60)
        if not titulo.lower().startswith("achadinho"):
            nome_curto = tema[:38].strip()
            titulo = _truncate_title(f"Achadinho Shopee: {nome_curto}{preco_fmt}", 60)
    else:
        nome_curto = tema[:38].strip()
        titulo = _truncate_title(f"Achadinho Shopee: {nome_curto}{preco_fmt}", 60)

    # Partes reais (sempre por código, com dados da API)
    linha_preco = ""
    if original_price > price > 0:
        linha_preco = f"💰 De {brl(original_price)} por apenas {brl(price)}"
        if discount > 0:
            linha_preco += f" ({discount:.0f}% OFF)"
    elif price > 0:
        linha_preco = f"💰 Apenas {brl(price)}"

    meta_linha = ""
    if rating > 0:
        meta_linha += f"⭐ {rating:.1f}/5"
    if sold > 0:
        meta_linha += (f" | 🛒 {sold} vendidos" if meta_linha else f"🛒 {sold} vendidos")

    # Resumo específico: primeira frase REAL do roteiro (não template genérico)
    frases = _split_sentences(roteiro)
    resumo = frases[0] if frases else f"Esse achadinho da Shopee resolve de verdade."

    # Descrição: corpo do Gemini (sem o bloco de links, que montamos por código)
    corpo = ""
    if ai:
        corpo = str(ai["descricao"]).strip()
    tags_ai = [str(t).strip().lower() for t in (ai or {}).get("tags", []) if str(t).strip()]

    descricao_parts = [f"🛍️ {tema}"]
    if link:
        descricao_parts.append(f"🔗 Compre aqui: {link}")
    if linha_preco:
        descricao_parts.append(linha_preco)
    if meta_linha:
        descricao_parts.append(meta_linha)
    descricao_parts.append("")
    if corpo:
        descricao_parts.append(corpo)
    else:
        descricao_parts.append(
            f"{resumo}\n"
            f"Clica no link acima para garantir o seu antes que acabe!"
        )
    descricao_parts.append("")
    descricao_parts.append(FTC_DISCLAIMER_PT)
    descricao_parts.append("")
    descricao_parts.append("#achadinhoshopee #shopeebrasil #oferta #comprasonline #promocao")

    tags = ["achadinho shopee", "shopee", "shopee brasil", tema.lower(), "oferta", "promoção"]
    tags += tags_ai + _extract_keywords(roteiro, limit=5)
    unique_tags: List[str] = []
    for t in tags:
        t = re.sub(r"\s+", " ", t.strip().lower())
        if t and len(t) > 2 and t not in unique_tags:
            unique_tags.append(t)

    return {
        "titulo": titulo,
        "descricao": "\n".join(descricao_parts),
        "tags": unique_tags[:15],
        "categoria": "Education",
        "idioma": "pt-BR",
    }


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def generate_video_metadata(
    tipo_video: str,
    tema_ou_produto: str,
    roteiro: str,
    produtos: Optional[List[Dict[str, str]]] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Gera título, descrição, tags, categoria e idioma para um vídeo do canal.

    Args:
        tipo_video: 'global', 'brasil' ou 'shopee'.
        tema_ou_produto: Tema do vídeo ou nome do produto.
        roteiro: Texto completo da narração (base para resumo/tags).
        produtos: (BRASIL) lista de dicts {'nome','link'} para o bloco de afiliados.
        extras: (SHOPEE) dict com preco, desconto, rating, vendidos, link_afiliado,
                preco_original vindos da API Shopee.

    Returns:
        Dict com: titulo, descricao, tags, categoria, idioma.

    Raises:
        ValueError: se tipo_video for desconhecido.
    """
    tipo = tipo_video.strip().lower()
    if tipo == "global":
        meta = _metadata_global(tema_ou_produto, roteiro)
    elif tipo == "brasil":
        meta = _metadata_brasil(tema_ou_produto, roteiro, produtos)
    elif tipo == "shopee":
        meta = _metadata_shopee(tema_ou_produto, roteiro, extras)
    else:
        raise ValueError(f"tipo_video inválido: {tipo_video!r} (use global|brasil|shopee)")

    logger.info(
        "Metadata %s → título: '%s' (%d chars), %d tags",
        tipo, meta["titulo"], len(meta["titulo"]), len(meta["tags"]),
    )
    return meta


def _normalize_filename(text: str, max_length: int = 30) -> str:
    """
    Slug descritivo para nomes de arquivo (minúsculas, sem acentos/símbolos,
    sem stopwords EN/PT, truncado mantendo palavras completas).

    Mantém cópia local idêntica a engine.normalize_filename() para evitar
    import circular entre engine e metadata_generator.
    """
    if not text or not text.strip():
        return "video"
    slug = unicodedata.normalize("NFKD", text.lower())
    slug = "".join(c for c in slug if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", " ", slug).strip()
    words = [w for w in slug.split() if w not in _STOPWORDS] or slug.split() or ["video"]
    result = ""
    for w in words:
        candidate = f"{result}_{w}" if result else w
        if len(candidate) > max_length:
            break
        result = candidate
    return result or words[0][:max_length]


def save_metadata_file(
    video_path: Union[str, Path],
    thumbnail_path: Union[str, Path],
    tipo: str,
    metadata: Dict[str, Any],
    tema_ou_produto: str = "",
) -> str:
    """
    Salva o JSON final de metadados no padrão
    output/metadata_{tipo}_{tema_normalizado}_{timestamp}.json.

    Args:
        video_path: Caminho do MP4 gerado.
        thumbnail_path: Caminho da thumbnail JPG.
        tipo: 'global', 'brasil' ou 'shopee'.
        metadata: Retorno de generate_video_metadata().
        tema_ou_produto: Tema/nome usado no slug do arquivo.

    Returns:
        Path do arquivo JSON salvo.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _normalize_filename(tema_ou_produto or metadata.get("titulo", ""))
    payload = {
        "video_file": str(video_path),
        "thumbnail_file": str(thumbnail_path),
        "tipo": tipo,
        "titulo": metadata.get("titulo", ""),
        "descricao": metadata.get("descricao", ""),
        "tags": metadata.get("tags", []),
        "categoria": metadata.get("categoria", ""),
        "idioma": metadata.get("idioma", ""),
        "agendar_para": _next_schedule_slot(),
    }
    out = OUTPUT_DIR / f"metadata_{tipo}_{slug}_{ts}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info("Metadados salvos: %s", out)
    return str(out)


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------


def _fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Redimensiona cobrindo (cover) e centraliza crop para w x h."""
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    resized = img.resize((int(iw * scale), int(ih * scale)), Image.LANCZOS)
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    y: int,
    fill: str,
    stroke_width: int = 0,
    stroke_fill: str = "black",
) -> None:
    """Desenha texto horizontalmente centralizado em THUMB_W."""
    draw.text((THUMB_W // 2, y), text, font=font, fill=fill,
              anchor="mm", stroke_width=stroke_width, stroke_fill=stroke_fill)


def generate_thumbnail(
    tipo_video: str,
    titulo: str,
    imagem_fundo: Optional[str] = None,
    output_path: Optional[str] = None,
    preco_str: str = "",
    tema_ou_produto: str = "",
) -> str:
    """
    Gera thumbnail 1280x720 otimizada por tipo de vídeo.

    Nome padrão do arquivo: thumb_{tipo}_{tema_normalizado}_{timestamp}.jpg

    Args:
        tipo_video: 'global', 'brasil' ou 'shopee'.
        titulo: Título do vídeo (versão curta será usada no overlay).
        imagem_fundo: Imagem de fundo opcional (ex: foto do produto).
        output_path: Destino JPG explícito (opcional).
        preco_str: Preço formatado para badge na thumbnail SHOPEE.
        tema_ou_produto: Tema/nome usado no slug do nome do arquivo.

    Returns:
        Path da thumbnail salva.
    """
    tipo = tipo_video.strip().lower()
    if tipo not in ("global", "brasil", "shopee"):
        raise ValueError(f"tipo_video inválido: {tipo_video!r}")

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = _normalize_filename(tema_ou_produto or titulo)
        output_path = str(OUTPUT_DIR / f"thumb_{tipo}_{slug}_{ts}.jpg")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    canvas = Image.new("RGB", (THUMB_W, THUMB_H), (26, 26, 26))
    draw = ImageDraw.Draw(canvas)
    short = _short_phrase(titulo)
    phrase_font = _load_font(96)

    if tipo == "global":
        # Fundo escuro #1a1a1a + elementos geométricos abstratos + texto amarelo
        draw.rectangle([0, 0, THUMB_W, THUMB_H], fill=(26, 26, 26))
        for i, (x0, y0, x1, y1) in enumerate([
            (60, 80, 420, 640), (900, 40, 1240, 300), (820, 420, 1220, 680),
        ]):
            color = (40, 40, 46) if i % 2 == 0 else (52, 48, 30)
            draw.rounded_rectangle([x0, y0, x1, y1], radius=28, outline=color, width=10)
        draw.line([(0, THUMB_H - 60), (THUMB_W, THUMB_H - 60)], fill=(255, 230, 0), width=12)
        lines = textwrap.wrap(short, width=14)[:2]
        y = THUMB_H // 2 - (len(lines) - 1) * 60
        for line in lines:
            _draw_centered_text(draw, line, phrase_font, y, "#FFE600", stroke_width=3, stroke_fill="black")
            y += 120

    elif tipo == "brasil":
        # Gradiente escuro vertical + texto branco com borda preta
        top_c, bot_c = (10, 14, 32), (28, 10, 48)
        for y in range(THUMB_H):
            ratio = y / THUMB_H
            c = tuple(int(a + (b - a) * ratio) for a, b in zip(top_c, bot_c))
            draw.line([(0, y), (THUMB_W, y)], fill=c)
        draw.rectangle([0, 0, THUMB_W, 14], fill=(237, 20, 91))
        if imagem_fundo and Path(imagem_fundo).exists():
            try:
                icon = Image.open(imagem_fundo).convert("RGBA")
                icon.thumbnail((300, 300), Image.LANCZOS)
                canvas.paste(icon, (THUMB_W - 360, THUMB_H - icon.height - 40), icon)
                draw = ImageDraw.Draw(canvas)
            except Exception as exc:
                logger.warning("Falha ao usar ícone na thumb: %s", exc)
        lines = textwrap.wrap(short, width=14)[:2]
        y = THUMB_H // 2 - (len(lines) - 1) * 60
        for line in lines:
            _draw_centered_text(draw, line, phrase_font, y, "white", stroke_width=6, stroke_fill="black")
            y += 120

    else:  # shopee
        # Fundo: imagem do produto desfocada; destaque ACHADINHO amarelo + preço verde
        if imagem_fundo and Path(imagem_fundo).exists():
            try:
                bg = _fit_cover(Image.open(imagem_fundo).convert("RGB"), THUMB_W, THUMB_H)
                bg = bg.filter(ImageFilter.GaussianBlur(radius=18))
                dark = Image.new("RGB", (THUMB_W, THUMB_H), (0, 0, 0))
                bg = Image.blend(bg, dark, 0.45)
                canvas = bg
                draw = ImageDraw.Draw(canvas)
            except Exception as exc:
                logger.warning("Falha ao desfocar fundo da thumb: %s", exc)
        else:
            draw.rectangle([0, 0, THUMB_W, THUMB_H], fill=(237, 20, 91))

        big = _load_font(130)
        _draw_centered_text(draw, "ACHADINHO", big, 180, "#FFE600", stroke_width=8, stroke_fill="black")
        name_font = _load_font(56)
        name_lines = textwrap.wrap(_short_phrase(titulo, max_words=5), width=18)[:2]
        ny = 300
        for line in name_lines:
            _draw_centered_text(draw, line, name_font, ny, "white", stroke_width=4, stroke_fill="black")
            ny += 70
        if preco_str:
            price_font = _load_font(110)
            _draw_centered_text(draw, preco_str, price_font, THUMB_H - 150, "#00FF00",
                                stroke_width=6, stroke_fill="black")
        # badge canto
        draw.rounded_rectangle([THUMB_W - 300, 24, THUMB_W - 24, 104], radius=18, fill=(255, 230, 0))
        badge_font = _load_font(44)
        draw.text((THUMB_W - 162, 64), "SHOPEE", font=badge_font, fill="black", anchor="mm")

    canvas.save(output_path, "JPEG", quality=90)
    logger.info("Thumbnail %s salva: %s", tipo, output_path)
    return output_path


# ---------------------------------------------------------------------------
# Teste rápido / exemplos
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.INFO)
    OUTPUT_DIR.mkdir(exist_ok=True)

    demo_script_en = (
        "Your smartphone looks like magic, but behind every screen sits a quiet "
        "monopoly. A handful of companies control the chips, the rare earths and "
        "the patents that make modern phones possible."
    )
    demo_script_pt = (
        "Montar um setup gamer bom e barato é possível quando você sabe onde gastar. "
        "Nesse vídeo testei teclado mecânico, mouse e headset abaixo de quinhentos reais."
    )
    demo_script_sp = (
        "Gente, olha esse suporte de monitor articulado que achei na Shopee por "
        "quarenta e cinco reais. Ele ajusta altura e rotação e liberou muito espaço na mesa."
    )

    m1 = generate_video_metadata("global", "smartphone supply chain", demo_script_en)
    print("\n--- GLOBAL ---")
    print("Título:", m1["titulo"])
    print("Descrição:", m1["descricao"][:200], "…")
    print("Tags:", m1["tags"])

    m2 = generate_video_metadata(
        "brasil", "setup gamer barato", demo_script_pt,
        produtos=[{"nome": "Teclado Mecânico RGB", "link": "https://s.shopee.com.br/abc"}],
    )
    print("\n--- BRASIL ---")
    print("Título:", m2["titulo"])
    print("Descrição:", m2["descricao"][:200], "…")
    print("Tags:", m2["tags"])

    m3 = generate_video_metadata(
        "shopee", "Suporte Monitor Articulado", demo_script_sp,
        extras={"preco": 45.90, "preco_original": 89.90, "desconto": 49, "rating": 4.8,
                "vendidos": 12000, "link_afiliado": "https://s.shopee.com.br/xyz"},
    )
    print("\n--- SHOPEE ---")
    print("Título:", m3["titulo"])
    print("Descrição:", m3["descricao"][:250], "…")
    print("Tags:", m3["tags"])

    t1 = generate_thumbnail("global", m1["titulo"], tema_ou_produto="smartphone supply chain")
    t2 = generate_thumbnail("brasil", m2["titulo"], tema_ou_produto="setup gamer barato")
    t3 = generate_thumbnail("shopee", m3["titulo"], preco_str="R$ 45,90",
                           tema_ou_produto="Suporte Monitor Articulado")
    j1 = save_metadata_file("output/video_global_demo.mp4", t1, "global", m1,
                            tema_ou_produto="smartphone supply chain")
    print("\nThumbs:", t1, t2, t3)
    print("JSON:", j1)
