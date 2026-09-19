"""Помощник по отзывам (Feature C, событийная часть) — НЕ реализовано.

Ни в backend/, ни в kim_bot/ нет ни одного метода чтения отзывов WB/Ozon
(проверено по всему репозиторию) — это полностью новая интеграция:

1. Новые методы WBClient/OzonClient для получения отзывов (WB: Feedbacks
   API; Ozon: /v1/review/list — оба нужно сверить с актуальной документацией
   на момент реализации, здесь не угадываются).
2. Черновик ответа — единственное по-настоящему LLM-уместное место во всей
   Feature C (сам текст ответа, а не цифры отчёта) — через anthropic client,
   по образцу существующего использования в backend/ai_engine_app.py.
3. Ничего не публикуется без явного подтверждения кнопкой в чате — см.
   confirm-before-execute паттерн в actions.py.
"""
import logging

log = logging.getLogger("seldereeva_bot.reviews")


class ReviewsNotConfigured(Exception):
    pass


def poll_new_reviews():
    raise ReviewsNotConfigured("Работа с отзывами ещё не реализована — нужна новая интеграция с WB/Ozon Reviews API (см. план).")
