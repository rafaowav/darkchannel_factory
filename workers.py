"""
workers.py – Handlers de estágio executados pelo JobManager.

Cada função é um estágio do pipeline (research → script → render → review),
registra artefatos na pasta do job e atualiza o status no SQLite.
Toda chamada bloqueante vai para thread via services/render_service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from database import get_db
from models import JobStatus, JobType, SCRIPT_LIMITS, utcnow_iso
from utils.files import JobFolders, ensure_free_space, redact_secrets

logger = logging.getLogger(__name__)

# callback para enviar arquivos/mensagens ao admin: async fn(chat_id, kind, payload)
_send_fn: Optional[Any] = None


def set_notify_sender(fn: Any) -> None:
    """Injeta o sender do bot (evita import circular bot ↔ workers)."""
    global _send_fn
    _send_fn = fn


async def notify(job_id: str, text: str) -> None:
    if _send_fn:
        try:
            await _send_fn("text", {"job_id": job_id, "text": text})
        except Exception as exc:
            logger.warning("notify falhou: %s", exc)


async def send_artifact(job_id: str, path: str, caption: str = "") -> None:
    if _send_fn:
        try:
            await _send_fn("file", {"job_id": job_id, "path": path, "caption": caption})
        except Exception as exc:
            logger.warning("send_artifact falhou (%s): %s", path, exc)


def _job_or_raise(job_id: str):  # type: ignore[no-untyped-def]
    job = get_db().get_job(job_id)
    if job is None:
        raise RuntimeError(f"Job {job_id} não existe.")
    return job


# ===========================================================================
# Estágio RESEARCHING: briefing + roteiro + aprovação humana
# ===========================================================================


async def stage_research(job_id: str) -> None:
    db = get_db()
    job = _job_or_raise(job_id)
    folder = JobFolders(job.id, job.title)
    db.set_folder(job.id, str(folder.path))
    db.update_status(job.id, JobStatus.RESEARCHING.value)
    folder.write_input({"job_id": job.id, "type": job.type, "title": job.title,
                        "payload": job.payload, "created_at": job.created_at})

    tipo_engine = "shopee" if job.type == JobType.SHOPEE_SHORT.value else \
        ("global" if job.type == JobType.GLOBAL_LONG.value else "brasil")
    tema = job.payload.get("tema") or job.title

    # ---- briefing (não aplica a reels Shopee; lá o snapshot substitui) ----
    produtos_payload: List[Dict[str, Any]] = job.payload.get("produtos", [])
    research_text = ""
    if tipo_engine != "shopee":
        from services.research_service import build_briefing

        await notify(job_id, "🔎 Pesquisando briefing…")
        prod_desc = "\n".join(f"- {p.get('nome')}: {p.get('link', '')}" for p in produtos_payload)
        research_text = build_briefing(tipo_engine, tema, prod_desc or None)
        folder.write_text(folder.research_md, research_text)

    # ---- dados reais do produto (BRASIL com busca Shopee / SHOPEE) ----
    extras: Optional[Dict[str, Any]] = None
    snapshots: List[Dict[str, Any]] = []
    if tipo_engine == "shopee":
        from services import shopee_service

        await notify(job_id, "🛍️ Consultando API Shopee (dados reais)…")
        url = job.payload.get("url")
        keyword = job.payload.get("keyword") or tema
        product = None
        try:
            if url:
                product = await asyncio.to_thread(shopee_service.details, url)
            else:
                found = await asyncio.to_thread(shopee_service.search, keyword, 5)
                if not found:
                    raise RuntimeError(f"Nenhum produto válido encontrado para '{keyword}'.")
                product = max(found, key=lambda p: p.sold)
        except Exception as exc:
            db.update_status(job_id, JobStatus.FAILED.value, error=redact_secrets(str(exc)))
            await notify(job_id, f"❌ Produto indisponível/inválido: {exc}")
            return

        aff = await asyncio.to_thread(
            shopee_service.affiliate_link,
            product.product_url or product.offer_link,
            str(int(asyncio.get_running_loop().time()))[-5:],
        )
        snap = shopee_service.snapshot(product, affiliate_url=aff)
        snapshots = [snap]
        db.save_product_snapshot(job_id, snap)
        folder.write_product_snapshot(snapshots)
        db.update_payload(job_id, product=snap)
        extras = {
            "preco": snap["price"], "preco_original": snap["price_original"],
            "desconto": snap["discount"], "rating": snap["rating"],
            "vendidos": snap["sold"], "link_afiliado": snap["affiliate_url"],
        }
        tema = snap["name"]

    elif tipo_engine == "brasil" and produtos_payload:
        # BRASIL usa os snapshots já escolhidos no fluxo guiado
        snaps = job.payload.get("product_snapshots", [])
        if snaps:
            folder.write_product_snapshot(snaps)
            snapshots = snaps
            for s in snaps:
                db.save_product_snapshot(job_id, s)

    # ---- roteiro validado ----
    duracao = {"global": "long", "brasil": "long", "shopee": "reel"}[tipo_engine]
    tema_prompt = tema
    if tipo_engine == "shopee" and extras:
        facts = [f"DADOS REAIS DA API (use SOMENTE estes números):\n"
                 f"Preço: R$ {extras['preco']:.2f}",
                 f"Desconto: {extras['desconto']:.0f}% OFF" if extras["desconto"] else "",
                 f"Avaliação: {extras['rating']:.1f}★" if extras["rating"] else "",
                 f"Vendidos: {extras['vendidos']}"]
        tema_prompt = tema + "\n\n" + "\n".join(f for f in facts if f)
    elif snapshots:
        linhas = [f"- {s['name'][:60]} — R$ {s['price']:.2f}, {s['rating']:.1f}★, "
                  f"{s['sold']} vendidos (consulta {s['checked_at']})" for s in snapshots]
        tema_prompt = tema + "\n\nPRODUTOS REAIS (use SOMENTE estes preços/dados):\n" + "\n".join(linhas)

    from services.script_service import (
        format_duration, generate_script, script_stats,
    )

    await notify(job_id, "✍️ Gerando roteiro…")
    min_w, max_w = SCRIPT_LIMITS[job.job_type]
    try:
        script = await asyncio.to_thread(generate_script, tipo_engine, tema_prompt, duracao)
    except ValueError as exc:
        db.update_status(job_id, JobStatus.FAILED.value, error=redact_secrets(str(exc)))
        await notify(job_id, f"❌ Roteiro reprovado na validação: {exc}")
        return

    words, est_sec = script_stats(script)
    folder.write_script(script)
    db.update_payload(job_id, word_count=words, est_seconds=est_sec, script_path=str(folder.script_txt))
    db.update_status(job_id, JobStatus.AWAITING_SCRIPT_APPROVAL.value)

    resumo = " ".join(script.split(". ")[0:2])[:400]
    await notify(
        job_id,
        f"📄 Roteiro pronto: {words} palavras (~{format_duration(est_sec)}).\n\n{resumo}…",
    )
    await send_artifact(job_id, str(folder.script_txt), f"Roteiro {job_id}")
    if _send_fn:
        await _send_fn("script_approval", {"job_id": job_id})


# ===========================================================================
# Estágio RENDERING: áudio + assets + FFmpeg + metadata + review
# ===========================================================================


async def stage_render(job_id: str) -> None:
    db = get_db()
    job = _job_or_raise(job_id)
    folder = JobFolders(job.id, job.title)
    ensure_free_space()
    db.update_status(job.id, JobStatus.RENDERING.value)
    await notify(job_id, "🎞️ Renderizando… (fila ativa: use /status)")

    tipo_engine = "shopee" if job.type == JobType.SHOPEE_SHORT.value else \
        ("global" if job.type == JobType.GLOBAL_LONG.value else "brasil")

    script = folder.script_txt.read_text(encoding="utf-8")

    # ---- áudio ----
    from engine import VOICES
    from services.render_service import synthesize

    await synthesize(script, VOICES[tipo_engine.upper()], str(folder.narration_mp3))
    from engine import get_media_duration

    audio_sec = get_media_duration(str(folder.narration_mp3))
    db.register_asset(job_id, "audio", str(folder.narration_mp3))
    logger.info("Áudio real: %.0fs", audio_sec)

    # ---- SRT ----
    from services.script_service import build_srt

    srt = build_srt(script, audio_sec)
    if srt:
        folder.write_text(folder.subtitles_srt, srt)
        db.register_asset(job_id, "srt", str(folder.subtitles_srt))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    from engine import normalize_filename

    slug = normalize_filename(job.title)
    video_name = f"video_{job.type.replace('_short', '')}_{slug}_{ts}.mp4"
    video_path = str(folder.video_path(video_name))

    if tipo_engine == "shopee":
        video_out = await _render_shopee(job, folder, script, audio_sec, video_path)
    else:
        video_out = await _render_documentary(job, folder, tipo_engine, audio_sec, video_path)

    db.register_asset(job_id, "video", video_out)

    try:
        # ---- metadata + thumbnail ----
        from services.metadata_service import (
            build_review_markdown, generate_metadata, generate_thumbnail,
        )

        produtos_links = job.payload.get("produtos")
        extras = None
        snap = job.payload.get("product")
        if snap:
            extras = {
                "preco": snap.get("price"), "preco_original": snap.get("price_original"),
                "desconto": snap.get("discount"), "rating": snap.get("rating"),
                "vendidos": snap.get("sold"), "link_afiliado": snap.get("affiliate_url"),
            }
        meta = await asyncio.to_thread(
            generate_metadata, tipo_engine, job.title, script, produtos_links, extras
        )
        thumb_fundo = None
        imgs = [a["path"] for a in db.list_assets(job_id) if a["kind"] == "image"]
        if imgs:
            thumb_fundo = imgs[0]
        preco_str = f"R$ {extras['preco']:.2f}".replace(".", ",") if extras and extras.get("preco") else ""
        await asyncio.to_thread(
            generate_thumbnail, tipo_engine, meta["titulo"], thumb_fundo,
            str(folder.thumbnail_jpg), preco_str, job.title,
        )
        db.register_asset(job_id, "thumbnail", str(folder.thumbnail_jpg))

        metadata_doc = {
            "job_id": job_id,
            "video_file": video_out,
            "thumbnail_file": str(folder.thumbnail_jpg),
            "tipo": tipo_engine,
            **meta,
            "gerado_em": utcnow_iso(),
        }
        folder.write_metadata(metadata_doc)
        db.update_payload(job_id, video_path=video_out, metadata=meta, audio_seconds=audio_sec)

        words = len(script.split())
        review = build_review_markdown(job_id, meta, video_out, audio_sec, words,
                                       job.payload.get("product_snapshots"))
        folder.write_review(review)
    except Exception as exc:
        logger.exception("Metadados/thumbnail falharam para %s (vídeo preservado)", job_id)
        db.update_payload(job_id, video_path=video_out)
        folder.write_text(
            folder.review_md,
            f"# Review — {job_id}\n\nVídeo OK, mas metadados falharam: "
            f"{redact_secrets(str(exc))}\nRode /regerar depois.",
        )
        meta = {}

    db.update_status(job_id, JobStatus.RENDERED.value)
    db.update_status(job_id, JobStatus.REVIEW_REQUIRED.value)

    await notify(job_id, f"✅ Render concluído ({audio_sec / 60:.1f} min): {video_name}")
    await send_artifact(job_id, video_out, f"🎬 {job_id} preview")
    if meta:
        await send_artifact(job_id, str(folder.thumbnail_jpg), f"🖼️ Thumbnail {job_id}")
    await send_artifact(job_id, str(folder.review_md), f"📋 Review {job_id}")
    if _send_fn:
        await _send_fn("review", {"job_id": job_id})


async def _render_documentary(job, folder, tipo_engine: str, audio_sec: float, video_path: str) -> str:
    """16:9 com B-roll variado (múltiplos clipes encadeados, sem loop único)."""
    from engine import render_slideshow_clips
    from services.render_service import download_background_clips

    query = job.payload.get("query_pexels") or job.title
    base = [t.strip() for t in query.split(",") if t.strip()]
    queries = (base + [query, "technology abstract", "cinematic b roll"])[:4]
    clips = await download_background_clips(queries, count=3, orientation="landscape")
    if not clips:
        raise RuntimeError("Nenhum clipe de B-roll disponível (Pexels offline/vazio).")
    return await asyncio.to_thread(
        render_slideshow_clips, clips, str(folder.narration_mp3), video_path,
        width=1920, height=1080, fps=30, transition=1.0,
    )


async def _render_shopee(job, folder, script: str, audio_sec: float, video_path: str) -> str:
    """9:16 cinematográfico multi-cena com fotos reais do anúncio."""
    from services.render_service import (
        build_cards, download_background_clips, download_product_images,
        render_shopee_reel,
    )

    snap = job.payload.get("product") or {}
    images = await download_product_images_from_snap(job, folder)
    cards = await build_cards(images)
    queries = _bg_queries_for(snap.get("name", job.title))
    clips = await download_background_clips(queries, count=3, orientation="portrait")
    price_str = f"R$ {snap.get('price', 0):.2f}".replace(".", ",") if snap.get("price") else ""
    return await render_shopee_reel(
        cards, clips, str(folder.narration_mp3), video_path,
        snap.get("name", job.title), price_str,
        float(snap.get("discount", 0) or 0), float(snap.get("rating", 0) or 0),
        duration=None,
    )


async def download_product_images_from_snap(job, folder) -> List[str]:  # type: ignore[no-untyped-def]
    """Rebaixa imagens reais a partir do snapshot salvo no payload."""
    from shopee_client import init_shopee_client

    db = get_db()
    snap = job.payload.get("product")
    if not snap:
        return []
    client = init_shopee_client()
    if client is None:
        return []

    class _P:  # adapter mínimo p/ download_product_images
        item_id = str(snap.get("item_id", ""))
        name = snap.get("name", "")
        images = snap.get("images", [])
        raw: Dict[str, Any] = {}

    paths = await asyncio.to_thread(client.download_product_images, _P(), str(folder.path), 5)
    for p in paths:
        db.register_asset(job.id, "image", p)
    return paths


def _bg_queries_for(name: str) -> List[str]:
    from engine import _build_background_queries

    return _build_background_queries(name, "")


# ===========================================================================
# Estágio REVIEW_REQUIRED: nada automático — espera humano.
# Estágios auxiliares chamados pelos callbacks do bot:
# ===========================================================================


async def queue_render(job_id: str) -> None:
    """Marca queued_render e enfileira (chamado após aprovar roteiro)."""
    db = get_db()
    if db.update_status(job_id, JobStatus.QUEUED_RENDER.value):
        from job_manager import get_job_manager

        await get_job_manager().enqueue(job_id)


async def regenerate(job_id: str, what: str) -> None:
    """Regeneração parcial: script | thumb | video."""
    db = get_db()
    job = _job_or_raise(job_id)
    if what == "script":
        ok = db.update_status(job_id, JobStatus.RESEARCHING.value)
        if ok:
            from job_manager import get_job_manager

            await get_job_manager().enqueue(job_id)
    elif what in ("thumb", "video"):
        ok = db.update_status(job_id, JobStatus.QUEUED_RENDER.value)
        if ok:
            from job_manager import get_job_manager

            await get_job_manager().enqueue(job_id)
    else:
        await notify(job_id, f"⚠️ Alvo de regeneração desconhecido: {what}")


def register_stages(mgr) -> None:  # type: ignore[no-untyped-def]
    """Registra os handlers de estágio no JobManager."""
    mgr.register_stage(JobStatus.RESEARCHING.value, stage_research)
    mgr.register_stage(JobStatus.RENDERING.value, stage_render)
