"""
engine.py – Pipeline modular de geração de vídeos para YouTube e Shopee.
Três funções independentes: GLOBAL, BRASIL e SHOPEE.
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import edge_tts
import requests
from dotenv import load_dotenv
from google import genai
from google.api_core import exceptions as google_exceptions
from mutagen.mp3 import MP3

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
BROLL_DIR = BASE_DIR / "broll"
AUDIO_DIR = BASE_DIR / "audios"
OUTPUT_DIR = BASE_DIR / "output"

for _d in (ASSETS_DIR, BROLL_DIR, AUDIO_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
PEXELS_API_KEY: str = os.getenv("PEXELS_API_KEY", "")

if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY não configurada – geração de roteiro desabilitada.")
if not PEXELS_API_KEY:
    logger.warning("PEXELS_API_KEY não configurada – download de B‑roll desabilitada.")

# ---------------------------------------------------------------------------
# Gemini – inicialização do cliente
# ---------------------------------------------------------------------------

client: Optional[genai.Client] = None
# Modelos disponíveis na API v1beta do Google AI Studio:
# - gemini-3.5-flash: modelo mais recente da família Flash (recomendado)
# - gemini-flash-latest: alias que sempre aponta para a versão estável mais recente
# Se 404 occurir, troque para "gemini-flash-latest" como fallback
GEMINI_MODEL: str = "gemini-3.5-flash"

MAX_RETRIES: int = 3
RETRY_DELAY_SECONDS: int = 10

if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)
    logger.info("Cliente Gemini inicializado (modelo: %s)", GEMINI_MODEL)

# Vozes edge-tts
VOICES: Dict[str, str] = {
    "GLOBAL": "en-US-ChristopherNeural",
    "BRASIL": "pt-BR-AntonioNeural",
    "SHOPEE": "pt-BR-FranciscaNeural",
    "TIKTOK": "pt-BR-ThalitaMultilingualNeural",
}

AFILIADOS_PATH = BASE_DIR / "afiliados.json"

# ---------------------------------------------------------------------------
# Shopee – cliente de afiliados
# ---------------------------------------------------------------------------

_shopee_client = None  # lazy init

def _get_shopee_client():  # type: ignore[no-untyped-def]
    """Inicializa cliente Shopee sob demanda."""
    global _shopee_client
    if _shopee_client is None:
        from shopee_client import init_shopee_client
        _shopee_client = init_shopee_client()
    return _shopee_client


# ---------------------------------------------------------------------------
# Pillow – composição de slides 9:16 com overlay de preço/rating/link
# ---------------------------------------------------------------------------

try:
    from PIL import Image, ImageDraw, ImageFont
    _PILLOW_OK = True
except ImportError:  # pragma: no cover
    _PILLOW_OK = False
    logger.warning("Pillow não instalado – overlays desabilitados nos vídeos.")

VIDEO_W: int = 1080
VIDEO_H: int = 1920


def _load_font(size: int) -> Any:
    """Carrega uma fonte TTF do sistema com fallback para bitmap."""
    candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_text(draw: "ImageDraw.ImageDraw", text: str, font: Any, max_width: int) -> List[str]:
    """Quebra texto em linhas que caibam em max_width."""
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _format_brl(value: float) -> str:
    """Formata float como moeda brasileira (ex: 89.9 → 'R$ 89,90')."""
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def build_product_slide(
    image_path: str,
    name: str,
    price_str: str,
    original_price_str: str = "",
    discount_pct: float = 0.0,
    rating: float = 0.0,
    sold: int = 0,
    affiliate_link: str = "",
    index: int = 0,
    total: int = 1,
) -> str:
    """
    Compõe um slide vertical 1080x1920 com a foto real do produto e
    overlays de nome, preço, desconto, rating e link de afiliado.

    Args:
        image_path: Caminho local da imagem do produto.
        name: Nome do produto.
        price_str: Preço formatado (ex: 'R$ 89,90').
        original_price_str: Preço antes do desconto (opcional).
        discount_pct: Percentual de desconto exibido em badge.
        rating: Avaliação média (0-5).
        sold: Quantidade vendida.
        affiliate_link: Link curto de afiliado exibido no rodapé.
        index: Índice do slide (para numeração 1/N).
        total: Total de slides.

    Returns:
        Path do PNG gerado em assets/.
    """
    output_path = str(ASSETS_DIR / f"slide_{_unique('shopee')}.png")

    if not _PILLOW_OK:
        # Sem Pillow: usa a imagem diretamente (será tratada no ffmpeg)
        return image_path

    base = Image.open(image_path).convert("RGB")
    canvas = Image.new("RGB", (VIDEO_W, VIDEO_H), (18, 18, 24))

    # Fit da foto na metade superior mantendo proporção
    photo_h = int(VIDEO_H * 0.58)
    bw, bh = base.size
    scale = min(VIDEO_W / bw, photo_h / bh)
    new_w, new_h = int(bw * scale), int(bh * scale)
    resized = base.resize((new_w, new_h), Image.LANCZOS)
    canvas.paste(resized, ((VIDEO_W - new_w) // 2, 40))

    draw = ImageDraw.Draw(canvas)
    margin = 60
    y = photo_h + 80

    font_title = _load_font(56)
    font_price = _load_font(96)
    font_old = _load_font(44)
    font_badge = _load_font(48)
    font_meta = _load_font(44)
    font_link = _load_font(40)
    font_small = _load_font(32)

    # Nome do produto (até 2 linhas)
    title_lines = _wrap_text(draw, name, font_title, VIDEO_W - 2 * margin)[:2]
    for line in title_lines:
        draw.text((margin, y), line, fill=(245, 245, 245), font=font_title)
        y += 70
    y += 20

    # Badge de desconto + preço riscado
    if discount_pct and discount_pct > 0:
        badge = f" -{discount_pct:.0f}% "
        tw = draw.textlength(badge, font=font_badge)
        draw.rounded_rectangle(
            [margin, y + 14, margin + tw + 24, y + 14 + 76],
            radius=14, fill=(220, 38, 38),
        )
        draw.text((margin + 12, y + 18), badge, fill=(255, 255, 255), font=font_badge)
        if original_price_str:
            old_x = margin + tw + 48
            draw.text((old_x, y + 26), original_price_str, fill=(160, 160, 160), font=font_old)
            ow = draw.textlength(original_price_str, font=font_old)
            draw.line(
                [(old_x, y + 52), (old_x + ow, y + 52)],
                fill=(160, 160, 160), width=4,
            )
        y += 110

    # Preço atual
    draw.text((margin, y), price_str, fill=(255, 183, 3), font=font_price)
    y += 130

    # Rating e vendidos
    if rating > 0:
        stars = "★" * int(round(rating)) + "☆" * (5 - int(round(rating)))
        meta = f"{stars} {rating:.1f}"
        if sold > 0:
            meta += f"   |   {sold} vendidos"
        draw.text((margin, y), meta, fill=(250, 204, 21), font=font_meta)
        y += 70

    # Link de afiliado no rodapé
    if affiliate_link:
        footer_y = VIDEO_H - 170
        draw.rounded_rectangle(
            [margin, footer_y, VIDEO_W - margin, footer_y + 110],
            radius=20, fill=(237, 20, 91),
        )
        draw.text((margin + 24, footer_y + 12), "Link na Shopee 👇", fill=(255, 255, 255), font=font_small)
        link_lines = _wrap_text(draw, affiliate_link, font_link, VIDEO_W - 2 * margin - 48)[:1]
        draw.text((margin + 24, footer_y + 52), link_lines[0], fill=(255, 255, 255), font=font_link)

    # Numeração do slide
    if total > 1:
        draw.text((VIDEO_W - margin - 100, 40), f"{index + 1}/{total}",
                  fill=(255, 255, 255), font=font_meta)

    canvas.save(output_path, "PNG")
    logger.info("Slide composto (%d/%d): %s", index + 1, total, output_path)
    return output_path


def build_clean_product_card(
    image_path: str,
    max_size: int = 1000,
    corner_radius: int = 48,
) -> str:
    """
    Recorta a foto do produto para um cartão quadrado centralizado com
    cantos arredondados e fundo transparente (para composição via overlay).

    Args:
        image_path: Caminho da imagem real do produto.
        max_size: Lado máximo do cartão em pixels.
        corner_radius: Raio dos cantos arredondados.

    Returns:
        Path do PNG (RGBA) gerado em assets/.
    """
    output_path = str(ASSETS_DIR / f"card_{_unique('shopee')}.png")

    base = Image.open(image_path).convert("RGB")
    bw, bh = base.size
    side = min(bw, bh)
    left = (bw - side) // 2
    top = (bh - side) // 2
    cropped = base.crop((left, top, left + side, top + side))
    if side > max_size:
        cropped = cropped.resize((max_size, max_size), Image.LANCZOS)

    w, h = cropped.size
    mask = Image.new("L", (w, h), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([0, 0, w, h], radius=corner_radius, fill=255)

    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    card.paste(cropped, (0, 0), mask)
    card.save(output_path, "PNG")
    logger.info("Cartão de produto gerado: %s (%dx%d)", output_path, w, h)
    return output_path


# ---------------------------------------------------------------------------
# FFmpeg – renderização cinematográfica multi-camada (Shopee 9:16)
# ---------------------------------------------------------------------------


def _escape_filter_text(text: str) -> str:
    """Escapa texto para uso dentro do filtro drawtext do FFmpeg."""
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019")


def _dt_expr(expr: str) -> str:
    """
    Formata uma expressão de drawtext (enable/alpha/y) como valor
    single-quoted, escapando apóstrofos internos.
    """
    return "'" + expr.replace("'", "\u2019") + "'"


# Termos genéricos de vídeo (Pexels) mapeados por categoria de produto
_BACKGROUND_QUERY_MAP: List[tuple] = [
    (("teclado", "mouse", "gamer", "monitor", "pc", "notebook", "setup"),
     ["desk setup", "hands typing keyboard", "rgb gaming setup"]),
    (("fone", "headset", "caixa de som", "speaker", "earphone", "audio"),
     ["person listening music", "headphones desk", "audio studio"]),
    (("celular", "smartphone", "capinha", "carregador", "tablet"),
     ["using smartphone hands", "phone charging", "tech flatlay"]),
    (("camera", "ring light", "tripé", "tripod", "microfone"),
     ["content creator filming", "studio lighting setup", "podcast microphone"]),
    (("cadeira", "mesa", "suporte", "organiza", "estojo", "armário"),
     ["desk organization", "home office interior", "clean workspace"]),
    (("roupa", "camisa", "calça", "vestido", "tênis", "sapato", "bolsa"),
     ["fashion street style", "clothing rack boutique", "trying on shoes"]),
    (("cozinha", "panela", "liquidificador", "cafeteira", "air fryer", "utensílio"),
     ["cooking kitchen hands", "coffee making", "kitchen gadgets"]),
    (("maquiagem", "skincare", "perfume", "cabelo", "escova"),
     ["makeup application", "skincare routine", "beauty flatlay"]),
    (("academia", "fitness", "halter", "elástico", "yoga", "suplemento"),
     ["gym workout hands", "fitness equipment", "home exercise"]),
    (("relógio", "relogio", "pulseira", "smartwatch"),
     ["wristwatch closeup", "smartwatch hands", "luxury watch desk"]),
    (("brinquedo", "infantil", "bebê", "bebe"),
     ["kids playing toys", "baby room decor", "colorful toys"]),
    (("limpeza", "vassoura", "mop", "organizad", "casa"),
     ["cleaning home", "tidying room", "spray bottle cleaning"]),
]


def _build_background_queries(nome: str, descricao: str = "") -> List[str]:
    """
    Deriva 2-3 queries em inglês para clipes genéricos de fundo no Pexels,
    com base nas palavras-chave da categoria do produto.

    Args:
        nome: Nome do produto.
        descricao: Descrição do produto (ajuda no match de categoria).

    Returns:
        Lista de queries para download_brolls().
    """
    text = f"{nome} {descricao}".lower()
    for keywords, queries in _BACKGROUND_QUERY_MAP:
        if any(kw in text for kw in keywords):
            logger.info("Categoria detectada → queries de fundo: %s", queries)
            return queries
    generic = [f"{nome.split()[0]} product", "unboxing gift box", "shopping bags"]
    logger.info("Sem categoria específica – usando queries genéricas.")
    return generic


def _get_duration_float(media_path: str) -> float:
    """Duração em segundos de um arquivo de mídia (fallback: 10s)."""
    raw = _get_duration(media_path)
    try:
        return float(raw)
    except ValueError:
        return 10.0


def _ffmpeg_font_arg() -> str:
    """Fonte TTF para drawtext (com fallback interno do FFmpeg)."""
    for path in (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf"):
        if Path(path).exists():
            return path.replace("\\", "/")
    return "Sans Serif:bold"


def render_cinematic_shopee(
    card_paths: List[str],
    clips: List[str],
    audio_path: str,
    output_path: str,
    product_name: str,
    price_str: str,
    discount_pct: float = 0.0,
    rating: float = 0.0,
    duration: Optional[float] = None,
) -> str:
    """
    Renderiza vídeo vertical 9:16 cinematográfico com 3 camadas:

      Camada 1 (fundo): clipes do Pexels em loop com blur + opacidade ~40%.
      Camada 2 (produto): fotos reais em cartões com Ken Burns (zoom+pan)
                          e transições fade/slide (xfade) entre elas.
      Camada 3 (texto):  legendas animadas via drawtext —
                          gancho "ACHADINHO DA SHOPEE" (0-3s, amarelo),
                          nome do produto ao centro (meio do vídeo),
                          preço/desconto em verde + "Link na bio" (final),
                          fade out global nos últimos 0.5s.

    Args:
        card_paths: Cartões RGBA das imagens reais do produto.
        clips: Clipes de fundo do Pexels (portrait). Lista vazia → cor sólida.
        audio_path: Narração MP3.
        output_path: Destino do MP4.
        product_name: Nome do produto (legenda central).
        price_str: Preço formatado (ex: 'R$ 89,90').
        discount_pct: Percentual de desconto exibido no final.
        rating: Avaliação média exibida no final.
        duration: Duração alvo (default: duração do áudio).

    Returns:
        Path do MP4 renderizado.
    """
    dur = duration if duration else _get_duration_float(audio_path)
    fps = 30
    font = _ffmpeg_font_arg()

    # ---------------- timeline dos segmentos de produto ----------------
    n_cards = max(len(card_paths), 1)
    hook_end = min(3.0, dur * 0.12)
    cta_start = dur - max(dur * 0.18, 4.0)
    prod_window = max(cta_start - hook_end, 2.0)
    transition = 0.5
    seg_len = prod_window / n_cards + transition  # overlap coberto pelo xfade

    def _fmt(v: float) -> str:
        return f"{v:.2f}"

    filter_parts: List[str] = []
    inputs: List[str] = [audio_path]  # índice 0 = narração

    # ---------- Camada 1: fundo (clipes em loop, blur, opacidade ~40%) ----------
    bg_clips = clips[:4] if clips else []
    for clip in bg_clips:
        inputs += ["-stream_loop", "-1", "-i", clip]
    next_idx = 1 + len(bg_clips)  # próximo índice livre de entrada

    if bg_clips:
        for i in range(len(bg_clips)):
            filter_parts.append(
                f"[{i + 1}:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
                f"crop={VIDEO_W}:{VIDEO_H},setsar=1,fps={fps},"
                f"gblur=sigma=22,eq=brightness=-0.06[b{i}]"
            )
        if len(bg_clips) == 1:
            filter_parts.append("[b0]colorchannelmixer=aa=0.4[bgfin]")
        else:
            chain = "[b0]"
            acc = 0.0
            span = dur / len(bg_clips)
            for j in range(1, len(bg_clips)):
                offset = _fmt(acc + span - 0.8)
                out = "bgfin" if j == len(bg_clips) - 1 else f"bgm{j}"
                filter_parts.append(
                    f"{chain}[b{j}]xfade=transition=fade:duration=0.8:offset={offset}[{out}]"
                )
                chain = f"[{out}]"
                acc += span - 0.8
            filter_parts.append(f"{chain}colorchannelmixer=aa=0.4[bgfin]")
        filter_parts.append(
            f"color=c=0x0d0d14:s={VIDEO_W}x{VIDEO_H}:r={fps}:d={_fmt(dur)}[base0]"
        )
        filter_parts.append("[base0][bgfin]overlay=0:0[canvas]")
    else:
        filter_parts.append(
            f"color=c=0x0d0d14:s={VIDEO_W}x{VIDEO_H}:r={fps}:d={_fmt(dur)}[canvas]"
        )

    # ---------- Camada 2: cartões do produto com Ken Burns + xfade ----------
    for card in card_paths:
        inputs += ["-loop", "1", "-t", _fmt(seg_len), "-i", card]

    if card_paths:
        zoom_frames = int(seg_len * fps)
        kb_labels: List[str] = []
        for i in range(len(card_paths)):
            src_idx = next_idx + i
            pan_y = (
                "ih/2-(ih/zoom/2)" if i % 2 == 0
                else "ih/2-(ih/zoom/2)+(on/15)"
            )
            filter_parts.append(
                f"[{src_idx}:v]format=rgba,scale=920:920,"
                f"zoompan=z='min(1+0.066*on/{fps},1.2)':x='iw/2-(iw/zoom/2)':"
                f"y='{pan_y}':d={zoom_frames}:s=920x920:fps={fps},"
                f"fade=t=in:st=0:d={transition}:alpha=1,"
                f"fade=t=out:st={_fmt(max(seg_len - transition, 0))}:d={transition}:alpha=1[kc{i}]"
            )
            kb_labels.append(f"[kc{i}]")

        if len(kb_labels) == 1:
            overlay_src = "kc0"
        else:
            chain = kb_labels[0]
            acc = seg_len
            for j, lbl in enumerate(kb_labels[1:], start=1):
                kind = "slideleft" if j % 2 == 1 else "slideright"
                if j % 3 == 0:
                    kind = "fade"
                offset = _fmt(acc - transition)
                out = "prod" if j == len(kb_labels) - 1 else f"px{j}"
                filter_parts.append(
                    f"{chain}{lbl}xfade=transition={kind}:duration={transition}:offset={offset}[{out}]"
                )
                chain = f"[{out}]"
                acc += seg_len - transition
            overlay_src = "prod"

        y_center = int(VIDEO_H * 0.5 - 460)
        filter_parts.append(
            f"[canvas][{overlay_src}]overlay=x=(W-w)/2:y={y_center}:"
            f"enable={_dt_expr(_escape_filter_text(f'between(t,{_fmt(hook_end)},{_fmt(min(cta_start + 1.0, dur))})'))}[cv2]"
        )
        current = "[cv2]"
    else:
        current = "[canvas]"

    # ---------- Camada 3: textos animados (drawtext) ----------
    texts: List[tuple] = []

    # 1) Gancho "ACHADINHO DA SHOPEE" – 0-3s, amarelo sobre caixa semi-transparente
    texts.append(dict(
        text="ACHADINHO DA SHOPEE", size=88, color="0xFFE600",
        y="(h-text_h)*0.18", box=1, boxcolor="black@0.55", borderw=0,
        enable=f"lt(t,{_fmt(hook_end)})",
        alpha="if(lt(t,0.4),t/0.4,if(gt(t,2.5),(3-t)/0.5,1))",
        center=True,
    ))
    # 2) Nome do produto – meio do vídeo, branco com sombra preta
    name_short = product_name[:42] + ("…" if len(product_name) > 42 else "")
    texts.append(dict(
        text=name_short, size=58, color="white",
        y="h*0.86", box=0, shadow=1, borderw=0,
        enable=f"between(t,{_fmt(hook_end)},{_fmt(cta_start)})",
        alpha=f"if(lt(t-{_fmt(hook_end)},0.5),(t-{_fmt(hook_end)})/0.5,1)",
        center=True,
    ))
    # 3) Final: preço verde + rating + CTA
    final_line1 = f"{price_str} ({discount_pct:.0f}% OFF)" if discount_pct > 0 else price_str
    if final_line1:
        texts.append(dict(
            text=final_line1, size=92, color="0x00FF00",
            y="h*0.80", box=1, boxcolor="black@0.55", borderw=0,
            enable=f"gte(t,{_fmt(cta_start)})",
            alpha=f"if(lt(t-{_fmt(cta_start)},0.5),(t-{_fmt(cta_start)})/0.5,1)",
            center=True,
        ))
    if rating > 0:
        texts.append(dict(
            text=f"{rating:.1f}/5", size=60, color="0xFFC83D",
            y="h*0.80+110", box=1, boxcolor="black@0.45", borderw=0,
            enable=f"gte(t,{_fmt(cta_start)})",
            alpha=f"if(lt(t-{_fmt(cta_start)},0.5),(t-{_fmt(cta_start)})/0.5,1)",
            center=True,
        ))
    texts.append(dict(
        text="Link na bio", size=72, color="white",
        y="h*0.80+210", box=1, boxcolor="0xED145B", borderw=0,
        enable=f"gte(t,{_fmt(cta_start)})",
        alpha=f"if(lt(t-{_fmt(cta_start)},0.5),(t-{_fmt(cta_start)})/0.5,1)",
        center=True,
    ))

    prev = current
    for k, t in enumerate(texts):
        out = "txtfin" if k == len(texts) - 1 else f"txt{k}"
        x_expr = "(w-text_w)/2" if t.get("center") else "60"

        def _q(expr: str) -> str:
            """Envolve expressão em aspas simples para o parser de filtros."""
            return "'" + expr + "'"

        dt = [
            f"fontfile={_escape_filter_text(_escape_filter_text(font))}",
            f"text='{_escape_filter_text(t['text'])}'",
            f"fontsize={t['size']}",
            f"fontcolor={t['color']}",
            f"x={x_expr}",
            f"y={_q(_escape_filter_text(t['y']))}",
            f"enable={_q(_escape_filter_text(t['enable']))}",
        ]
        if t.get("box"):
            dt.append(f"box=1:boxcolor={t['boxcolor']}:boxborderw=28")
        if t.get("shadow"):
            dt.append("shadowcolor=black@0.75:shadowx=4:shadowy=4")
        if t.get("alpha"):
            dt.append(f"alpha={_q(_escape_filter_text(t['alpha']))}")
        filter_parts.append(f"{prev}drawtext=" + ":".join(dt) + f"[{out}]")
        prev = f"[{out}]"

    # 4) Fade out suave final (0.5s)
    filter_parts.append(
        f"{prev}fade=t=out:st={_fmt(max(dur - 0.5, 0))}:d=0.5,format=yuv420p[vout]"
    )

    args = [
        "ffmpeg", "-y",
        "-i", inputs[0],
    ]
    # demais entradas já trazem seus próprios flags (-stream_loop / -loop) + -i
    args += inputs[1:]
    args += [
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-c:a", "aac", "-b:a", "128k",
        "-t", _fmt(dur),
        "-movflags", "+faststart",
        output_path,
    ]
    logger.info(
        "Renderizando vídeo cinematográfico (%.1fs, %d cards, %d clipes)…",
        dur, len(card_paths), len(bg_clips),
    )
    _run_ffmpeg(args, "cinematic-shopee")
    return output_path


# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------


def _timestamp() -> str:
    """Gera timestamp para nomes de arquivo."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


import re as _re
import unicodedata as _unicodedata

# Palavras vazias (EN + PT) removidas na normalização de nomes de arquivo
_STOPWORDS: frozenset = frozenset({
    # inglês
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "with",
    "from", "by", "is", "are", "was", "were", "be", "been", "this", "that",
    "these", "those", "it", "its", "as", "but", "not", "no", "so", "if", "than",
    # português
    "o", "os", "as", "um", "uma", "uns", "umas", "de", "da", "do", "dos", "das",
    "em", "na", "no", "nas", "nos", "e", "ou", "que", "para", "por", "com",
    "sem", "ao", "aos", "se", "como", "mais", "muito", "isso", "este", "esta",
})


def normalize_filename(text: str, max_length: int = 30) -> str:
    """
    Converte um tema/nome de produto em slug descritivo para nome de arquivo.

    Passos: minúsculas → remove acentos (NFKD) → remove símbolos →
    remove stopwords (EN/PT) → junta com '_' → trunca mantendo palavras
    completas.

    Args:
        text: Texto livre (ex: 'The Microchip Supply Bottleneck').
        max_length: Limite de caracteres do resultado (padrão 30).

    Returns:
        Slug seguro para filesystem (ex: 'microchip_supply_bottleneck').

    Examples:
        >>> normalize_filename("The Microchip Supply Bottleneck")
        'microchip_supply_bottleneck'
        >>> normalize_filename("Suporte Articulado para Monitor")
        'suporte_articulado_monitor'
    """
    if not text or not text.strip():
        return "video"

    # minúsculas + remove acentos
    slug = text.lower()
    slug = _unicodedata.normalize("NFKD", slug)
    slug = "".join(c for c in slug if not _unicodedata.combining(c))

    # mantém apenas alfanuméricos e espaços
    slug = _re.sub(r"[^a-z0-9]+", " ", slug).strip()

    # remove stopwords e ordena preservando a original
    words = [w for w in slug.split() if w not in _STOPWORDS]
    if not words:
        words = slug.split() or ["video"]

    result = "_".join(words)

    # trunca mantendo palavras completas
    if len(result) > max_length:
        truncated = ""
        for w in words:
            candidate = f"{truncated}_{w}" if truncated else w
            if len(candidate) > max_length:
                break
            truncated = candidate
        result = truncated or words[0][:max_length]

    logger.info("Tema normalizado: %s", result)
    return result


def load_afiliados() -> dict:
    """Carrega o arquivo de afiliados."""
    if AFILIADOS_PATH.exists():
        with open(AFILIADOS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


# ---------------------------------------------------------------------------
# Gemini – chamada com retry, rate-limit e respeito ao RetryInfo
# ---------------------------------------------------------------------------

GEMINI_MIN_INTERVAL_SECONDS: float = 3.5   # espaçamento mínimo entre chamadas
_gemini_last_call_ts: float = 0.0
_gemini_rate_lock = threading.Lock()


class GeminiQuotaError(RuntimeError):
    """Cota diária/free-tier esgotada — não adianta insistir agora."""

    def __init__(self, message: str, retry_in_seconds: float) -> None:
        super().__init__(message)
        self.retry_in_seconds = retry_in_seconds


def _parse_retry_delay(exc: Exception) -> Optional[float]:
    """Extrai retryDelay (ex: '43s', '1.5s') do corpo do erro 429."""
    match = _re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', str(exc))
    if not match:
        match = _re.search(r"retry in ([\d.]+)s", str(exc), flags=_re.I)
    return float(match.group(1)) if match else None


def _gemini_generate(prompt: str) -> str:
    """
    Envia prompt ao Gemini com throttle global, retry exponencial e
    tratamento específico de cota (RetryInfo / free-tier diário).

    Returns:
        Texto da resposta.

    Raises:
        GeminiQuotaError: cota estourada (com retry_in_seconds para agendar).
        RuntimeError: falha definitiva (modelo/credencial/esgotado).
    """
    global _gemini_last_call_ts

    if client is None:
        raise RuntimeError(
            "Cliente Gemini não inicializado. Verifique GEMINI_API_KEY no .env"
        )

    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        # Throttle global: nunca mais que ~1 chamada a cada MIN_INTERVAL
        with _gemini_rate_lock:
            wait = GEMINI_MIN_INTERVAL_SECONDS - (time.monotonic() - _gemini_last_call_ts)
            if wait > 0:
                time.sleep(wait)
            _gemini_last_call_ts = time.monotonic()

        try:
            logger.info("Gemini – tentativa %d/%d…", attempt, MAX_RETRIES)
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            return response.text or ""
        except google_exceptions.NotFound as exc:
            logger.error(
                "Modelo não encontrado (404). Modelo '%s' Erro: %s",
                GEMINI_MODEL, exc,
            )
            raise RuntimeError(
                f"Modelo '{GEMINI_MODEL}' não encontrado."
            ) from exc
        except google_exceptions.PermissionDenied as exc:
            logger.error("Permissão negada (403): %s", exc)
            raise RuntimeError("Permissão negada. Verifique a GEMINI_API_KEY.") from exc
        except (google_exceptions.ResourceExhausted, google_exceptions.TooManyRequests) as exc:
            delay = _parse_retry_delay(exc)
            text = str(exc)
            daily = "PerDay" in text or "quota exceeded" in text.lower() and "day" in text.lower()
            if daily or (delay and delay > 120):
                retry_in = delay or 3600
                logger.error(
                    "Gemini: cota FREE-TIER diária esgotada (~%ds). Não insistir.",
                    int(retry_in),
                )
                raise GeminiQuotaError(
                    "Cota diária gratuita do Gemini esgotada. Faça upgrade do plano "
                    "no AI Studio ou aguarde a virada da cota.",
                    retry_in_seconds=retry_in,
                ) from exc
            last_error = exc
            backoff = max(delay or RETRY_DELAY_SECONDS, RETRY_DELAY_SECONDS) * attempt
            logger.warning(
                "Gemini 429 (rate limit) tentativa %d/%d. Retry em %.0fs…",
                attempt, MAX_RETRIES, backoff,
            )
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
            continue
        except google_exceptions.ServiceUnavailable as exc:
            last_error = exc
            logger.warning(
                "Indisponível (503) tentativa %d/%d. Retry em %ds…",
                attempt, MAX_RETRIES, RETRY_DELAY_SECONDS,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
            continue
        except Exception as exc:
            # SDK novo (google.genai.errors.ClientError) também cai aqui
            status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            if status == 429 or "RESOURCE_EXHAUSTED" in str(exc):
                delay = _parse_retry_delay(exc)
                if delay and delay > 120:
                    raise GeminiQuotaError(
                        "Cota diária gratuita do Gemini esgotada. Faça upgrade "
                        "no AI Studio ou aguarde a virada da cota.",
                        retry_in_seconds=delay,
                    ) from exc
                last_error = exc
                backoff = max(delay or RETRY_DELAY_SECONDS, RETRY_DELAY_SECONDS) * attempt
                logger.warning(
                    "Gemini 429 tentativa %d/%d. Retry em %.0fs…",
                    attempt, MAX_RETRIES, backoff,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(backoff)
                continue
            logger.error("Erro inesperado no Gemini: %s", exc)
            raise

    raise RuntimeError(
        f"Gemini indisponível após {MAX_RETRIES} tentativas."
    ) from last_error


# ---------------------------------------------------------------------------
# Roteiros – prompts por formato (long-form documentário x short) e validação
# ---------------------------------------------------------------------------

WORDS_PER_MINUTE: int = 150  # estimativa edge-tts pt-BR/en-US


def validate_script_length(script: str, min_words: int, max_words: int) -> bool:
    """
    Valida a contagem de palavras de um roteiro.

    Args:
        script: Texto da narração.
        min_words: Mínimo aceitável (abaixo disso → regenerar).
        max_words: Máximo desejável (acima disso apenas warning).

    Returns:
        True se o roteiro atende ao mínimo.
    """
    words = len(script.split())
    if words < min_words:
        logger.error("Roteiro curto: %d palavras (mínimo %d)", words, min_words)
        return False
    if words > max_words:
        logger.warning("Roteiro longo: %d palavras (máximo %d)", words, max_words)
    return True


def _log_script_stats(script: str) -> None:
    """Loga contagem de palavras e duração estimada do roteiro."""
    words = len(script.split())
    est_sec = words / WORDS_PER_MINUTE * 60
    logger.info("Roteiro: %s palavras", f"{words:,}".replace(",", "."))
    logger.info(
        "Duração estimada: %d min %02ds (baseado em %d palavras/min)",
        int(est_sec // 60), int(est_sec % 60), WORDS_PER_MINUTE,
    )


def generate_scripts(tipo: str, tema: str, duracao: str = "long") -> str:
    """
    Gera um roteiro via Gemini no formato pedido.

    Args:
        tipo: 'global', 'brasil' ou 'shopee'.
        tema: Tema do vídeo ou nome do produto.
        duracao: 'long' (8-12 min documentário), 'short' (60-90s) ou 'reel'
                 (25-35s, usado pelo pipeline Shopee).

    Returns:
        Texto puro da narração.

    Raises:
        ValueError: tipo/duração inválidos ou roteiro abaixo do mínimo mesmo
                    após tentativa de expansão.
        RuntimeError: falha na API Gemini.
    """
    tipo = tipo.strip().lower()
    duracao = duracao.strip().lower()

    if tipo == "global" and duracao == "long":
        prompt = f"""
Write a documentary narration script IN ENGLISH of 8-12 MINUTES of spoken
narration (1,300-1,800 words — ABSOLUTE MINIMUM 1,300 words). Topic: {tema}

MANDATORY structure (follow the time blocks):
- [0:00-0:30] Impact hook: a shocking statistic or provocative question
- [0:30-2:00] Historical context: origin of the problem, antecedents, first signs
- [2:00-4:30] Development: main players, conflicts, concrete data, chronology
- [4:30-7:00] Dramatic turning point: scandal, bankruptcy, discovery, point of no return
- [7:00-9:30] Consequences: market impact, people affected, lessons learned
- [9:30-11:00] Current situation: where we are today, recent developments
- [11:00-12:00] Provocative conclusion: future, trends, open question for comments

Style rules:
- Netflix/Vox documentary tone: investigative, cinematic, restrained suspense
- Use specific data: dates, dollar amounts, company and people names
- Cite sources naturally ("according to…", "a 2024 report by…")
- Smooth transitions between blocks; never announce the sections
- No stage directions, no headings — ONLY the spoken narration text
        """.strip()
        min_w, max_w = 1300, 1800

    elif tipo == "global" and duracao == "short":
        prompt = f"""
You are a professional documentary scriptwriter. Write a compelling, analytical
narration script in English for a 60-90 second video (150-200 words) about:
{tema}.

Requirements:
- Hook in the first sentence, punchy sentences, one rhetorical question
- Build tension fast and end with a thought-provoking conclusion
- No stage directions — ONLY the spoken narration text
        """.strip()
        min_w, max_w = 150, 260

    elif tipo == "brasil" and duracao == "long":
        prompt = f"""
Escreva um documentário EM PORTUGUÊS de 8-10 MINUTOS de narração
(1.200-1.500 palavras — MÍNIMO ABSOLUTO 1.200 palavras) sobre: {tema}
(tecnologia e produtividade).

Estrutura obrigatória (siga os blocos de tempo):
- [0:00-0:45] Problema comum que todo desenvolvedor/gamer enfrenta
- [0:45-2:30] Análise do problema: por que acontece, estatísticas, exemplos reais
- [2:30-5:00] Soluções práticas: 3-4 produtos/acessórios específicos com preços em reais
- [5:00-7:00] Comparativo: opções baratas vs caras, prós e contras de cada
- [7:00-8:30] Minha recomendação pessoal: qual escolher e por quê
- [8:30-10:00] Call-to-action: links na descrição, cupons, grupo do Telegram

Estilo:
- Tom de amigo que entende do assunto, técnico mas acessível
- Cite produtos reais (teclado, mouse, suporte, monitor) com preços entre R$ 50 e R$ 500
- Sem marcações de seção ou direção — APENAS o texto falado da narração
        """.strip()
        min_w, max_w = 1200, 1500

    elif tipo == "brasil" and duracao == "short":
        prompt = f"""
Você é um roteirista técnico do YouTube Brasil. Escreva uma narração envolvente
de 60-90 segundos (150-200 palavras) sobre: {tema}.

Regras:
- Comece com o problema, mostre a solução, termine citando que "os links estão
  na descrição" de forma orgânica
- Tom técnico-educativo, frases curtas
- APENAS o texto da narração, sem direções de cena
        """.strip()
        min_w, max_w = 150, 260

    elif tipo == "tiktok" or duracao == "micro":
        prompt = f"""
Roteiro TIKTOK de 25-40 SEGUNDOS (80-110 palavras) sobre: {tema}.

Estrutura:
- [0:00-0:03] Gancho forte e direto (pergunta, fato chocante ou curiosidade)
- [0:03-0:15] Contexto rapido + dado concreto
- [0:15-0:30] Solucao ou insight principal
- [0:30-0:38] CTA curto ("segue pra mais", "comenta ai")

Tom: dinamico, informal, frases curtas. MAXIMO 110 palavras.
Escreva APENAS o texto da narracao, sem direcoes de cena.
        """.strip()
        min_w, max_w = 70, 120

    elif duracao == "reel" or tipo == "shopee":
        prompt = f"""
Copy de 25-35 SEGUNDOS (70-90 palavras) para Shopee Video sobre: {tema}.

Estrutura:
- [0:00-0:03] Gancho: "Olha esse achadinho!"
- [0:03-0:15] Problema que resolve + demonstração
- [0:15-0:25] Preço, desconto, urgência
- [0:25-0:30] Call-to-action: "Clica no link"

Tom: animado, urgente, linguagem de TikTok. MÁXIMO 90 palavras.
Escreva APENAS o texto da narração.
        """.strip()
        min_w, max_w = 60, 110

    else:
        raise ValueError(f"Combinação inválida: tipo={tipo!r} duracao={duracao!r}")

    logger.info("Gemini – roteiro %s/%s ('%s')…", tipo, duracao, tema[:50])
    script = _gemini_generate(prompt).strip()
    _log_script_stats(script)

    if not validate_script_length(script, min_w, max_w):
        logger.info("Regenerando roteiro expandido (%s/%s)…", tipo, duracao)
        expand_prompt = (
            prompt
            + f"\n\nIMPORTANTE: sua última tentativa ficou aquém do mínimo. "
            + f"Entregue OBRIGATORIAMENTE entre {min_w} e {max_w} palavras, "
            + "desenvolvendo cada bloco com dados, exemplos e transições."
        )
        script = _gemini_generate(expand_prompt).strip()
        _log_script_stats(script)
        if not validate_script_length(script, min_w, max_w):
            raise ValueError(
                f"Roteiro {tipo}/{duracao} abaixo do mínimo ({min_w} palavras) "
                f"mesmo após expansão: {len(script.split())} palavras."
            )

    return script


# ---------------------------------------------------------------------------
# Duração de mídia (mutagen)
# ---------------------------------------------------------------------------


def get_media_duration(path: Union[str, Path]) -> float:
    """
    Duração exata de um arquivo de mídia em segundos.

    Usa mutagen para áudio (MP3/M4A) e ffprobe como fallback para vídeo.
    """
    path_str = str(path)
    try:
        return float(MP3(path_str).info.length)
    except Exception:
        pass
    try:
        from mutagen.mp4 import MP4

        dur = float(MP4(path_str).info.length)
        if dur > 0:
            return dur
    except Exception:
        pass
    try:
        dur = float(_get_duration(path_str))
        if dur > 0:
            return dur
    except Exception as exc:
        logger.warning("Falha ao medir duração de %s: %s", path_str, exc)
    logger.warning("Duração de %s desconhecida – assumindo 10s.", path_str)
    return 10.0


def _fmt_mmss(seconds: float) -> str:
    """Formata segundos como '9 min 54s'."""
    m, s = divmod(int(round(seconds)), 60)
    return f"{m} min {s:02d}s"


# ---------------------------------------------------------------------------
# edge-tts – síntese de voz
# ---------------------------------------------------------------------------


async def _synthesize(text: str, voice: str, output_path: str) -> str:
    """Gera áudio MP3 a partir de texto."""
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)
    logger.info("Áudio salvo: %s", output_path)
    return output_path


def synthesize_speech(text: str, voice: str, output_path: str) -> str:
    """Wrapper síncrono para síntese de voz."""
    return asyncio.run(_synthesize(text, voice, output_path))


# ---------------------------------------------------------------------------
# Pexels – download de B‑roll e imagens
# ---------------------------------------------------------------------------

PEXELS_MIN_CLIP_SECONDS: float = 8.0
PEXELS_TARGET_COVERAGE_RATIO: float = 1.35
PEXELS_MAX_SCENES: int = 40
_PEXELS_PAGE_SIZE: int = 15


def _pexels_headers() -> Dict[str, str]:
    return {"Authorization": PEXELS_API_KEY}


def _download_file(url: str, filepath: str, timeout: int = 180) -> None:
    """Baixa um arquivo binário (clipe/imagem) com streaming."""
    resp = requests.get(url, timeout=timeout, stream=True)
    resp.raise_for_status()
    with open(filepath, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def _search_pexels_videos(
    query: str, orientation: str = "landscape", per_page: int = _PEXELS_PAGE_SIZE
) -> List[Dict[str, Any]]:
    """Busca vídeos no Pexels e retorna a lista de resultados (máx. por página)."""
    try:
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers=_pexels_headers(),
            params={
                "query": query,
                "per_page": per_page,
                "orientation": orientation,
                "size": "medium",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("videos", [])
    except requests.RequestException as exc:
        logger.error("Erro ao buscar vídeos no Pexels ('%s'): %s", query, exc)
        return []


def _search_pexels_images(
    query: str, orientation: str = "landscape", per_page: int = _PEXELS_PAGE_SIZE
) -> List[Dict[str, Any]]:
    """Busca fotos no Pexels (endpoint /v1/search)."""
    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            headers=_pexels_headers(),
            params={"query": query, "per_page": per_page, "orientation": orientation},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("photos", [])
    except requests.RequestException as exc:
        logger.error("Erro ao buscar imagens no Pexels ('%s'): %s", query, exc)
        return []


def _pick_image_link(photo: Dict[str, Any]) -> Optional[str]:
    """Escolhe a maior variante JPEG de uma foto do Pexels."""
    src = photo.get("src") or {}
    width = photo.get("width") or 0
    height = photo.get("height") or 0
    if max(width, height) >= 1920:
        return src.get("original") or src.get("large2x") or src.get("large")
    return src.get("large2x") or src.get("large") or src.get("original")


def build_scene_plan(
    queries: List[str], target_seconds: float
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """
    Planeja quantos clipes de vídeo e fotos são necessários para cobrir o
    áudio sem congelar quadro único. Cada cena de vídeo dura ~10-14s; fotos
    servem de complemento (Ken Burns) quando o vídeo não cobre tudo.

    Returns:
        (plan_videos, plan_images): listas de pares (query, filename_base).
    """
    import random

    qs = [q for q in dict.fromkeys(queries) if q and q.strip()]
    if not qs:
        qs = ["cinematic b roll"]
    # cenas de ~10-14s, com limite p/ não explode o download em docs longos
    n_scenes = min(max(len(qs), int(target_seconds // 12) + 1), PEXELS_MAX_SCENES)
    rng = random.Random(1234)
    rotated = qs[1:] + qs[:1] if len(qs) > 1 else qs
    pool = qs + rotated + rng.sample(qs * 3, min(len(qs) * 3, 8))
    plan_videos = [(pool[i % len(pool)], f"v{i}") for i in range(n_scenes)]
    extra = max(int(n_scenes * 0.35), 3)
    img_pool = pool + ["nature landscape", "city aerial timelapse", "abstract lights"]
    plan_images = [
        (img_pool[(n_scenes + i) % len(img_pool)], f"i{i}") for i in range(extra)
    ]
    return plan_videos, plan_images


def fetch_media_pool(
    plan_videos: List[Tuple[str, str]],
    plan_images: List[Tuple[str, str]],
    orientation: str = "landscape",
    max_workers: int = 6,
) -> Tuple[List[str], List[str]]:
    """
    Busca e baixa em paralelo os candidatos planejados (vídeos + fotos).

    Vídeos: ignora clipes curtos demais (<PEXELS_MIN_CLIP_SECONDS) e evita
    duplicatas pelo id do Pexels. Fotos entram como pool complementar.

    Returns:
        (video_paths, image_paths) baixados com sucesso.
    """
    if not PEXELS_API_KEY:
        logger.warning("PEXELS_API_KEY não configurada – sem mídia do Pexels.")
        return [], []

    seen_ids: set = set()
    id_lock = threading.Lock()
    video_paths: List[str] = []
    image_paths: List[str] = []

    def grab_video(query: str, base: str) -> Optional[str]:
        for video in _search_pexels_videos(query, orientation):
            vid = video.get("id")
            if vid in seen_ids:
                continue
            api_dur = video.get("duration") or 0
            if 0 < api_dur < PEXELS_MIN_CLIP_SECONDS:
                continue
            link = _pick_mp4_link(video, orientation)
            if not link:
                continue
            with id_lock:
                if vid in seen_ids:
                    continue
                seen_ids.add(vid)
            filepath = str(BROLL_DIR / (base + "_" + _unique("broll") + ".mp4"))
            try:
                _download_file(link, filepath)
                real = get_media_duration(filepath)
                if 0 < api_dur <= 1 and real < PEXELS_MIN_CLIP_SECONDS:
                    logger.info("Clipe curto descartado (%s, %.1fs).", query, real)
                    os.remove(filepath)
                    return None
                logger.info("Clipe Pexels (%s, %ss): %s", query, api_dur or round(real), filepath)
                return filepath
            except requests.RequestException as exc:
                logger.warning("Falha ao baixar clipe '%s': %s", query, exc)
                return None
        return None

    def grab_image(query: str, base: str) -> Optional[str]:
        for photo in _search_pexels_images(query, orientation):
            link = _pick_image_link(photo)
            if not link:
                continue
            filepath = str(BROLL_DIR / (base + "_" + _unique("pximg") + ".jpg"))
            try:
                _download_file(link, filepath, timeout=60)
                logger.info("Foto Pexels (%s): %s", query, filepath)
                return filepath
            except requests.RequestException as exc:
                logger.warning("Falha ao baixar foto '%s': %s", query, exc)
                return None
        return None

    jobs = [("v", q, b) for q, b in plan_videos] + [("i", q, b) for q, b in plan_images]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(grab_video if kind == "v" else grab_image, query, base): kind
            for kind, query, base in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            kind = futures[fut]
            try:
                path = fut.result()
            except Exception as exc:
                logger.warning("Erro inesperado no pool Pexels: %s", exc)
                continue
            if not path:
                continue
            (video_paths if kind == "v" else image_paths).append(path)

    logger.info(
        "Pool Pexels: %d vídeos + %d fotos.", len(video_paths), len(image_paths)
    )
    return video_paths, image_paths


def ensure_timeline_coverage(
    clip_paths: List[str],
    image_paths: List[str],
    target_seconds: float,
    min_clip_seconds: float = PEXELS_MIN_CLIP_SECONDS,
) -> List[str]:
    """
    Monta a sequência final de cenas que cobre o áudio: soma das durações dos
    clipes deve atingir ~target*PEXELS_TARGET_COVERAGE_RATIO. Preenche buracos
    com fotos (viram clipes Ken Burns via render) ou repetindo clipes longos.

    Args:
        clip_paths: Clipes de vídeo disponíveis (ordem de preferência).
        image_paths: Fotos Pexels/Shopee complementares.
        target_seconds: Duração da narração.
        min_clip_seconds: Corte mínimo aceitável por clipe.

    Returns:
        Lista ordenada de paths (vídeos e/ou imagens) para o slideshow.
    """
    import random

    need = target_seconds * PEXELS_TARGET_COVERAGE_RATIO

    def _dur(p: str) -> float:
        d = get_media_duration(p)
        return d if d and d > 0 else 10.0

    usable = [c for c in clip_paths if _dur(c) >= min_clip_seconds]
    fallback_only = bool(clip_paths) and not usable
    chosen: List[str] = []
    covered = 0.0
    rng = random.Random(len(clip_paths) + len(image_paths) + int(target_seconds))

    # intercala ordem p/ evitar blocos repetidos da mesma query
    ordered = list(usable)
    rng.shuffle(ordered)

    for c in ordered:
        if covered >= need:
            break
        chosen.append(c)
        covered += min(_dur(c), max(min_clip_seconds, need - covered))

    if covered < need:
        for img in image_paths:
            if covered >= need:
                break
            chosen.append(img)
            covered += 6.0  # cada foto vira cena Ken Burns de ~6s

    if covered < need and fallback_only:
        chosen = list(clip_paths)
        while sum(_dur(c) for c in chosen) < need:
            chosen.append(rng.choice(clip_paths))

    if covered < need and usable:
        longest = max(usable, key=_dur)
        while sum(_dur(c) for c in chosen) < need:
            chosen.append(longest)

    if not chosen and clip_paths:
        chosen = [clip_paths[0]]
    return chosen


def download_broll(query: str, orientation: str = "portrait") -> Optional[str]:
    """Baixa um clipe de vídeo do Pexels."""
    if not PEXELS_API_KEY:
        logger.warning("PEXELS_API_KEY não configurada – ignorando B‑roll.")
        return None

    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 5, "orientation": orientation}
    url = "https://api.pexels.com/videos/search"

    logger.info("Pexels – buscando B‑roll: '%s' (%s)", query, orientation)
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("Erro ao buscar vídeos no Pexels: %s", exc)
        return None

    videos = data.get("videos", [])
    if not videos:
        logger.warning("Nenhum vídeo encontrado para: %s", query)
        return None

    for video in videos:
        for file in video.get("video_files", []):
            if file.get("file_type") == "video/mp4" and file.get("height", 0) >= 1080:
                video_url = file["link"]
                filename = _unique("broll") + ".mp4"
                filepath = str(BROLL_DIR / filename)
                logger.info("Baixando clipe: %s", video_url)
                vresp = requests.get(video_url, timeout=120, stream=True)
                vresp.raise_for_status()
                with open(filepath, "wb") as f:
                    for chunk in vresp.iter_content(chunk_size=8192):
                        f.write(chunk)
                logger.info("B‑roll salvo: %s", filepath)
                return filepath

    logger.warning("Nenhum MP4 HD encontrado para: %s", query)
    return None


def _pick_mp4_link(video: Dict[str, Any], orientation: str = "portrait") -> Optional[str]:
    """Escolhe o melhor link MP4 de um vídeo do Pexels para a orientação dada."""
    files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"]
    if not files:
        return None

    def score(f: Dict[str, Any]) -> float:
        w, h = f.get("width") or 0, f.get("height") or 0
        if orientation == "portrait":
            ratio = max(w, h) / max(min(w, h), 1)
            vertical = 1.5 if h > w else 0.0
            return vertical * 1000 + min(ratio, 2.0) * 100 + min(h, 1920)
        return min(h, 1080)

    best = max(files, key=score)
    return best.get("link")


def download_brolls(queries: List[str], count: int = 3, orientation: str = "portrait") -> List[str]:
    """
    Baixa múltiplos clipes genéricos do Pexels (um por query distinta).

    Args:
        queries: Lista de termos de busca (ex: ['desk setup', 'hands typing']).
        count: Quantidade máxima de clipes a baixar.
        orientation: 'portrait' (9:16) ou 'landscape'.

    Returns:
        Lista de paths locais dos clipes baixados.
    """
    if not PEXELS_API_KEY:
        logger.warning("PEXELS_API_KEY não configurada – sem clipes de fundo.")
        return []

    clips: List[str] = []
    seen_ids: set = set()

    for query in queries:
        if len(clips) >= count:
            break
        logger.info("Pexels – buscando clipe de fundo: '%s'", query)
        got = False
        for video in _search_pexels_videos(query, orientation):
            vid = video.get("id")
            if vid in seen_ids:
                continue
            link = _pick_mp4_link(video, orientation)
            if not link:
                continue
            filepath = str(BROLL_DIR / (_unique("broll") + ".mp4"))
            try:
                _download_file(link, filepath)
                seen_ids.add(vid)
                clips.append(filepath)
                logger.info("Clipe de fundo salvo (%s): %s", query, filepath)
                got = True
                break
            except requests.RequestException as exc:
                logger.warning("Falha ao baixar clipe '%s': %s", query, exc)
        if not got:
            logger.warning("Nenhum clipe útil para '%s'.", query)

    logger.info("%d clipes de fundo baixados.", len(clips))
    return clips


def download_image(url: str) -> Optional[str]:
    """Baixa uma imagem de URL ou retorna path local se já existir."""
    local_path = Path(url)
    if local_path.exists():
        return str(local_path)

    filename = _unique("img") + ".jpg"
    filepath = ASSETS_DIR / filename

    logger.info("Baixando imagem: %s", url)
    try:
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info("Imagem salva: %s", filepath)
        return str(filepath)
    except requests.RequestException as exc:
        logger.error("Falha ao baixar imagem %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# FFmpeg – utilitários
# ---------------------------------------------------------------------------


def _run_ffmpeg(args: list[str], desc: str) -> None:
    """Executa comando FFmpeg."""
    logger.info("FFmpeg (%s): %s", desc, " ".join(args[:6]))
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("FFmpeg falhou (%s): %s", desc, result.stderr[:500])
        raise RuntimeError(f"FFmpeg falhou: {result.stderr[:200]}")
    logger.info("FFmpeg (%s) concluído.", desc)


def _get_duration(audio_path: str) -> str:
    """Obtém duração do áudio via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "60"


def render_horizontal(
    audio_path: str, broll_path: Optional[str], output_path: str, label: str
) -> str:
    """
    Renderiza vídeo horizontal 16:9 com a duração exata do áudio.

    O clipe de fundo entra em loop (-stream_loop -1) até o fim da narração;
    sem -shortest (evita cortes prematuros). Fade in/out de 2s no áudio e
    fade out de 2s no vídeo para transição suave.
    """
    duration = get_media_duration(audio_path)
    d = f"{duration:.2f}"
    fade_out_start = max(duration - 2.0, 0.0)
    afade_out_start = max(duration - 2.0, 0.0)

    if broll_path and Path(broll_path).exists():
        args = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", broll_path,
            "-i", audio_path,
            "-filter_complex",
            f"[0:v]scale=1920:1080:force_original_aspect_ratio=increase,"
            f"crop=1920:1080,setsar=1,fade=t=out:st={afade_out_start:.2f}:d=2[v];"
            f"[1:a]afade=t=in:st=0:d=2,afade=t=out:st={afade_out_start:.2f}:d=2[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-t", d,
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        args = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=1920x1080:d={d}",
            "-i", audio_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-af", f"afade=t=in:st=0:d=2,afade=t=out:st={afade_out_start:.2f}:d=2",
            "-c:a", "aac", "-b:a", "128k",
            "-t", d,
            "-movflags", "+faststart",
            output_path,
        ]
    logger.info("Renderizando %s (%s)…", label, _fmt_mmss(duration))
    _run_ffmpeg(args, label)
    return output_path


def render_vertical(
    audio_path: str, broll_path: Optional[str], output_path: str
) -> str:
    """
    Renderiza vídeo vertical 9:16 com duração exata do áudio (loop do fundo,
    sem -shortest, fades de 2s).
    """
    duration = get_media_duration(audio_path)
    d = f"{duration:.2f}"
    fo = max(duration - 2.0, 0.0)

    if broll_path and Path(broll_path).exists():
        args = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", broll_path,
            "-i", audio_path,
            "-filter_complex",
            f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,setsar=1,fade=t=out:st={fo:.2f}:d=2[v];"
            f"[1:a]afade=t=in:st=0:d=2,afade=t=out:st={fo:.2f}:d=2[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-t", d,
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        args = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=1080x1920:d={d}",
            "-i", audio_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-af", f"afade=t=in:st=0:d=2,afade=t=out:st={fo:.2f}:d=2",
            "-c:a", "aac", "-b:a", "128k",
            "-t", d,
            "-movflags", "+faststart",
            output_path,
        ]
    _run_ffmpeg(args, "vertical")
    return output_path


def render_slideshow(
    image_paths: List[str],
    audio_path: str,
    output_path: str,
    width: int = 1080,
    height: int = 1920,
) -> str:
    """
    Renderiza slideshow com efeito Ken Burns (zoom + pan) a partir de imagens.
    Cada imagem é exibida por tempo igual, com transição suave.
    """
    if not image_paths:
        logger.warning("Nenhuma imagem para slideshow – usando fundo preto.")
        duration = _get_duration(audio_path)
        args = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d={duration}",
            "-i", audio_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", output_path,
        ]
        _run_ffmpeg(args, "slideshow-fallback")
        return output_path

    duration = float(_get_duration(audio_path))
    per_image = duration / len(image_paths)

    # Monta filter_complex com Ken Burns (zoom in lento)
    inputs: list[str] = []
    filter_parts: list[str] = []
    for i, img in enumerate(image_paths):
        inputs.extend(["-loop", "1", "-t", f"{per_image:.2f}", "-i", img])
        filter_parts.append(
            f"[{i}:v]scale=8000:-1,zoompan=z='min(zoom+0.001,1.5)':"
            f"d={int(per_image * 25)}:x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':s={width}x{height}:fps=25,"
            f"format=yuv420p[v{i}]"
        )

    # 3. Concatena todos os streams de vídeo com crossfade de 0.5s (xfade)
    if len(image_paths) == 1:
        out_label = "v0"
    else:
        chain = "[v0]"
        acc = per_image
        for i in range(1, len(image_paths)):
            offset = f"{max(acc - 0.5, 0):.2f}"
            last = i == len(image_paths) - 1
            label = "outv" if last else f"x{i}"
            filter_parts.append(
                f"{chain}[v{i}]xfade=transition=fade:duration=0.5:offset={offset}[{label}]"
            )
            chain = f"[{label}]"
            acc += per_image - 0.5
        out_label = "outv"

    args = [
        "ffmpeg", "-y",
        *inputs,
        "-i", audio_path,
        "-filter_complex", ";".join(filter_parts),
        "-map", f"[{out_label}]", "-map", f"{len(image_paths)}:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", output_path,
    ]

    _run_ffmpeg(args, "slideshow-kenburns")
    return output_path


def render_slideshow_clips(
    clip_paths: List[str],
    audio_path: str,
    output_path: str,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    transition: float = 1.0,
    scene_seconds: float = 12.0,
) -> str:
    """
    Encadeia múltiplos clipes de vídeo (e fotos, com Ken Burns) com crossfade
    (xfade) até a duração exata da narração — usado em documentários para
    evitar loop único repetido / quadro congelado.

    Cada cena é normalizada para a mesma resolução/fps/SAR; o resultado tem
    fade in/out de 2s no áudio e fade out de 2s no vídeo.

    Args:
        clip_paths: Clipes locais (Pexels), vídeos ou imagens. Vazio → preto.
        audio_path: Narração que define a duração final.
        output_path: Destino MP4.
        width/height: Resolução do vídeo final.
        fps: Frames por segundo.
        transition: Duração do crossfade entre cenas, em segundos.
        scene_seconds: Duração mínima de cada cena (ajustada para cima se
            houver poucas cenas; fotos viram cenas Ken Burns com essa duração).

    Returns:
        Path do MP4 renderizado.
    """
    duration = get_media_duration(audio_path)
    d = f"{duration:.2f}"
    fo = max(duration - 2.0, 0.0)

    if not clip_paths:
        return _render_solid_bg(audio_path, output_path, width, height, duration)

    # cena-alvo dinâmica: cobre o áudio com as cenas disponíveis sem congelar
    n = len(clip_paths)
    scene_seconds = max(
        float(scene_seconds),
        duration * PEXELS_TARGET_COVERAGE_RATIO / n + transition,
    )

    _IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

    inputs: List[str] = []
    filter_parts: List[str] = []
    spans: List[float] = []
    for i, clip in enumerate(clip_paths):
        is_image = Path(clip).suffix.lower() in _IMG_EXTS
        if is_image:
            # foto → cena Ken Burns (zoom lento) com duração fixa
            span = max(scene_seconds, transition * 2 + 0.5)
            frames = int(span * fps)
            inputs += ["-loop", "1", "-t", f"{span:.2f}", "-i", clip]
            filter_parts.append(
                f"[{i}:v]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
                f"crop={width * 2}:{height * 2},setsar=1,"
                f"zoompan=z='min(zoom+0.0008,1.25)':x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)':d={frames}:s={width}x{height}:fps={fps},"
                f"format=yuv420p[n{i}]"
            )
        else:
            span = min(max(get_media_duration(clip), transition * 2 + 0.5),
                       max(scene_seconds, transition * 2 + 0.5))
            inputs += ["-ss", "0", "-t", f"{span:.2f}", "-i", clip]
            filter_parts.append(
                f"[{i}:v]fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,format=yuv420p[n{i}]"
            )
        spans.append(span)

    # Encadeia N cenas distintas com crossfade (sem loop único repetido)
    n = len(clip_paths)
    if n == 1:
        chain = "[n0]"
    else:
        chain = "[n0]"
        acc = 0.0
        for i in range(1, n):
            offset = max(acc + spans[i - 1] - transition, 0.0)
            last = i == n - 1
            label = "vx" if last else f"m{i}"
            filter_parts.append(
                f"{chain}[n{i}]xfade=transition=fade:duration={transition:.2f}:offset={offset:.2f}[{label}]"
            )
            chain = f"[{label}]"
            acc += spans[i - 1] - transition

    # Se a cadeia terminar antes do áudio, congela o último frame até 'd'
    filter_parts.append(
        f"{chain}tpad=stop_mode=clone:stop_duration={d},"
        f"fade=t=out:st={fo:.2f}:d=2,format=yuv420p[vout]"
    )
    args = [
        "ffmpeg", "-y",
        *inputs,
        "-i", audio_path,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]", "-map", f"{n}:a",
        "-af", f"afade=t=in:st=0:d=2,afade=t=out:st={fo:.2f}:d=2",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-t", d,
        "-movflags", "+faststart",
        output_path,
    ]
    logger.info("Renderizando slideshow de %d cenas (%s)…", n, _fmt_mmss(duration))
    _run_ffmpeg(args, "slideshow-clips")
    return output_path


def _render_solid_bg(
    audio_path: str, output_path: str, width: int, height: int, duration: float
) -> str:
    """Fallback: cor sólida pela duração exata do áudio."""
    d = f"{duration:.2f}"
    fo = max(duration - 2.0, 0.0)
    args = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d={d}",
        "-i", audio_path,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-af", f"afade=t=in:st=0:d=2,afade=t=out:st={fo:.2f}:d=2",
        "-c:a", "aac", "-b:a", "128k",
        "-t", d,
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(args, "solid-bg")
    return output_path


def _publish_metadata(
    tipo: str,
    video_path: str,
    tema_ou_produto: str,
    roteiro: str,
    produtos: Optional[List[Dict[str, str]]] = None,
    extras: Optional[Dict[str, Any]] = None,
    fundo_thumb: Optional[str] = None,
) -> Dict[str, str]:
    """
    Gera título/descrição/tags + thumbnail e salva o JSON de metadados.

    Args:
        tipo: 'global', 'brasil' ou 'shopee'.
        video_path: MP4 recém-renderizado.
        tema_ou_produto: Tema/nome do produto.
        roteiro: Texto da narração.
        produtos: Produtos para bloco de afiliados (BRASIL).
        extras: Dados reais do produto (SHOPEE).
        fundo_thumb: Imagem de fundo da thumbnail (foto do produto).

    Returns:
        Dict com paths: metadata_file, thumbnail_file, titulo.
    """
    from metadata_generator import (
        generate_thumbnail,
        generate_video_metadata,
        save_metadata_file,
    )

    try:
        meta = generate_video_metadata(tipo, tema_ou_produto, roteiro, produtos, extras)
        preco_str = ""
        if tipo == "shopee" and extras and float(extras.get("preco", 0) or 0) > 0:
            preco_str = _format_brl(float(extras["preco"]))
        thumb = generate_thumbnail(
            tipo, meta["titulo"], imagem_fundo=fundo_thumb,
            preco_str=preco_str, tema_ou_produto=tema_ou_produto,
        )
        json_path = save_metadata_file(video_path, thumb, tipo, meta, tema_ou_produto)
        logger.info("Arquivos finais → vídeo=%s | thumb=%s | metadata=%s",
                    video_path, thumb, json_path)
        return {"metadata_file": json_path, "thumbnail_file": thumb, "titulo": meta["titulo"]}
    except Exception as exc:
        logger.error("Falha ao gerar metadados/thumbnail (%s): %s", tipo, exc)
        return {}


# ===========================================================================
# FUNÇÃO 1 – VÍDEO GLOBAL (inglês, horizontal 16:9)
# ===========================================================================


def _prepare_documentary_scenes(
    query_pexels: str, audio_path: str, orientation: str = "landscape"
) -> List[str]:
    """Busca pool de vídeos + fotos no Pexels e monta a sequência de cenas."""
    base = [t.strip() for t in query_pexels.split(",") if t.strip()]
    queries = base + ["cinematic b roll", "technology abstract", "documentary aerial"]
    target = get_media_duration(audio_path)
    plan_videos, plan_images = build_scene_plan(queries, target)
    clips, photos = fetch_media_pool(plan_videos, plan_images, orientation)
    scenes = ensure_timeline_coverage(clips, photos, target)
    logger.info("Cenas preparadas: %d para %s.", len(scenes), _fmt_mmss(target))
    return scenes


def generate_global_video(tema: str, query_pexels: str, duracao: str = "long") -> str:
    """
    Gera vídeo em inglês no estilo documentário de tecnologia/finanças/geopolítica.

    Args:
        tema: Tema do vídeo em inglês.
        query_pexels: Query de busca para B‑roll no Pexels.
        duracao: 'long' (8-12 min, 1.300-1.800 palavras) ou 'short' (60-90s).

    Returns:
        Caminho do arquivo MP4 gerado.
    """
    ts = _timestamp()
    tema_slug = normalize_filename(tema)
    suffix = "" if duracao == "long" else f"_{duracao}"
    output_path = str(OUTPUT_DIR / f"video_global_{tema_slug}{suffix}_{ts}.mp4")
    logger.info("Arquivos salvos com prefixo: video_global_%s", tema_slug)

    logger.info("=== GERANDO VÍDEO GLOBAL %s (%s) ===", duracao.upper(), tema)

    # 1. Roteiro (com validação de contagem de palavras + expansão)
    logger.info("Etapa 1/5 – Gerando roteiro GLOBAL (%s)…", duracao)
    script = generate_scripts("global", tema, duracao=duracao)
    logger.info("Roteiro GLOBAL (%d chars): %s…", len(script), script[:120])

    # 2. Áudio
    logger.info("Etapa 2/5 – Sintetizando voz GLOBAL…")
    audio_path = str(AUDIO_DIR / f"global{suffix}_{ts}.mp3")
    synthesize_speech(script, VOICES["GLOBAL"], audio_path)
    logger.info("Duração real do áudio: %s", _fmt_mmss(get_media_duration(audio_path)))

    # 3. B‑roll (múltiplos clipes + fotos, sem loop único)
    logger.info("Etapa 3/5 – Buscando B‑roll…")
    scenes = _prepare_documentary_scenes(query_pexels, audio_path)

    # 4. Renderização horizontal
    logger.info("Etapa 4/4 – Renderizando vídeo GLOBAL…")
    render_slideshow_clips(scenes, audio_path, output_path, width=1920, height=1080)

    logger.info("=== VÍDEO GLOBAL CONCLUÍDO → %s ===", output_path)
    return output_path


def generate_long_video(tipo: str, tema: str, query_pexels: str) -> str:
    """
    Gera vídeo LONGO para YouTube (documentário de 8-12 minutos).

    Args:
        tipo: 'global' ou 'brasil'.
        tema: Tema do vídeo.
        query_pexels: Query de B‑roll no Pexels.

    Returns:
        Caminho do MP4 gerado.
    """
    tipo = tipo.strip().lower()
    if tipo == "global":
        return generate_global_video(tema, query_pexels or tema, duracao="long")
    if tipo == "brasil":
        afiliados = load_afiliados()
        produtos = [
            {"nome": v.get("nome", k), "link": v.get("link_shopee", "")}
            for k, v in afiliados.items()
            if isinstance(v, dict) and "nome" in v
        ]
        return generate_brasil_video(tema, query_pexels or tema, produtos, duracao="long")
    raise ValueError(f"tipo inválido para generate_long_video: {tipo!r} (use global|brasil)")


def generate_short_video(tipo: str, tema: str, query_pexels: str) -> str:
    """
    Gera vídeo CURTO (60-90 segundos) para Shorts/TikTok.

    Args:
        tipo: 'global' ou 'brasil'.
        tema: Tema do vídeo.
        query_pexels: Query de B‑roll no Pexels.

    Returns:
        Caminho do MP4 gerado.
    """
    tipo = tipo.strip().lower()
    if tipo == "global":
        return generate_global_video(tema, query_pexels or tema, duracao="short")
    if tipo == "brasil":
        afiliados = load_afiliados()
        produtos = [
            {"nome": v.get("nome", k), "link": v.get("link_shopee", "")}
            for k, v in afiliados.items()
            if isinstance(v, dict) and "nome" in v
        ]
        return generate_brasil_video(tema, query_pexels or tema, produtos, duracao="short")
    raise ValueError(f"tipo inválido para generate_short_video: {tipo!r} (use global|brasil)")


# ===========================================================================
# FUNÇÃO 2 – VÍDEO BRASIL (português técnico, horizontal 16:9)
# ===========================================================================


def generate_brasil_video(
    tema: str,
    query_pexels: str,
    produtos: Optional[List[Dict[str, str]]] = None,
    duracao: str = "long",
) -> str:
    """
    Gera vídeo em português com tom técnico-educativo, citando periféricos
    e produtos com links de afiliado.

    Args:
        tema: Tema do vídeo.
        query_pexels: Query de busca para B‑roll.
        produtos: Lista de dicts com 'nome' e 'link' para mencionar no CTA.
        duracao: 'long' (8-10 min, 1.200-1.500 palavras) ou 'short' (60-90s).

    Returns:
        Caminho do arquivo MP4 gerado.
    """
    ts = _timestamp()
    tema_slug = normalize_filename(tema)
    suffix = "" if duracao == "long" else f"_{duracao}"
    output_path = str(OUTPUT_DIR / f"video_brasil_{tema_slug}{suffix}_{ts}.mp4")
    logger.info("Arquivos salvos com prefixo: video_brasil_%s", tema_slug)

    logger.info("=== GERANDO VÍDEO BRASIL %s (%s) ===", duracao.upper(), tema)

    # Monta texto dos produtos para o prompt
    produtos_texto = ""
    if produtos:
        produtos_texto = "\nProdutos para mencionar no CTA:\n"
        for p in produtos:
            produtos_texto += f"- {p['nome']}: {p['link']}\n"

    # 1. Roteiro (validado por generate_scripts) + enriquecimento com produtos
    logger.info("Etapa 1/5 – Gerando roteiro BRASIL (%s)…", duracao)
    tema_completo = f"{tema}.{produtos_texto}" if produtos_texto else tema
    script = generate_scripts("brasil", tema_completo, duracao=duracao)
    logger.info("Roteiro BRASIL (%d chars): %s…", len(script), script[:120])

    # 2. Áudio
    logger.info("Etapa 2/5 – Sintetizando voz BRASIL…")
    audio_path = str(AUDIO_DIR / f"brasil{suffix}_{ts}.mp3")
    synthesize_speech(script, VOICES["BRASIL"], audio_path)
    logger.info("Duração real do áudio: %s", _fmt_mmss(get_media_duration(audio_path)))

    # 3. B‑roll (múltiplos clipes + fotos, sem loop único)
    logger.info("Etapa 3/5 – Buscando B‑roll…")
    scenes = _prepare_documentary_scenes(query_pexels, audio_path)

    # 4. Renderização horizontal
    logger.info("Etapa 4/5 – Renderizando vídeo BRASIL…")
    render_slideshow_clips(scenes, audio_path, output_path, width=1920, height=1080)

    # 5. Metadados + thumbnail
    logger.info("Etapa 5/5 – Gerando metadados e thumbnail BRASIL…")
    _publish_metadata("brasil", output_path, tema, script, produtos)

    logger.info("=== VÍDEO BRASIL CONCLUÍDO → %s ===", output_path)
    return output_path


# ===========================================================================
# FUNÇÃO 3 – VÍDEO SHOPEE (vertical 9:16, com imagens e Ken Burns)
# Aceita: dict manual, URL de produto, ou keyword de busca
# ===========================================================================


def generate_shopee_video(
    produto: Optional[Dict[str, Any]] = None,
    shopee_url: Optional[str] = None,
    shopee_keyword: Optional[str] = None,
) -> str:
    """
    Gera vídeo vertical 9:16 para Shopee Vídeo com dados REAIS do produto.

    Aceita três modos de operação (mutuamente exclusivos):
      1. produto (dict)       – dados manuais do afiliados.json
      2. shopee_url (str)     – URL de produto Shopee (detalhes via API real)
      3. shopee_keyword (str) – palavra-chave (busca real, pega o mais vendido)

    Nos modos 2 e 3: baixa as imagens reais via client.download_product_image(),
    gera copy de 20-30s citando preço/desconto/rating reais, cria o link curto
    de afiliado com subId e salva os metadados junto ao MP4.

    Args:
        produto: Dict com nome, descricao, imagens, link_shopee (modo manual).
        shopee_url: URL completa do produto na Shopee.
        shopee_keyword: Palavra-chave para busca na API Shopee.

    Returns:
        Caminho do arquivo MP4 gerado.

    Raises:
        ValueError: Se nenhum dos parâmetros for fornecido.
        RuntimeError: Se a API Shopee falhar ou nenhum produto for encontrado.
    """
    from shopee_client import (
        ShopeeAffiliateError,
        ShopeeProductNotFoundError,
        ShopeeTimeoutError,
    )

    if not produto and not shopee_url and not shopee_keyword:
        raise ValueError(
            "Forneça um dos: produto (dict), shopee_url ou shopee_keyword"
        )

    ts = _timestamp()
    nome = "Produto"
    descricao = ""
    imagens_raw: List[str] = []
    link = ""
    price = 0.0
    original_price_str = ""
    discount = 0.0
    rating = 0.0
    sold = 0
    product_obj = None  # ShopeeProduct quando vindo da API real

    # ------------------------------------------------------------------
    # Modo 1: Produto manual (afiliados.json)
    # ------------------------------------------------------------------

    sc = None  # cliente Shopee (definido nos modos URL/KEYWORD)
    if produto and not shopee_url and not shopee_keyword:
        nome = produto.get("nome", "Produto")
        descricao = produto.get("descricao", "")
        imagens_raw = produto.get("imagens", [])
        link = produto.get("link_shopee", "")
        logger.info("Modo MANUAL – produto: %s", nome)

    # ------------------------------------------------------------------
    # Modo 2: URL do produto Shopee → get_product_details()
    # ------------------------------------------------------------------
    elif shopee_url:
        sc = _get_shopee_client()
        if sc is None:
            raise RuntimeError(
                "Cliente Shopee não inicializado. "
                "Configure SHOPEE_PARTNER_ID e SHOPEE_PARTNER_KEY no .env"
            )
        logger.info("Modo URL – buscando detalhes: %s", shopee_url[:80])
        try:
            product_obj = sc.get_product_details(shopee_url)
        except ShopeeProductNotFoundError as exc:
            raise RuntimeError(f"Produto indisponível ou não encontrado: {exc}") from exc
        except ShopeeTimeoutError as exc:
            raise RuntimeError(f"Timeout na API Shopee: {exc}") from exc
        except ShopeeAffiliateError as exc:
            raise RuntimeError(f"Erro ao buscar produto na Shopee: {exc}") from exc

        nome = product_obj.name or nome
        descricao = product_obj.description
        imagens_raw = product_obj.images
        price = product_obj.price
        discount = product_obj.discount
        rating = product_obj.rating
        sold = product_obj.sold
        if product_obj.price_original > product_obj.price > 0:
            original_price_str = _format_brl(product_obj.price_original)

        # Link curto de afiliado com subId (numérico, exigido pela API)
        base_url = product_obj.product_url or product_obj.offer_link or shopee_url
        link = sc.generate_affiliate_link(base_url, sub_id=str(int(time.time()))[-5:])
        logger.info(
            "Produto selecionado (URL): %s – R$ %.2f – %.1f★ – %d vendidos",
            nome[:50], price, rating, sold,
        )
        logger.info("Link de afiliado gerado: %s", link)

    # ------------------------------------------------------------------
    # Modo 3: Keyword de busca → search_products() + primeiro resultado
    # ------------------------------------------------------------------
    elif shopee_keyword:
        sc = _get_shopee_client()
        if sc is None:
            raise RuntimeError(
                "Cliente Shopee não inicializado. "
                "Configure SHOPEE_PARTNER_ID e SHOPEE_PARTNER_KEY no .env"
            )
        logger.info("Modo KEYWORD – buscando: '%s'", shopee_keyword)
        try:
            products = sc.search_products(shopee_keyword, limit=5)
        except ShopeeTimeoutError as exc:
            raise RuntimeError(f"Timeout na API Shopee: {exc}") from exc
        except ShopeeAffiliateError as exc:
            raise RuntimeError(f"Erro ao buscar produtos: {exc}") from exc

        logger.info(
            "%d produtos encontrados para '%s'.", len(products), shopee_keyword
        )
        if not products:
            raise RuntimeError(
                f"Nenhum produto encontrado para: {shopee_keyword}"
            )

        # Pega o mais relevante/mais vendido (primeiro resultado)
        product_obj = max(products, key=lambda p: p.sold)
        nome = product_obj.name or nome
        descricao = product_obj.description
        imagens_raw = product_obj.images
        price = product_obj.price
        discount = product_obj.discount
        rating = product_obj.rating
        sold = product_obj.sold
        if product_obj.price_original > product_obj.price > 0:
            original_price_str = _format_brl(product_obj.price_original)

        base_url = product_obj.product_url or product_obj.offer_link
        link = (
            sc.generate_affiliate_link(base_url, sub_id=str(int(time.time()))[-5:])
            if base_url else ""
        )
        logger.info(
            "Produto selecionado (busca): %s – R$ %.2f – %.1f★ – %d vendidos",
            nome[:50], price, rating, sold,
        )
        logger.info("Link de afiliado gerado: %s", link)

    # ------------------------------------------------------------------
    # Geração do vídeo (comum a todos os modos)
    # ------------------------------------------------------------------

    safe_name = normalize_filename(nome)
    output_path = str(OUTPUT_DIR / f"video_shopee_{safe_name}_{ts}.mp4")
    logger.info("Arquivos salvos com prefixo: video_shopee_%s", safe_name)

    logger.info("=== GERANDO VÍDEO SHOPEE (%s) ===", nome)

    # 1. Copy com dados reais (preço, desconto, rating) – formato reel 25-35s
    tema_reel = nome
    facts: List[str] = []
    if descricao:
        facts.append(f"Descrição: {descricao[:200]}")
    if price > 0:
        facts.append(
            f"PREÇO REAL ATUAL: {original_price_str or ''} por R$ {price:.2f}".strip()
        )
    if discount > 0:
        facts.append(f"DESCONTO REAL: {discount:.0f}% OFF")
    if rating > 0:
        facts.append(f"AVALIAÇÃO REAL: {rating:.1f} estrelas")
    if sold > 0:
        facts.append(f"UNIDADES VENDIDAS: {sold}")
    if link:
        facts.append(f"LINK DE AFILIADO: {link}")
    if facts:
        tema_reel = nome + "\n\nDados reais do produto:\n" + "\n".join(facts)

    logger.info("Etapa 1/5 – Gerando copy SHOPEE (reel)…")
    try:
        script = generate_scripts("shopee", tema_reel, duracao="reel")
    except ValueError as exc:
        # Reels curtos variam muito; aceita-se o melhor resultado com warning
        logger.warning("Validação do reel flexível: %s", exc)
        script = _gemini_generate(
            f"Copy de 25-35 segundos (70-90 palavras), estilo TikTok/achadinho, "
            f"sobre este produto Shopee com dados reais:\n{tema_reel}\n\n"
            f"Gancho → problema que resolve → preço/desconto/urgência → "
            f"'Clica no link'. APENAS o texto falado."
        ).strip()
    logger.info("Copy SHOPEE (%d chars): %s…", len(script), script[:120])

    # 2. Áudio
    logger.info("Etapa 2/5 – Sintetizando voz SHOPEE…")
    audio_path = str(AUDIO_DIR / f"shopee_{safe_name}_{ts}.mp3")
    synthesize_speech(script, VOICES["SHOPEE"], audio_path)

    # 3. Baixar imagens reais do produto
    logger.info("Etapa 3/6 – Baixando imagens reais do produto…")
    image_paths: List[str] = []
    if product_obj is not None and sc is not None:
        # API real → usa download_product_images()/download_product_image()
        image_paths = sc.download_product_images(product_obj, ASSETS_DIR, max_images=5)
    else:
        for img_url in imagens_raw:
            path = download_image(img_url)
            if path:
                image_paths.append(path)
            else:
                logger.warning("Imagem ignorada: %s", img_url)

    logger.info("%d imagens baixadas.", len(image_paths))
    if not image_paths:
        logger.warning("Nenhuma imagem válida – vídeo será gerado com fundo animado.")

    # 3b. Gerar cartões limpos (foto real recortada, p/ camada de produto)
    card_paths: List[str] = []
    for img in image_paths[:4]:
        try:
            card_paths.append(build_clean_product_card(img))
        except Exception as exc:
            logger.warning("Falha ao gerar cartão (%s): %s", img, exc)

    # 4. Clipes de fundo genéricos do Pexels (multi-camada, ~40% opacidade)
    logger.info("Etapa 4/6 – Buscando clipes de fundo no Pexels…")
    queries = _build_background_queries(nome, descricao)
    bg_clips = download_brolls(queries, count=4, orientation="portrait")

    # 5. Renderização cinematográfica vertical 9:16
    logger.info("Etapa 5/6 – Renderizando vídeo SHOPEE cinematográfico…")
    price_str = _format_brl(price) if price > 0 else ""
    render_cinematic_shopee(
        card_paths=card_paths,
        clips=bg_clips,
        audio_path=audio_path,
        output_path=output_path,
        product_name=nome,
        price_str=price_str,
        discount_pct=discount,
        rating=rating,
    )

    # 6. Salvar metadados do produto (dados brutos da API) + YouTube metadata/thumbnail
    product_meta_path = str(OUTPUT_DIR / f"video_shopee_{safe_name}_{ts}_metadata.json")
    product_meta = {
        "produto": nome,
        "item_id": getattr(product_obj, "item_id", ""),
        "shop_id": getattr(product_obj, "shop_id", ""),
        "preco": price,
        "desconto": discount,
        "rating": rating,
        "vendidos": sold,
        "link_afiliado": link,
        "url_produto": getattr(product_obj, "product_url", ""),
        "video_path": output_path,
        "roteiro": script,
        "imagens_baixadas": len(image_paths),
        "gerado_em": datetime.now().isoformat(),
    }
    with open(product_meta_path, "w", encoding="utf-8") as f:
        json.dump(product_meta, f, ensure_ascii=False, indent=2)
    logger.info("Metadados salvos: %s", product_meta_path)

    # Metadados YouTube + thumbnail com a foto real do produto como fundo
    _publish_metadata(
        "shopee", output_path, nome, script,
        extras={
            "preco": price,
            "preco_original": getattr(product_obj, "price_original", 0.0),
            "desconto": discount,
            "rating": rating,
            "vendidos": sold,
            "link_afiliado": link,
        },
        fundo_thumb=image_paths[0] if image_paths else None,
    )

    logger.info("=== VÍDEO SHOPEE CONCLUÍDO → %s ===", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Ponto de entrada (modo CLI)
# ---------------------------------------------------------------------------


def _selftest_normalize() -> None:
    """Testes rápidos da normalização de nomes de arquivo."""
    cases = [
        ("The Microchip Supply Bottleneck", "microchip_supply_bottleneck"),
        ("Suporte Articulado para Monitor", "suporte_articulado_monitor"),
        ("Como Montar um Setup de Programação por Menos de R$ 500", "montar_setup_programacao_menos"),
        ("TECLADO MECÂNICO RGB!! Gamer 2026", "teclado_mecanico_rgb_gamer"),
        ("A IA e o Futuro dos Chips", "ia_futuro_chips"),
        ("", "video"),
    ]
    failures = 0
    for text, expected in cases:
        got = normalize_filename(text)
        ok = got == expected
        if not ok:
            failures += 1
        print(f"{'OK ' if ok else 'FAIL'} {text!r:55} -> {got!r} (esperado {expected!r})")
    assert all(len(normalize_filename(t)) <= 30 for t, _ in cases), "slug > 30 chars"
    print("SELFTEST:", "TODOS OK" if failures == 0 else f"{failures} FALHAS")


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "selftest":
        _selftest_normalize()
        sys.exit(0)

    if len(sys.argv) < 2:
        print("Uso:")
        print("  python engine.py selftest                      # testa normalização")
        print("  python engine.py global <tema> <query_pexels>      # documentário 8-12 min")
        print("  python engine.py global_short <tema> <query>       # Shorts 60-90s")
        print("  python engine.py brasil <tema> <query_pexels>      # documentário 8-10 min")
        print("  python engine.py brasil_short <tema> <query>       # Shorts 60-90s")
        print("  python engine.py shopee <nome_produto>")
        print("  python engine.py shopee_url <url_shopee>")
        print("  python engine.py shopee_search <palavra_chave>")
        sys.exit(1)

    mode = sys.argv[1].lower()

    try:
        if mode in ("global", "global_long"):
            tema = sys.argv[2] if len(sys.argv) > 2 else "AI regulation geopolitics"
            query = sys.argv[3] if len(sys.argv) > 3 else tema
            path = generate_global_video(tema, query, duracao="long")
            print(f"Vídeo gerado: {path}")

        elif mode == "global_short":
            tema = sys.argv[2] if len(sys.argv) > 2 else "AI regulation geopolitics"
            query = sys.argv[3] if len(sys.argv) > 3 else tema
            path = generate_short_video("global", tema, query)
            print(f"Vídeo gerado: {path}")

        elif mode in ("brasil", "brasil_long"):
            tema = sys.argv[2] if len(sys.argv) > 2 else "review teclado mecanico"
            query = sys.argv[3] if len(sys.argv) > 3 else tema
            afiliados = load_afiliados()
            produtos = [
                {"nome": v.get("nome", k), "link": v.get("link_shopee", "")}
                for k, v in afiliados.items()
                if isinstance(v, dict) and "nome" in v
            ]
            path = generate_brasil_video(tema, query, produtos, duracao="long")
            print(f"Vídeo gerado: {path}")

        elif mode == "brasil_short":
            tema = sys.argv[2] if len(sys.argv) > 2 else "review teclado mecanico"
            query = sys.argv[3] if len(sys.argv) > 3 else tema
            path = generate_short_video("brasil", tema, query)
            print(f"Vídeo gerado: {path}")

        elif mode == "shopee":
            produto_key = sys.argv[2] if len(sys.argv) > 2 else "suporte_monitor"
            afiliados = load_afiliados()
            if produto_key not in afiliados:
                print(f"Produto '{produto_key}' não encontrado em afiliados.json")
                print(f"Disponíveis: {list(afiliados.keys())}")
                sys.exit(1)
            path = generate_shopee_video(produto=afiliados[produto_key])
            print(f"Vídeo gerado: {path}")

        elif mode == "shopee_url":
            url = sys.argv[2] if len(sys.argv) > 2 else ""
            if not url:
                print("Uso: python engine.py shopee_url <url_shopee>")
                sys.exit(1)
            path = generate_shopee_video(shopee_url=url)
            print(f"Vídeo gerado: {path}")

        elif mode == "shopee_search":
            keyword = sys.argv[2] if len(sys.argv) > 2 else "teclado mecanico"
            path = generate_shopee_video(shopee_keyword=keyword)
            print(f"Vídeo gerado: {path}")

        else:
            print(f"Modo desconhecido: {mode}")
            sys.exit(1)

    except Exception as exc:
        logger.exception("Pipeline falhou: %s", exc)
        sys.exit(1)
