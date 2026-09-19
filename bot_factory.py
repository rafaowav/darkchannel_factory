"""
bot_factory.py – Painel de controle do youtube_factory no Telegram (aiogram 3).

Fluxo guiado por botões: criação de jobs, seleção de produtos Shopee com
snapshot auditável, aprovação humana de roteiro e de render, fila persistente
(job_manager), lote semanal e registro manual de publicação.

Nada é publicado automaticamente em YouTube/Shopee. Somente o admin
(TELEGRAM_ADMIN_CHAT_ID) pode operar o painel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

load_dotenv()

from utils.files import setup_logging  # noqa: E402

setup_logging()
logger = logging.getLogger("bot")

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID: str = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

if not TELEGRAM_BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN não configurado. Encerrando.")
    sys.exit(1)
if not TELEGRAM_ADMIN_CHAT_ID:
    logger.warning("TELEGRAM_ADMIN_CHAT_ID vazio – restrição desabilitada (INSEGURO).")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import keyboards as K  # noqa: E402
from database import get_db  # noqa: E402
from job_manager import get_job_manager, next_batch_id, next_job_id  # noqa: E402
from models import JobStatus, JobType  # noqa: E402
from utils.files import JobFolders, redact_secrets  # noqa: E402
import workers  # noqa: E402

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()


def is_admin(message: Optional[Message] = None, query: Optional[CallbackQuery] = None) -> bool:
    if not TELEGRAM_ADMIN_CHAT_ID:
        return True
    if message is not None and message.from_user:
        return str(message.from_user.id) == str(TELEGRAM_ADMIN_CHAT_ID)
    if query is not None and query.from_user:
        return str(query.from_user.id) == str(TELEGRAM_ADMIN_CHAT_ID)
    return False


async def deny_unless_admin_cb(query: CallbackQuery) -> bool:
    if is_admin(query=query):
        return True
    await query.answer("⛔ Acesso negado.", show_alert=True)
    return False


# ---------------------------------------------------------------------------
# Estados FSM
# ---------------------------------------------------------------------------


class GlobalFlow(StatesGroup):
    tema = State()
    visual_query = State()


class BrasilFlow(StatesGroup):
    tema = State()
    source = State()
    keyword = State()
    urls = State()
    visual_query = State()


class ShopeeFlow(StatesGroup):
    entrada = State()
    duration = State()


class TikTokFlow(StatesGroup):
    tema = State()
    legendas = State()


class BatchFlow(StatesGroup):
    kind = State()
    temas_global = State()
    temas_brasil = State()
    keywords_shopee = State()


# ---------------------------------------------------------------------------
# Helpers de UI / progresso
# ---------------------------------------------------------------------------

JOB_PROGRESS_TEXT: Dict[str, str] = {
    JobStatus.IDEA.value: "💡 criado",
    JobStatus.RESEARCHING.value: "🔎 pesquisando/roteirizando",
    JobStatus.SCRIPT_READY.value: "📄 roteiro pronto",
    JobStatus.AWAITING_SCRIPT_APPROVAL.value: "⏸ aguardando aprovação do roteiro",
    JobStatus.QUEUED_RENDER.value: "🚦 na fila de render",
    JobStatus.RENDERING.value: "🎞️ renderizando",
    JobStatus.RENDERED.value: "✅ renderizado",
    JobStatus.REVIEW_REQUIRED.value: "🧐 aguardando revisão final",
    JobStatus.APPROVED.value: "👍 aprovado",
    JobStatus.SCHEDULED.value: "🗓 agendado",
    JobStatus.PUBLISHED.value: "📤 publicado",
    JobStatus.REJECTED.value: "❌ rejeitado",
    JobStatus.FAILED.value: "💥 falhou",
    JobStatus.CANCELLED.value: "🚫 cancelado",
}


def job_line(job) -> str:  # type: ignore[no-untyped-def]
    label = JOB_PROGRESS_TEXT.get(job.status, job.status)
    return f"<code>{job.id}</code> [{job.type}] — {label}"


async def notify_progress(job_id: str, text: str) -> None:
    """Callback do JobManager → Telegram."""
    try:
        await bot.send_message(int(TELEGRAM_ADMIN_CHAT_ID or 0), f"<b>{job_id}</b>\n{text}")
    except Exception as exc:
        logger.error("notify_progress falhou: %s", exc)


async def send_payload(kind: str, payload: Dict[str, Any]) -> None:
    """Sender injetado nos workers (text/file/script_approval/review)."""
    chat_id = int(TELEGRAM_ADMIN_CHAT_ID or 0)
    job_id = payload.get("job_id", "")
    if kind == "text":
        await bot.send_message(chat_id, f"<b>{job_id}</b>\n{payload['text']}")
    elif kind == "file":
        path = Path(payload["path"])
        if not path.exists():
            return
        caption = payload.get("caption", path.name)
        if path.suffix == ".mp4":
            await bot.send_video(chat_id, FSInputFile(str(path)), caption=caption,
                                 supports_streaming=True)
        elif path.suffix in (".jpg", ".png"):
            await bot.send_photo(chat_id, FSInputFile(str(path)), caption=caption)
        else:
            await bot.send_document(chat_id, FSInputFile(str(path)), caption=caption)
    elif kind == "script_approval":
        from database import get_db as gdb

        job = gdb().get_job(job_id)
        if job:
            await bot.send_message(
                chat_id,
                f"📋 <b>Aprove o roteiro de {job_id}</b>\n{job_line(job)}\n\n"
                "Render só começa após sua aprovação.",
                reply_markup=K.script_approval(job_id),
            )
    elif kind == "review":
        await bot.send_message(
            chat_id,
            f"🧐 <b>Revisão final de {job_id}</b>\n"
            "Confira vídeo, thumbnail e review.md antes de aprovar.\n"
            "Publicação é SEMPRE manual.",
            reply_markup=K.review_actions(job_id),
        )


# ---------------------------------------------------------------------------
# Criação de jobs
# ---------------------------------------------------------------------------


async def create_job(
    job_type: JobType, title: str, payload: Dict[str, Any], batch_id: Optional[str] = None
) -> str:
    prefix = {
        JobType.GLOBAL_LONG: "GLB",
        JobType.BRASIL_LONG: "BR",
        JobType.SHOPEE_SHORT: "SP",
        JobType.TIKTOK_SHORT: "TT",
    }[job_type]
    job_id = next_job_id(prefix)
    db = get_db()
    db.create_job(job_id, job_type.value, title[:80], payload, batch_id)
    await get_job_manager().enqueue(job_id)
    logger.info("Job criado: %s (%s) '%s'", job_id, job_type.value, title[:50])
    return job_id


def _parse_lines(text: str) -> List[str]:
    items = [l.strip() for l in re.split(r"\n|;", text) if l.strip()]
    return items


def _valid_http_url(url: str) -> bool:
    return url.startswith(("http://", "https://")) and ("shopee." in url or "." in url)


# ---------------------------------------------------------------------------
# Comandos básicos
# ---------------------------------------------------------------------------


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not is_admin(message):
        await message.answer("⛔ Acesso restrito ao administrador.")
        return
    await message.answer(
        "🎬 <b>YouTube Factory — Painel</b>\n\n"
        "Escolha o que vamos produzir hoje:",
        reply_markup=K.main_menu(),
    )


@dp.message(Command("novo"))
async def cmd_novo(message: Message) -> None:
    if not is_admin(message):
        return
    await message.answer("O que vamos criar?", reply_markup=K.main_menu())


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not is_admin(message):
        return
    await message.answer(
        "<b>Comandos:</b>\n"
        "<code>/novo</code> menu guiado\n"
        "<code>/global &lt;tema&gt;</code> documentário EN 8-12 min\n"
        "<code>/brasil &lt;tema&gt;</code> documentário PT 8-10 min\n"
        "<code>/shopee &lt;busca&gt;</code> reel vertical com produto real\n"
        "<code>/shopee_url &lt;url&gt;</code> reel a partir de URL\n"
        "<code>/lote_semana</code> lote semanal com fases\n"
        "<code>/status [job]</code> · <code>/jobs</code> · <code>/preview &lt;job&gt;</code>\n"
        "<code>/metadados &lt;job&gt;</code> · <code>/aprovar &lt;job&gt;</code>\n"
        "<code>/rejeitar &lt;job&gt; &lt;motivo&gt;</code> · <code>/regerar &lt;job&gt; alvo</code>\n"
        "<code>/publicado &lt;job&gt; &lt;url&gt;</code> · <code>/cancelar &lt;job&gt;</code>\n"
        "<code>/retry &lt;job_id|all&gt;</code> re-enfileira job que falhou\n"
        "<code>/limpar_cache</code>",
    )


# ---------------------------------------------------------------------------
# Menu principal (callbacks new:*)
# ---------------------------------------------------------------------------


@dp.callback_query(F.data.startswith("new:"))
async def cb_new(query: CallbackQuery, state: FSMContext) -> None:
    if not await deny_unless_admin_cb(query):
        return
    kind = query.data.split(":", 1)[1]
    if kind == "tiktok_short":
        await state.set_state(TikTokFlow.tema.state)
        await query.message.edit_text("Digite o <b>tema</b> do TikTok (até 15s):")  # type: ignore[union-attr]
        await query.answer()
        return
    await query.message.edit_text("Digite o <b>tema</b> do vídeo:")  # type: ignore[union-attr]
    if kind == "global_long":
        await state.set_state(GlobalFlow.tema.state)
        await state.update_data(job_type=JobType.GLOBAL_LONG.value)
    elif kind == "brasil_long":
        await state.set_state(BrasilFlow.tema.state)
        await state.update_data(job_type=JobType.BRASIL_LONG.value)
    else:
        await state.set_state(ShopeeFlow.entrada.state)
        await query.message.edit_text(  # type: ignore[union-attr]
            "Envie uma <b>palavra-chave</b> ou a <b>URL do produto</b> na Shopee:"
        )
    await query.answer()


# ---------------------------------------------------------------------------
# Fluxo GLOBAL
# ---------------------------------------------------------------------------


@dp.message(GlobalFlow.tema, F.text)
async def global_tema(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    tema = message.text.strip()
    await state.update_data(tema=tema)
    await state.set_state(GlobalFlow.visual_query.state)
    suggestions = [tema, f"{tema} documentary", "technology abstract", "city night aerial"]
    await message.answer(
        f"Tema: <i>{tema}</i>\n\nEscolha a <b>query visual</b> do Pexels (ou escreva outra):",
        reply_markup=K.visual_query_suggestions(suggestions),
    )


@dp.callback_query(GlobalFlow.visual_query, F.data.startswith("vq:"))
async def global_vq(query: CallbackQuery, state: FSMContext) -> None:
    if not await deny_unless_admin_cb(query):
        return
    value = query.data.split(":", 1)[1]
    if value == "manual":
        await query.message.edit_text("Digite a query visual desejada:")  # type: ignore[union-attr]
        await state.set_state(GlobalFlow.visual_query.state)
        state_data = await state.get_data()
        state_data["awaiting_raw_query"] = True
        await state.set_data(state_data)
        await query.answer()
        return
    data = await state.get_data()
    await _finish_global(query.message.chat.id, data["tema"], value)  # type: ignore[union-attr]
    await state.clear()
    await query.message.edit_text(f"✅ Job criado para <i>{data['tema']}</i>.")  # type: ignore[union-attr]
    await query.answer()


@dp.message(GlobalFlow.visual_query, F.text)
async def global_vq_text(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    data = await state.get_data()
    await _finish_global(message.chat.id, data["tema"], message.text.strip())
    await state.clear()


async def _finish_global(chat_id: int, tema: str, query_pexels: str) -> None:
    job_id = await create_job(
        JobType.GLOBAL_LONG, tema, {"tema": tema, "query_pexels": query_pexels}
    )
    await bot.send_message(
        chat_id,
        f"🌍 <b>Job {job_id}</b> criado e na fila de pesquisa.\n"
        "Você receberá o roteiro para aprovação.",
    )


# ---------------------------------------------------------------------------
# Fluxo BRASIL
# ---------------------------------------------------------------------------


@dp.message(BrasilFlow.tema, F.text)
async def brasil_tema(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    tema = message.text.strip()
    await state.update_data(tema=tema)
    await state.set_state(BrasilFlow.source.state)
    await message.answer(
        f"Tema: <i>{tema}</i>\n\nDe onde vêm os <b>produtos</b> do roteiro?",
        reply_markup=K.product_source_choices(),
    )


@dp.callback_query(BrasilFlow.source, F.data.startswith("psrc:"))
async def brasil_source(query: CallbackQuery, state: FSMContext) -> None:
    if not await deny_unless_admin_cb(query):
        return
        mode = query.data.split(":", 1)[1]
    if mode == "search":
        await state.set_state(BrasilFlow.keyword.state)
        await query.message.edit_text("Palavra-chave da busca Shopee:")  # type: ignore[union-attr]
    elif mode == "catalog":
        afiliados = _load_catalog()
        if not afiliados:
            await query.message.edit_text("Catálogo local vazio (afiliados.json).")  # type: ignore[union-attr]
            await state.clear()
        else:
            rows = [[(f"{'⬜'} {p['nome'][:40]} (id={i})", f"ppick:{i}")] for i, p in enumerate(afiliados)]
            rows.append([("➡️ Continuar sem seleção", "pcatalog_done")])
            await state.update_data(catalog=afiliados, selected=[], mode="catalog")
            await state.set_state(BrasilFlow.visual_query.state)
            await query.message.edit_text("Selecione 2-4 produtos do catálogo:", reply_markup=K.kb(rows))  # type: ignore[union-attr]
    else:  # urls
        await state.set_state(BrasilFlow.urls.state)
        await query.message.edit_text(  # type: ignore[union-attr]
            "Cole as URLs dos produtos (uma por linha, 2-4):"
        )
    await query.answer()


def _load_catalog() -> List[Dict[str, Any]]:
    try:
        from engine import load_afiliados

        raw = load_afiliados()
        return [
            {"nome": v.get("nome", k), "link": v.get("link_shopee", ""),
             "descricao": v.get("descricao", ""), "imagens": v.get("imagens", [])}
            for k, v in raw.items() if isinstance(v, dict) and "nome" in v
        ]
    except Exception as exc:
        logger.error("Catálogo local: %s", exc)
        return []


@dp.message(BrasilFlow.keyword, F.text)
async def brasil_keyword(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    keyword = message.text.strip()
    await message.answer(f"🔎 Buscando <i>{keyword}</i> na Shopee…")
    try:
        from services import shopee_service

        products = await asyncio.to_thread(shopee_service.search, keyword, 6)
    except Exception as exc:
        await message.answer(f"❌ {redact_secrets(str(exc))}")
        await state.clear()
        return
    if not products:
        await message.answer("Nenhum produto válido encontrado.")
        await state.clear()
        return
    ids = list(range(len(products)))
    await state.update_data(mode="search", candidates=[p.to_dict() for p in products],
                            selected=[], keyword=keyword)
    rows = [[(f"⬜ {p.name[:45]} — {p.price_str}", f"ppick:{i}")] for i, p in enumerate(products)]
    rows.append([("Selecione 2-4 produtos", "noop")])
    await message.answer("Toque para selecionar 2-4 produtos:", reply_markup=K.kb(rows))
    await state.set_state(BrasilFlow.visual_query.state)


@dp.callback_query(BrasilFlow.visual_query, F.data.startswith("ppick:"))
async def brasil_pick(query: CallbackQuery, state: FSMContext) -> None:
    if not await deny_unless_admin_cb(query):
        return
    data = await state.get_data()
    idx = int(query.data.split(":")[1])
    selected: List[int] = data.get("selected", [])
    if idx in selected:
        selected.remove(idx)
    elif len(selected) < 4:
        selected.append(idx)
    await state.update_data(selected=selected)

    if data.get("mode") == "catalog":
        catalog = data["catalog"]
        rows = [[(f"{'✅' if i in selected else '⬜'} {p['nome'][:40]} (id={i})", f"ppick:{i}")]
                for i, p in enumerate(catalog)]
        rows.append([("➡️ Continuar", "pcatalog_done")])
        await query.message.edit_reply_markup(reply_markup=K.kb(rows))  # type: ignore[union-attr]
    else:
        candidates = data["candidates"]
        rows = [[(f"{'✅' if i in selected else '⬜'} {c['name'][:45]} — R$ {c['price']:.2f}".replace(".", ","), f"ppick:{i}")]
                for i, c in enumerate(candidates)]
        ready = 2 <= len(selected) <= 4
        rows.append([("➡️ Confirmar seleção" if ready else f"Selecione {len(selected)}/2-4",
                      "pdone" if ready else "noop")])
        await query.message.edit_reply_markup(reply_markup=K.kb(rows))  # type: ignore[union-attr]
    await query.answer()


@dp.callback_query(BrasilFlow.visual_query, F.data == "pcatalog_done")
async def brasil_catalog_done(query: CallbackQuery, state: FSMContext) -> None:
    if not await deny_unless_admin_cb(query):
        return
    data = await state.get_data()
    catalog = data["catalog"]
    selected = data.get("selected", [])
    produtos = [catalog[i] for i in selected] if selected else catalog[:3]
    await _finish_brasil(query.message.chat.id, data["tema"], produtos, [])  # type: ignore[union-attr]
    await state.clear()
    await query.answer("Selecionado.")


@dp.callback_query(BrasilFlow.visual_query, F.data == "pdone")
async def brasil_done(query: CallbackQuery, state: FSMContext) -> None:
    if not await deny_unless_admin_cb(query):
        return
    data = await state.get_data()
    candidates = data["candidates"]
    selected = data.get("selected", [])
    if not (2 <= len(selected) <= 4):
        await query.answer("Selecione entre 2 e 4 produtos.", show_alert=True)
        return
    produtos, snapshots = [], []
    from services.shopee_service import affiliate_link

    for i in selected:
        c = candidates[i]
        try:
            aff = await asyncio.to_thread(affiliate_link, c.get("product_url") or c.get("affiliate_url", ""))
        except Exception:
            aff = c.get("affiliate_url", "")
        prod = {"nome": c["name"], "link": aff}
        produtos.append(prod)
        c2 = dict(c)
        c2["affiliate_url"] = aff
        snapshots.append(c2)
    await _finish_brasil(query.message.chat.id, data["tema"], produtos, snapshots)  # type: ignore[union-attr]
    await state.clear()
    await query.answer("Selecionado.")


@dp.message(BrasilFlow.urls, F.text)
async def brasil_urls(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    urls = [u for u in _parse_lines(message.text) if _valid_http_url(u)][:4]
    if len(urls) < 2:
        await message.answer("Preciso de pelo menos 2 URLs válidas.")
        return
    from services import shopee_service

    produtos, snapshots, erros = [], [], []
    for url in urls:
        try:
            p = await asyncio.to_thread(shopee_service.details, url)
            aff = await asyncio.to_thread(
                shopee_service.affiliate_link, p.product_url or url
            )
            snap = shopee_service.snapshot(p, affiliate_url=aff)
            produtos.append({"nome": p.name, "link": aff})
            snapshots.append(snap)
        except Exception as exc:
            erros.append(redact_secrets(str(exc)))
    if len(produtos) < 2:
        await message.answer("Menos de 2 produtos válidos:\n" + "\n".join(erros[:4]))
        return
    data = await state.get_data()
    await _finish_brasil(message.chat.id, data["tema"], produtos, snapshots)
    await state.clear()


async def _finish_brasil(chat_id: int, tema: str, produtos: List[Dict[str, Any]],
                         snapshots: List[Dict[str, Any]]) -> None:
    job_id = await create_job(
        JobType.BRASIL_LONG, tema,
        {"tema": tema, "query_pexels": tema, "produtos": produtos,
         "product_snapshots": snapshots},
    )
    msg = (f"🇧🇷 <b>Job {job_id}</b> criado com {len(produtos)} produtos reais.\n"
           "⚠️ Preços são snapshots: confira antes de publicar.")
    await bot.send_message(chat_id, msg)


# ---------------------------------------------------------------------------
# Fluxo SHOPEE
# ---------------------------------------------------------------------------


@dp.message(TikTokFlow.tema, F.text)
async def tiktok_tema(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    tema = message.text.strip()
    await state.update_data(tema=tema)
    await state.set_state(TikTokFlow.legendas.state)
    await message.answer(
        f"Tema: <i>{tema}</i>\n\nIncluir legendas no vídeo?",
        reply_markup=K.subtitle_choices(),
    )


@dp.callback_query(TikTokFlow.legendas, F.data.startswith("subs:"))
async def tiktok_subs(query: CallbackQuery, state: FSMContext) -> None:
    if not await deny_unless_admin_cb(query):
        return
    with_subs = query.data.split(":", 1)[1] == "sim"
    data = await state.get_data()
    tema = data["tema"]
    job_id = await create_job(
        JobType.TIKTOK_SHORT, tema,
        {"tema": tema, "query_pexels": tema, "with_subtitles": with_subs},
    )
    await state.clear()
    await query.message.edit_text(  # type: ignore[union-attr]
        f"🎵 <b>Job {job_id}</b> criado (TikTok ≤15s, legendas={'on' if with_subs else 'off'})."
    )
    await query.answer()


@dp.message(ShopeeFlow.entrada, F.text)
async def shopee_entrada(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    text = message.text.strip()
    if _valid_http_url(text):
        await state.update_data(url=text)
        await state.set_state(ShopeeFlow.duration.state)
        await message.answer("Qual duração do reel?", reply_markup=K.duration_choices())
    else:
        await state.update_data(keyword=text)
        await state.set_state(ShopeeFlow.duration.state)
        await message.answer("Qual duração do reel?", reply_markup=K.duration_choices())


@dp.callback_query(ShopeeFlow.duration, F.data.startswith("dur:"))
async def shopee_dur(query: CallbackQuery, state: FSMContext) -> None:
    if not await deny_unless_admin_cb(query):
        return
    seconds = int(query.data.split(":")[1])
    data = await state.get_data()
    payload: Dict[str, Any] = {"duracion_seconds": seconds}
    title = ""
    if data.get("url"):
        payload["url"] = data["url"]
        title = data["url"][:60]
    else:
        payload["keyword"] = data.get("keyword", "")
        title = data.get("keyword", "produto shopee")
    job_id = await create_job(JobType.SHOPEE_SHORT, title, payload)
    await query.message.edit_text(  # type: ignore[union-attr]
        f"🛍️ <b>Job {job_id}</b> ({seconds}s) criado.\n"
        "⚠️ A vinculação do produto/sacolinha na Shopee precisa ser conferida "
        "manualmente no aplicativo antes de postar."
    )
    await state.clear()
    await query.answer()


# ---------------------------------------------------------------------------
# Lote semanal
# ---------------------------------------------------------------------------


@dp.callback_query(F.data == "batch:start")
async def batch_start(query: CallbackQuery, state: FSMContext) -> None:
    if not await deny_unless_admin_cb(query):
        return
    await state.set_state(BatchFlow.kind.state)
    await query.message.edit_text("Escolha o formato do lote semanal:")  # type: ignore[union-attr]
    await query.message.edit_reply_markup(reply_markup=K.batch_kinds())  # type: ignore[union-attr]
    await query.answer()


@dp.callback_query(BatchFlow.kind, F.data.startswith("bkind:"))
async def batch_kind(query: CallbackQuery, state: FSMContext) -> None:
    if not await deny_unless_admin_cb(query):
        return
    kind = query.data.split(":")[1]
    counts = {
        "full": (5, 5, 10), "global": (5, 0, 0),
        "brasil": (0, 5, 0), "shopee": (0, 0, 10),
    }.get(kind, (1, 1, 2))
    await state.set_data({"batch_kind": kind, "counts": counts})
    g, b, s = counts
    prompts = []
    if g:
        prompts.append(f"{g} temas GLOBAL (um por linha)")
    if b:
        prompts.append(f"{b} temas BRASIL (um por linha)")
    if s:
        prompts.append(f"{s} buscas SHOPEE (uma por linha)")
    await state.set_state(BatchFlow.temas_global.state)
    await query.message.edit_text(  # type: ignore[union-attr]
        "Lote " + kind.upper() + " — envie agora, em mensagens separadas:\n"
        + "\n".join(prompts)
    )
    await query.answer()


@dp.message(BatchFlow.temas_global, F.text)
async def batch_collect(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    data = await state.get_data()
    lines = _parse_lines(message.text)
    g, b, s = data["counts"]
    step = data.get("step", "global")
    collected = data.get("collected", {})
    if step == "global" or (step not in ("brasil", "shopee") and g):
        collected["global"] = lines[:g]
        data["step"] = "brasil"
    if data["step"] == "brasil" and b and "brasil" not in collected:
        collected["brasil"] = lines[:b]
        data["step"] = "shopee"
    if data["step"] == "shopee" and s and "shopee" not in collected:
        collected["shopee"] = lines[:s]
    data["collected"] = collected
    missing = [k for k, n in (("global", g), ("brasil", b), ("shopee", s)) if n and k not in collected]
    if missing:
        await state.set_data(data)
        await message.answer(f"Falta enviar: {', '.join(missing)}")
        return
    batch_id = next_batch_id()
    total = g + b + s
    get_db().create_batch(batch_id, data["batch_kind"], total)
    created = []
    for tema in collected.get("global", []):
        jid = await create_job(JobType.GLOBAL_LONG, tema,
                               {"tema": tema, "query_pexels": tema}, batch_id)
        created.append(jid)
    for tema in collected.get("brasil", []):
        jid = await create_job(JobType.BRASIL_LONG, tema,
                               {"tema": tema, "query_pexels": tema, "produtos": [],
                                "product_snapshots": []}, batch_id)
        created.append(jid)
    for kw in collected.get("shopee", []):
        jid = await create_job(JobType.SHOPEE_SHORT, kw,
                               {"keyword": kw, "duracion_seconds": 30}, batch_id)
        created.append(jid)
    get_db().update_batch(batch_id, status="running", phase="research")
    await state.clear()
    await message.answer(
        f"📦 <b>Lote {batch_id}</b> criado com {len(created)} jobs.<br>"
        "Fase 1: pesquisa/roteiros. Cada roteiro pedirá aprovação individual.<br>"
        "Renderização em fila: 1 longo simultâneo + 2 reels Shopee.<br><br>"
        + "<br>".join(created)
    )


# ---------------------------------------------------------------------------
# Comandos diretos (atalhos sem menu)
# ---------------------------------------------------------------------------


@dp.message(Command("global"))
async def cmd_global(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    tema = (command.args or "").strip()
    if not tema:
        await message.answer("Uso: /global <tema>")
        return
    job_id = await create_job(JobType.GLOBAL_LONG, tema,
                              {"tema": tema, "query_pexels": tema})
    await message.answer(f"🌍 Job {job_id} criado.")


@dp.message(Command("brasil"))
async def cmd_brasil(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    tema = (command.args or "").strip()
    if not tema:
        await message.answer("Uso: /brasil <tema> (menu guiado: /novo)")
        return
    job_id = await create_job(JobType.BRASIL_LONG, tema,
                              {"tema": tema, "query_pexels": tema, "produtos": [],
                               "product_snapshots": []})
    await message.answer(f"🇧🇷 Job {job_id} criado (sem produtos — roteiro genérico técnico).")


@dp.message(Command("shopee"))
async def cmd_shopee(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    kw = (command.args or "").strip()
    if not kw:
        await message.answer("Uso: /shopee <busca>")
        return
    job_id = await create_job(JobType.SHOPEE_SHORT, kw,
                              {"keyword": kw, "duracion_seconds": 30})
    await message.answer(f"🛍️ Job {job_id} criado.")


@dp.message(Command("voz"))
async def cmd_voz(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    from engine import TTS_ENGINE as atual

    alvo = (command.args or "").strip().lower()
    if not alvo:
        await message.answer(
            f"Motor de voz atual: <b>{atual}</b>\n\n"
            "Uso: /voz edge  |  /voz piper"
        )
        return
    try:
        from engine import set_tts_engine

        novo = set_tts_engine(alvo)
        await message.answer(f"🔊 Motor de voz alterado para: <b>{novo}</b>")
    except Exception as exc:
        await message.answer(f"❌ {redact_secrets(str(exc))}")


@dp.message(Command("shopee_url"))
async def cmd_shopee_url(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    url = (command.args or "").strip()
    if not _valid_http_url(url):
        await message.answer("Uso: /shopee_url <url http(s)…>")
        return
    job_id = await create_job(JobType.SHOPEE_SHORT, url[:60],
                              {"url": url, "duracion_seconds": 30})
    await message.answer(f"🛍️ Job {job_id} criado.")


@dp.message(Command("tiktok"))
async def cmd_tiktok(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    tema = (command.args or "").strip()
    if not tema:
        await message.answer("Uso: /tiktok <tema> — ou menu guiado: /novo")
        return
    job_id = await create_job(
        JobType.TIKTOK_SHORT, tema,
        {"tema": tema, "query_pexels": tema, "with_subtitles": True},
    )
    await message.answer(f"🎵 Job {job_id} criado (TikTok ≤15s).")


@dp.message(Command("lote_semana"))
async def cmd_lote(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await batch_start_inline(message, state)


async def batch_start_inline(message: Message, state: FSMContext) -> None:
    await state.set_state(BatchFlow.kind.state)
    await message.answer("Formato do lote semanal:", reply_markup=K.batch_kinds())


# ---------------------------------------------------------------------------
# Status / jobs / preview / metadados
# ---------------------------------------------------------------------------


def _status_report(job_id: Optional[str] = None) -> str:
    db = get_db()
    mgr = get_job_manager()
    if job_id:
        job = db.get_job(job_id)
        if not job:
            return f"Job {job_id} não encontrado."
        pub = db.get_publication(job_id)
        linhas = [f"<b>{job.id}</b> [{job.type}] {job.title[:60]}",
                  f"Status: {JOB_PROGRESS_TEXT.get(job.status, job.status)}",
                  f"Criado: {job.created_at}"]
        if job.error:
            linhas.append(f"Erro: <code>{redact_secrets(job.error)}</code>")
        if job.payload.get("word_count"):
            linhas.append(f"Roteiro: {job.payload['word_count']} palavras")
        if pub:
            linhas.append(f"Publicado: {pub['url']}")
        return "\n".join(linhas)
    counts = db.count_by_status()
    active = db.active_jobs()[:10]
    resumo = " · ".join(f"{JOB_PROGRESS_TEXT.get(k, k)}: {v}" for k, v in sorted(counts.items()))
    out = [f"📊 <b>Painel</b> — fila: {mgr.queue.qsize()} pendente(s)\n{resumo}\n"]
    for j in active:
        out.append(job_line(j))
    if not active:
        out.append("<i>Nenhum job ativo.</i>")
    return "\n".join(out)


@dp.message(Command("status"))
async def cmd_status(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    await message.answer(_status_report((command.args or "").strip() or None))


@dp.message(Command("jobs"))
async def cmd_jobs(message: Message) -> None:
    if not is_admin(message):
        return
    jobs = [(j.id, JOB_PROGRESS_TEXT.get(j.status, j.status)) for j in get_db().active_jobs()]
    await message.answer("Jobs ativos:", reply_markup=K.jobs_list(jobs))


@dp.callback_query(F.data == "nav:painel")
async def cb_painel(query: CallbackQuery) -> None:
    if not await deny_unless_admin_cb(query):
        return
    await query.message.edit_text(_status_report())  # type: ignore[union-attr]
    await query.message.edit_reply_markup(reply_markup=K.painel_nav())  # type: ignore[union-attr]
    await query.answer()


@dp.callback_query(F.data == "nav:novo")
async def cb_nav_novo(query: CallbackQuery) -> None:
    if not await deny_unless_admin_cb(query):
        return
    await query.message.edit_text("O que vamos criar?", reply_markup=K.main_menu())  # type: ignore[union-attr]
    await query.answer()


@dp.callback_query(F.data == "nav:jobs")
async def cb_nav_jobs(query: CallbackQuery) -> None:
    if not await deny_unless_admin_cb(query):
        return
    jobs = [(j.id, JOB_PROGRESS_TEXT.get(j.status, j.status)) for j in get_db().active_jobs()]
    await query.message.edit_text("Jobs ativos:", reply_markup=K.jobs_list(jobs))  # type: ignore[union-attr]
    await query.answer()


@dp.callback_query(F.data.startswith("jopen:"))
async def cb_jopen(query: CallbackQuery) -> None:
    if not await deny_unless_admin_cb(query):
        return
    job_id = query.data.split(":")[1]
    await query.message.edit_text(_status_report(job_id), reply_markup=K.cancel_confirm(job_id))  # type: ignore[union-attr]
    await query.answer()


@dp.message(Command("preview"))
async def cmd_preview(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    job_id = (command.args or "").strip()
    await _send_video_of(message, job_id)


async def _send_video_of(message: Message, job_id: str) -> None:
    job = get_db().get_job(job_id)
    if not job:
        await message.answer("Job não encontrado.")
        return
    video = job.payload.get("video_path")
    if not video or not Path(video).exists():
        await message.answer("Vídeo ainda não foi renderizado.")
        return
    await bot.send_video(message.chat.id, FSInputFile(video),
                         caption=f"🎬 {job_id} — {job.title[:60]}",
                         supports_streaming=True)


@dp.callback_query(F.data.startswith("prev:"))
async def cb_prev(query: CallbackQuery) -> None:
    if not await deny_unless_admin_cb(query):
        return
    job_id = query.data.split(":")[1]
    job = get_db().get_job(job_id)
    video = job.payload.get("video_path") if job else None
    if video and Path(video).exists():
        await bot.send_video(query.message.chat.id, FSInputFile(video),  # type: ignore[union-attr]
                             caption=f"🎬 {job_id}", supports_streaming=True)
    else:
        await query.answer("Sem vídeo ainda.", show_alert=True)
    await query.answer()


def _metadata_text(job_id: str) -> str:
    job = get_db().get_job(job_id)
    if not job:
        return "Job não encontrado."
    meta = job.payload.get("metadata")
    if not meta:
        return "Metadados ainda não gerados."
    return (
        f"<b>{job_id}</b>\n\n<b>Título:</b> {meta['titulo']}\n"
        f"<b>Categoria:</b> {meta['categoria']} | <b>Idioma:</b> {meta['idioma']}\n"
        f"<b>Tags:</b> {', '.join(meta['tags'])}\n\n{meta['descricao'][:900]}"
    )


@dp.message(Command("metadados"))
async def cmd_metadados(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    await message.answer(_metadata_text((command.args or "").strip()))


@dp.callback_query(F.data.startswith("meta:"))
async def cb_meta(query: CallbackQuery) -> None:
    if not await deny_unless_admin_cb(query):
        return
    await query.message.answer(_metadata_text(query.data.split(":")[1]))  # type: ignore[union-attr]
    await query.answer()


# ---------------------------------------------------------------------------
# Aprovação / rejeição / regeneração / publicação / cancelamento
# ---------------------------------------------------------------------------


async def _approve_script_or_review(job_id: str, query_or_message_chat_id: int) -> str:
    db = get_db()
    job = db.get_job(job_id)
    if not job:
        return "Job não encontrado."
    if job.status == JobStatus.AWAITING_SCRIPT_APPROVAL.value:
        await workers.queue_render(job_id)
        return f"▶️ {job_id} aprovado — render enfileirado."
    if job.status == JobStatus.REVIEW_REQUIRED.value:
        db.update_status(job_id, JobStatus.APPROVED.value)
        return (f"👍 {job_id} aprovado para publicação.\n"
                "Publique manualmente no YouTube e registre com:\n"
                f"/publicado {job_id} <url>")
    return f"Nada para aprovar ({job.status})."


@dp.callback_query(F.data.startswith("ok:"))
async def cb_ok(query: CallbackQuery) -> None:
    if not await deny_unless_admin_cb(query):
        return
    job_id = query.data.split(":")[1]
    result = await _approve_script_or_review(job_id, query.message.chat.id)  # type: ignore[union-attr]
    await query.message.answer(result)  # type: ignore[union-attr]
    await query.answer()


@dp.message(Command("aprovar"))
async def cmd_aprovar(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    job_id = (command.args or "").strip()
    await message.answer(await _approve_script_or_review(job_id, message.chat.id))


@dp.callback_query(F.data.startswith("rej:"))
async def cb_rej(query: CallbackQuery) -> None:
    if not await deny_unless_admin_cb(query):
        return
    job_id = query.data.split(":")[1]
    get_db().update_status(job_id, JobStatus.REJECTED.value, error="rejeitado via botão")
    await query.message.answer(f"❌ {job_id} rejeitado.")  # type: ignore[union-attr]
    await query.answer()


@dp.message(Command("rejeitar"))
async def cmd_rejeitar(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    parts = (command.args or "").split(maxsplit=1)
    if not parts:
        await message.answer("Uso: /rejeitar <job_id> [motivo]")
        return
    ok = get_db().update_status(parts[0], JobStatus.REJECTED.value,
                                error=parts[1] if len(parts) > 1 else None)
    await message.answer(f"{'❌ Rejeitado' if ok else 'Transição inválida'}: {parts[0]}")


@dp.callback_query(F.data.startswith("regen:"))
async def cb_regen(query: CallbackQuery) -> None:
    if not await deny_unless_admin_cb(query):
        return
    _, job_id, what = query.data.split(":", 2)
    await workers.regenerate(job_id, what)
    await query.message.answer(f"♻️ {job_id}: regenerando {what}…")  # type: ignore[union-attr]
    await query.answer()


@dp.message(Command("regerar"))
async def cmd_regerar(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer("Uso: /regerar <job_id> <roteiro|audio|thumb|video>")
        return
    alvo = {"roteiro": "script", "audio": "audio", "thumb": "thumb", "video": "video"}[parts[1]] \
        if parts[1] in ("roteiro", "audio", "thumb", "video") else parts[1]
    await workers.regenerate(parts[0], alvo)
    await message.answer(f"♻️ {parts[0]}: regenerando {alvo}…")


@dp.message(Command("publicado"))
async def cmd_publicado(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or not _valid_http_url(parts[1].strip()):
        await message.answer("Uso: /publicado <job_id> <url>")
        return
    job_id, url = parts[0], parts[1].strip()
    db = get_db()
    job = db.get_job(job_id)
    if not job:
        await message.answer("Job não encontrado.")
        return
    if job.status not in (JobStatus.APPROVED.value, JobStatus.SCHEDULED.value):
        await message.answer(f"Só é possível registrar publicação com o job aprovado "
                             f"(status atual: {job.status}).")
        return
    db.update_status(job_id, JobStatus.PUBLISHED.value)
    db.record_publication(job_id, url)
    await message.answer(f"📤 {job_id} marcado como publicado.\n{url}")


@dp.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(query: CallbackQuery) -> None:
    if not await deny_unless_admin_cb(query):
        return
    job_id = query.data.split(":")[1]
    ok = get_db().update_status(job_id, JobStatus.CANCELLED.value)
    await query.message.answer(f"{'🚫 Cancelado' if ok else 'Não cancelável'}: {job_id}")  # type: ignore[union-attr]
    await query.answer()


@dp.message(Command("cancelar"))
async def cmd_cancelar(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    job_id = (command.args or "").strip()
    ok = get_db().update_status(job_id, JobStatus.CANCELLED.value)
    await message.answer(f"{'🚫 Cancelado' if ok else 'Não cancelável'}: {job_id}")


@dp.message(Command("retry"))
async def cmd_retry(message: Message, command: CommandObject) -> None:
    """Re-enfileira um job que falhou (ex.: cota do Gemini renovada)."""
    if not is_admin(message):
        return
    args = (command.args or "").strip()
    mgr = get_job_manager()
    if args == "all":
        retried = [j.id for j in get_db().list_jobs([JobStatus.FAILED.value], limit=50)]
        done = 0
        for jid in retried:
            if await mgr.retry_failed(jid):
                done += 1
        await message.answer(f"🔁 {done} job(s) re-enfileirado(s).")
        return
    if not args:
        await message.answer("Uso: /retry <job_id> ou /retry all")
        return
    ok = await mgr.retry_failed(args)
    await message.answer(
        f"{'🔁 Job re-enfileirado.' if ok else 'Não foi possível (job inexistente ou não está em failed).'}"
    )


@dp.message(Command("limpar_cache"))
async def cmd_limpar_cache(message: Message) -> None:
    if not is_admin(message):
        return
    from utils.files import BASE_DIR

    freed = 0
    cache_dirs = [BASE_DIR / "broll", BASE_DIR / "assets"]
    for d in cache_dirs:
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file():
                try:
                    freed += f.stat().st_size
                    f.unlink()
                except OSError:
                    pass  # arquivo em uso
    await message.answer(f"🧹 Cache limpo: {freed / 1_048_576:.1f} MB liberados "
                         "(vídeos finais em output/jobs NÃO foram tocados).")


@dp.callback_query(F.data == "noop")
async def cb_noop(query: CallbackQuery) -> None:
    await query.answer()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


async def main() -> None:
    db = get_db()
    mgr = get_job_manager()
    workers.register_stages(mgr)
    workers.set_notify_sender(send_payload)
    mgr.set_progress_callback(notify_progress)
    await mgr.start()
    resumed = await mgr.resume_pending()
    logger.info(
        "Painel pronto. Jobs retomados: %d. Limites: 1 render longo, %d reels.",
        resumed, 2,
    )
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Encerrado pelo usuário.")
