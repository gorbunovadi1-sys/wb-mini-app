import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, UniqueConstraint

from .db import Base


class SeldCostPrice(Base):
    """Себестоимость за единицу товара, ключ — nmId (WB) или offer_id (Ozon)
    как строка. Отдельная от backend/cost_prices.py таблица намеренно: та —
    один общий JSON-файл для легаси однокабинетного дашборда, использовать
    его здесь означало бы смешать себестоимость Сельдереевой с чужими
    данными. См. seldereeva_bot/cost_prices.py."""
    __tablename__ = "seld_cost_price"
    __table_args__ = (UniqueConstraint("marketplace", "item_key", name="uq_seld_cost_price"),)

    id = Column(Integer, primary_key=True)
    marketplace = Column(String, nullable=False)  # "wb" | "ozon"
    item_key = Column(String, nullable=False)
    cost_price = Column(Float, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class SeldActionLog(Base):
    """Единая точка учёта для любого управляющего действия над рекламой
    (зачистка фразы, пауза кампании, изменение ставки) — создаётся как
    pending из мини-аппа или чата, исполняется только после подтверждения.
    Два входа (мини-апп/чат) пишут в одну и ту же таблицу и используют одну
    функцию исполнения — см. seldereeva_bot/actions.py."""
    __tablename__ = "seld_action_log"

    id = Column(Integer, primary_key=True)
    action_type = Column(String, nullable=False)  # "exclude_phrase" | "pause_campaign" | "set_bid"
    marketplace = Column(String, nullable=False, default="wb")
    target = Column(String, nullable=False)  # human-readable: campaign id / norm_query / etc.
    payload = Column(Text, nullable=True)  # JSON-encoded action params
    status = Column(String, nullable=False, default="pending")  # pending | confirmed | executed | failed | cancelled
    requested_by = Column(String, nullable=True)  # "miniapp" | "chat"
    requested_at = Column(DateTime, default=datetime.datetime.utcnow)
    confirmed_at = Column(DateTime, nullable=True)
    executed_at = Column(DateTime, nullable=True)
    result = Column(Text, nullable=True)


class SeldKizWithdrawn(Base):
    """Дедуп для ежедневной выгрузки КИЗ на вывод — один код не должен
    попасть в файл дважды. См. открытые вопросы в плане: сама выгрузка ещё
    не реализована (нет источника КИЗ по отправке Ozon и нет реального
    шаблона Честного Знака), таблица здесь — задел на потом."""
    __tablename__ = "seld_kiz_withdrawn"

    id = Column(Integer, primary_key=True)
    kiz_code = Column(String, nullable=False, unique=True)
    posting_number = Column(String, nullable=True)
    sale_price = Column(Float, nullable=True)
    withdrawn_at = Column(DateTime, default=datetime.datetime.utcnow)
    returned_at = Column(DateTime, nullable=True)  # set when it comes back via "вернуть в оборот"
