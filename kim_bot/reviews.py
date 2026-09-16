"""WB review auto-reply: drafts a reply with Claude for every new
unanswered review, and only posts it to WB once she approves it in the bot
(see project decision: recommend+confirm, not autopost)."""
import datetime
import logging

import anthropic

from . import config
from .db import SessionLocal
from .models import ReviewDraft

log = logging.getLogger("kim_bot.reviews")

_SYSTEM_PROMPT = (
    "Ты отвечаешь от лица бренда Chess Masters (магнитные шахматы, шашки, нарды) на отзывы покупателей "
    "на Wildberries. Пиши по-русски, коротко (2-4 предложения), тепло и естественно, без канцелярита и "
    "без шаблонных фраз под копирку. Обращайся к сути конкретного отзыва, а не общими словами. "
    "Если оценка низкая (1-3 из 5) или в отзыве жалоба/проблема — извинись по существу и предложи "
    "написать в чат с продавцом на WB, чтобы решить вопрос. Не подписывайся в конце."
)


def _build_client():
    if not config.ANTHROPIC_API_KEY:
        return None
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def generate_reply(review: dict) -> str:
    client = _build_client()
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY не задан")
    user_text = (
        f"Товар: {review.get('product_name') or '—'}\n"
        f"Оценка: {review.get('rating')}/5\n"
        f"Текст отзыва: {review.get('review_text') or '(без текста, только оценка)'}"
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
    )
    return resp.content[0].text.strip()


def sync_reviews(client) -> list[dict]:
    """Fetches unanswered WB reviews, creates a ReviewDraft (with an
    AI-drafted reply, if ANTHROPIC_API_KEY is set) for any not already
    tracked. Returns the newly-created drafts for the caller to send for
    approval — never posts anything itself."""
    raw = client.get_unanswered_reviews()
    new_drafts = []
    with SessionLocal() as db:
        for r in raw:
            review_id = r.get("id")
            if not review_id or db.query(ReviewDraft).filter_by(review_id=review_id).first():
                continue
            product = r.get("productDetails") or {}
            fields = dict(
                review_id=review_id,
                article=product.get("supplierArticle"),
                product_name=product.get("productName"),
                rating=r.get("productValuation"),
                review_text=r.get("text") or "",
                author_name=r.get("userName"),
            )
            try:
                draft_reply = generate_reply(fields)
            except Exception:
                log.exception(f"Failed to generate reply for review {review_id}")
                draft_reply = None
            row = ReviewDraft(**fields, draft_reply=draft_reply, status="pending")
            db.add(row)
            db.commit()
            new_drafts.append({**fields, "draft_reply": draft_reply, "db_id": row.id})
    return new_drafts


def get_draft(db_id: int) -> ReviewDraft:
    with SessionLocal() as db:
        return db.query(ReviewDraft).filter_by(id=db_id).first()


def approve_and_post(db_id: int, client, text: str = None) -> bool:
    """Posts the (possibly edited) reply to WB — the only place this module
    actually calls post_review_answer. Returns False if the draft is gone
    or already decided (e.g. a stale button press)."""
    with SessionLocal() as db:
        row = db.query(ReviewDraft).filter_by(id=db_id).first()
        if not row or row.status != "pending":
            return False
        final_text = text or row.draft_reply
        if not final_text:
            return False
        client.post_review_answer(row.review_id, final_text)
        row.draft_reply = final_text
        row.status = "posted"
        row.decided_at = datetime.datetime.utcnow()
        db.commit()
    return True


def skip(db_id: int) -> bool:
    with SessionLocal() as db:
        row = db.query(ReviewDraft).filter_by(id=db_id).first()
        if not row or row.status != "pending":
            return False
        row.status = "skipped"
        row.decided_at = datetime.datetime.utcnow()
        db.commit()
    return True


def set_draft_text(db_id: int, text: str):
    with SessionLocal() as db:
        row = db.query(ReviewDraft).filter_by(id=db_id).first()
        if row:
            row.draft_reply = text
            db.commit()
