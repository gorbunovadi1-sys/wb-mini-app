"""Собирает точную копию присланного дашборда (assets/dashboard_template.html,
скопирован из скилла anthropic-skills:wb-weekly-dashboard без изменений — та
же вёрстка, CSS, фильтры, графики, раскрывающиеся строки) живыми данными по
WB API вместо ручной выгрузки "Еженедельного детализированного отчёта".

parse_report() ниже — ДОСЛОВНАЯ копия функции того же имени из
scripts/build_dashboard.py скилла (включая FACT_COLS/LOGT/ALIASES и порядок
операций) — сознательно не переписана заново, чтобы не рисковать внести
расхождение в уже проверенную на реальных данных логику разбора отчёта.
Единственное, что заменено, — источник строк: вместо pandas.read_excel(xlsx)
это live-адаптер (_rows_to_dataframe), кормящий её теми же именами колонок,
но из JSON-ответа backend/margin.fetch_rows.

Соответствие колонка отчёта → поле JSON API подтверждено эмпирически 2026-09-16
на реальных данных кабинета Сельдереевой (см. историю сессии, а не документация
WB — она не описывает эти поля явно):

    Артикул поставщика                              -> vendorCode
    Код номенклатуры                                 -> nmId
    Название                                         -> title
    Обоснование для оплаты                           -> sellerOperName
    Дата продажи                                     -> saleDt (дата, без времени)
    Вайлдберриз реализовал Товар (Пр)                -> retailAmount
    Кол-во                                           -> quantity
    Вознаграждение Вайлдберриз (ВВ), без НДС         -> vw
    НДС с Вознаграждения Вайлдберриз                 -> vwNds
    Компенсация платёжных услуг/...                  -> acquiringFee
    К перечислению Продавцу за реализованный Товар   -> forPay
    Виды доставок, штрафов и корректировок ВВ        -> bonusTypeName
    Услуги по доставке товара покупателю             -> deliveryService
    Количество доставок                              -> deliveryAmount
    Количество возврата                              -> returnAmount
    Операции на приемке                              -> paidAcceptance
    Общая сумма штрафов                              -> penalty
    Компенсация скидки по программе лояльности       -> cashbackDiscount
    Хранение                                         -> paidStorage
    Удержания                                        -> deduction

НЕ найдено поле для "Возмещение за выдачу и возврат товаров на ПВЗ" внутри
строк "Продажа" (в xlsx это отдельная колонка с обычно небольшой/нулевой
суммой) — тождество выплаты (payout = rev − comm − pvz − acq) сошлось "до
копейки" на реальных данных БЕЗ этого слагаемого, т.е. pvz=0 в наблюдаемых
строках. Оставлено как pvz=0 везде — разночтение с оригиналом на эту (обычно
мелкую) статью возможно, но не придумываю поле вместо проверки. См. checks()
ниже — она такая же сверка, как в build_dashboard.py, и покажет расхождение,
если оно появится.

Реклама (camp) заполняется через _fetch_camp_facts: get_campaign_fullstats
вызывается отдельно на каждый день периода (обычный GET с ретраями на 429,
не тот жёсткий лимит 1/мин, что у finance-api/normquery) — это даёт настоящие
дневные суммы по КАМПАНИИ. Доля на конкретный артикул внутри кампании
разнесена поровну между её номенклатурами — у WB нет API с разбивкой
расхода по артикулу день-в-день, это тот же класс приближения, что и у
самого скилла при разборе выгрузок кампаний (там делёж пропорционален
доле артикула в "Статистике" за весь период; здесь взять эту пропорцию
можно только через отдельный normquery/stats на каждую кампанию, а это
уже упирается в лимит 10 запросов/мин — сознательно не делаю, чтобы не
растягивать сборку на десятки минут). kwFacts (разбор по кластерам/фразам
внутри карточки кампании) не заполняется вовсе — это отдельная, более
дорогая по API часть; кластеры с вердиктом «Зачистить» доступны отдельно
через /ads в боте или раздел Реклама в мини-аппе."""
import datetime
import logging

import pandas as pd

from backend import margin
from backend.wb_client import nm_ids_from_campaign_detail

from . import rate_limit

log = logging.getLogger("seldereeva_bot.dashboard_builder")

TEMPLATE_PATH = __file__.replace("dashboard_builder.py", "assets/dashboard_template.html")

# ---- дословно из scripts/build_dashboard.py (не менять порядок — на него
# завязаны индексы, которые читает JS шаблона) ----
FACT_COLS = ['rev', 'qty', 'comm', 'acq', 'pvz', 'payout', 'l0', 'l1', 'l2', 'l3', 'deliv', 'ret',
             'accept', 'fine', 'loyal', 'volunt', 'ad', 'imp', 'clicks', 'cart', 'adqty', 'adsum']
NC = len(FACT_COLS)

LOGT = ['К клиенту при продаже', 'К клиенту при отмене', 'От клиента при отмене',
        'Возврат товара, который приехал по МП, продавцу (К продавцу)']

BASIS_DELIVERY = ('Доставка', 'Логистика')
BASIS_DELIVERY_FIX = ('Коррекция стоимости доставки',)
AD_WITHHOLD_MARK = 'Продвижение'

C_ART = 'Артикул поставщика'
C_NM = 'Код номенклатуры'
C_NAME = 'Название'
C_BASIS = 'Обоснование для оплаты'
C_DATE = 'Дата продажи'


def r2(x):
    try:
        return round(float(x), 2)
    except Exception:
        return 0.0


def _num(row, key):
    v = row.get(key)
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _rows_to_dataframe(raw_rows: list) -> pd.DataFrame:
    """Живые строки backend/margin.fetch_rows (те же dict, что в JSON-ответе
    WB finance-api) -> DataFrame с колонками, которые ожидает parse_report
    (см. маппинг в docstring модуля)."""
    recs = []
    for r in raw_rows:
        sale_dt = r.get("saleDt") or r.get("rrDate")
        if not sale_dt:
            continue
        recs.append({
            C_ART: r.get("vendorCode"),
            C_NM: r.get("nmId"),
            C_NAME: r.get("title"),
            C_BASIS: r.get("sellerOperName"),
            C_DATE: sale_dt[:10],
            'Вайлдберриз реализовал Товар (Пр)': _num(r, "retailAmount"),
            'Кол-во': _num(r, "quantity"),
            'Вознаграждение Вайлдберриз (ВВ), без НДС': _num(r, "vw"),
            'НДС с Вознаграждения Вайлдберриз': _num(r, "vwNds"),
            'Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов': _num(r, "acquiringFee"),
            'К перечислению Продавцу за реализованный Товар': _num(r, "forPay"),
            'Виды доставок, штрафов и корректировок ВВ': r.get("bonusTypeName"),
            'Услуги по доставке товара покупателю': _num(r, "deliveryService"),
            'Количество доставок': _num(r, "deliveryAmount"),
            'Количество возврата': _num(r, "returnAmount"),
            'Операции на приемке': _num(r, "paidAcceptance"),
            'Общая сумма штрафов': _num(r, "penalty"),
            'Компенсация скидки по программе лояльности': _num(r, "cashbackDiscount"),
            'Хранение': _num(r, "paidStorage"),
            'Удержания': _num(r, "deduction"),
        })
    return pd.DataFrame.from_records(recs)


def pick_col(df, aliases):
    for c in aliases:
        if c in df.columns:
            return c
    return None


def parse_report(df):
    """Дословная копия build_dashboard.py:parse_report — см. docstring модуля."""
    d = df.copy()
    d[C_DATE] = pd.to_datetime(d[C_DATE])
    d['__day'] = d[C_DATE].dt.strftime('%Y-%m-%d')

    days = sorted(x for x in d['__day'].dropna().unique())
    if not days:
        raise ValueError("в отчёте нет ни одной даты продажи")
    di = {v: i for i, v in enumerate(days)}

    known = d.dropna(subset=[C_ART])
    names = known.groupby(C_ART)[C_NAME].first().to_dict() if C_NAME in d.columns else {}
    nm_map = known.groupby(C_ART)[C_NM].first().to_dict() if C_NM in d.columns else {}
    skus = sorted(set(d[C_ART].dropna()))
    if not skus:
        raise ValueError("в отчёте нет ни одного артикула поставщика")
    si = {v: i for i, v in enumerate(skus)}

    F = {}

    def cell(sku, day):
        return F.setdefault((sku, day), [0.0] * NC)

    IDX = {k: i for i, k in enumerate(FACT_COLS)}

    prod = d[d[C_BASIS] == 'Продажа']
    for _, r in prod.iterrows():
        c = cell(r[C_ART], r['__day'])
        c[IDX['rev']] += float(r.get('Вайлдберриз реализовал Товар (Пр)', 0) or 0)
        c[IDX['qty']] += float(r.get('Кол-во', 0) or 0)
        c[IDX['comm']] += (float(r.get('Вознаграждение Вайлдберриз (ВВ), без НДС', 0) or 0)
                            + float(r.get('НДС с Вознаграждения Вайлдберриз', 0) or 0))
        c[IDX['acq']] += float(r.get('Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов', 0) or 0)
        c[IDX['pvz']] += float(r.get('Возмещение за выдачу и возврат товаров на ПВЗ', 0) or 0)
        c[IDX['payout']] += float(r.get('К перечислению Продавцу за реализованный Товар', 0) or 0)

    V = 'Виды доставок, штрафов и корректировок ВВ'
    if V not in d.columns:
        d[V] = None
    for _, r in d[d[C_BASIS].isin(BASIS_DELIVERY)].iterrows():
        c = cell(r[C_ART], r['__day'])
        t = r.get(V)
        if t in LOGT:
            c[IDX['l' + str(LOGT.index(t))]] += float(r.get('Услуги по доставке товара покупателю', 0) or 0)
        c[IDX['deliv']] += float(r.get('Количество доставок', 0) or 0)
        c[IDX['ret']] += float(r.get('Количество возврата', 0) or 0)

    for _, r in d[d[C_BASIS].isin(BASIS_DELIVERY_FIX)].iterrows():
        cell(r[C_ART], r['__day'])[IDX['l0']] += float(r.get('Услуги по доставке товара покупателю', 0) or 0)

    for _, r in d[d[C_BASIS] == 'Обработка товара'].iterrows():
        cell(r[C_ART], r['__day'])[IDX['accept']] += float(r.get('Операции на приемке', 0) or 0)
    for _, r in d[d[C_BASIS] == 'Штраф'].iterrows():
        cell(r[C_ART], r['__day'])[IDX['fine']] += float(r.get('Общая сумма штрафов', 0) or 0)
    for _, r in d[d[C_BASIS] == 'Компенсация скидки по программе лояльности'].iterrows():
        cell(r[C_ART], r['__day'])[IDX['loyal']] += float(r.get('Компенсация скидки по программе лояльности', 0) or 0)
    for _, r in d[d[C_BASIS] == 'Добровольная компенсация при возврате'].iterrows():
        cell(r[C_ART], r['__day'])[IDX['volunt']] += float(r.get('К перечислению Продавцу за реализованный Товар', 0) or 0)

    storage = {v: 0.0 for v in days}
    withheld = {v: 0.0 for v in days}
    other = {v: 0.0 for v in days}
    other_kinds = {}
    for _, r in d[d[C_BASIS] == 'Хранение'].iterrows():
        storage[r['__day']] += float(r.get('Хранение', 0) or 0)
    for _, r in d[d[C_BASIS] == 'Удержание'].iterrows():
        amt = float(r.get('Удержания', 0) or 0)
        kind = str(r.get(V) or '')
        if AD_WITHHOLD_MARK.lower() in kind.lower():
            withheld[r['__day']] += amt
        else:
            other[r['__day']] += amt
            key = kind.split(',')[0].strip() or 'Прочее удержание'
            other_kinds[key] = other_kinds.get(key, 0) + amt

    fine_types = {}
    sh = d[d[C_BASIS] == 'Штраф']
    if len(sh) and V in sh.columns:
        for k, v in sh.groupby(V)['Общая сумма штрафов'].sum().items():
            fine_types[str(k)] = r2(v)

    return dict(days=days, di=di, skus=skus, si=si, names=names, nm_map=nm_map,
                F=F, storage=storage, withheld=withheld, other=other,
                other_kinds={k: r2(v) for k, v in other_kinds.items()},
                fine_types=fine_types, IDX=IDX, V=V)


def guess_gender(name):
    n = str(name or '').lower()
    if 'унисекс' in n:
        return 'Унисекс'
    if 'мужск' in n:
        return 'Мужские'
    return 'Женские'


def checks(df, tot, src):
    """Та же сверка, что в build_dashboard.py — сравнивает мою сумму с суммой
    по строкам «Продажа». Расхождение больше рубля означает ошибку в
    маппинге полей, а не в данных WB."""
    prod = df[df[C_BASIS] == 'Продажа']
    src_rev = float(prod['Вайлдберриз реализовал Товар (Пр)'].sum())
    src_pay = float(prod['К перечислению Продавцу за реализованный Товар'].sum())
    src_qty = float(prod['Кол-во'].sum())
    problems = []
    for label, mine, s in (
        ('выручка', tot['rev'], src_rev),
        ('к перечислению', tot['payout'], src_pay),
        ('продано, шт', tot['qty'], src_qty),
    ):
        ok = abs(mine - s) < 1
        log.info(f"сверка: {label} — я={mine:.2f} источник={s:.2f} {'ок' if ok else 'РАСХОЖДЕНИЕ'}")
        if not ok:
            problems.append(label)
    return problems


def _fetch_camp_facts(client, days_list: list, di: dict, nm2sku: dict, si: dict, F: dict, IDX: dict) -> list:
    """Расход/показы/клики/корзина/заказы по рекламе на (кампания, артикул,
    день) — настоящие ДНЕВНЫЕ суммы по кампании: get_campaign_fullstats
    вызывается отдельно на каждый день периода (это не тот же лимит 1/мин,
    что у finance-api или normquery/stats — обычный GET с ретраями на 429).
    Доля конкретного артикула внутри дня кампании разносится ПОРОВНУ между
    её номенклатурами — у fullstats нет разбивки по артикулу вообще, только
    по кампании целиком; равный делёж — тот же класс приближения, что и у
    самого скилла при разборе выгрузок (там делёж пропорционален расходу
    по «Статистике», здесь пропорций взять неоткуда без отдельного дорогого
    по лимиту запроса normquery/stats на каждую кампанию).

    Пишет результат сразу в ДВЕ структуры, как build_dashboard.py (см. его
    build(), строки 385-391): в R['F'] — те же 6 колонок ad/imp/clicks/cart/
    adqty/adsum, что и остальные деньги (оттуда берутся KPI «Потрачено»/
    «Заработано с РК» и колонка «Реклама» в детализации — они читают
    facts, а не camp напрямую), и в отдельный camp_facts — для таблицы
    кампаний и карточки «Активных кампаний». Забыть про facts — ровно та
    ошибка, из-за которой при первой сборке КПИ показывали 0₽ при
    непустом camp."""
    cutoff = datetime.date.fromisoformat(days_list[0])
    advert_ids = client.get_active_campaign_ids(changed_since=cutoff.isoformat())
    if not advert_ids:
        return []
    details = client.get_campaign_details(advert_ids)
    nm_ids_by_campaign = {d.get("id"): nm_ids_from_campaign_detail(d) for d in details}

    camp_facts = []
    for day in days_list:
        try:
            fullstats = client.get_campaign_fullstats(advert_ids, day, day)
        except Exception:
            log.exception(f"campaign fullstats failed for {day}")
            continue
        for s in fullstats:
            cid = s.get("advertId")
            spend = s.get("sum") or 0
            if not spend:
                continue
            nm_ids = [nm for nm in nm_ids_by_campaign.get(cid, set()) if nm in nm2sku and nm2sku[nm] in si]
            if not nm_ids:
                continue
            share = 1.0 / len(nm_ids)
            for nm in nm_ids:
                sku = nm2sku[nm]
                imp, clicks = r2((s.get("views") or 0) * share), r2((s.get("clicks") or 0) * share)
                cart, oq = r2((s.get("atbs") or 0) * share), round((s.get("orders") or 0) * share, 3)
                cost, osum = r2(spend * share), r2((s.get("sum_price") or 0) * share)
                camp_facts.append([str(cid), si[sku], di[day], cost, imp, clicks, cart, oq, osum])
                c = F.setdefault((sku, day), [0.0] * NC)
                c[IDX['ad']] += cost; c[IDX['imp']] += imp; c[IDX['clicks']] += clicks
                c[IDX['cart']] += cart; c[IDX['adqty']] += oq; c[IDX['adsum']] += osum
    return camp_facts


def last_complete_report_week(weeks_back: int = 1) -> tuple:
    """WB собирает "Еженедельный детализированный отчёт" по календарным
    неделям пн-вс (подтверждено её же примером: 31 авг - 6 сент, затем
    7-13 сент, отчёт за вторую появляется 14-го) — НЕ "последние 7 дней от
    сегодня". weeks_back=1 — последняя полностью завершившаяся неделя
    (что и нужно для дашборда по понедельникам: сегодня-пн, неделя
    пн-вс закончилась вчера); weeks_back=2 — неделя перед ней, и т.д."""
    today = datetime.date.today()
    this_monday = today - datetime.timedelta(days=today.weekday())
    monday = this_monday - datetime.timedelta(weeks=weeks_back)
    sunday = monday + datetime.timedelta(days=6)
    return monday, sunday


def build_dashboard_html(client, cost_prices: dict, title: str, weeks_back: int = 1) -> tuple:
    """Возвращает (html: str, problems: list[str]). Держит rate_limit.wb_finance_lock
    на всё время сборки — см. его docstring: WB throttles the finance API
    per account, not per WBClient, so two of these running at once fight
    over the same 1-req/min budget instead of queueing."""
    with rate_limit.wb_finance_lock:
        return _build_dashboard_html_locked(client, cost_prices, title, weeks_back)


def _build_dashboard_html_locked(client, cost_prices: dict, title: str, weeks_back: int = 1) -> tuple:
    cutoff, d_to = last_complete_report_week(weeks_back)
    fetch_from = cutoff - datetime.timedelta(days=7)  # запас, как в margin.py

    raw_rows = margin.fetch_rows(client, fetch_from, d_to)
    raw_rows = [r for r in raw_rows if r.get("saleDt") and cutoff.isoformat() <= r["saleDt"][:10] <= d_to.isoformat()]
    if not raw_rows:
        raise ValueError(f"нет строк отчёта за {cutoff}—{d_to}")

    df = _rows_to_dataframe(raw_rows)
    R = parse_report(df)
    days_list, si, di, IDX = R['days'], R['si'], R['di'], R['IDX']

    skus_out, no_cost = [], []
    for s in R['skus']:
        nm = R['nm_map'].get(s)
        nm_str = str(int(nm)) if nm else None
        cost = cost_prices.get(nm_str) if nm_str else None
        if cost is None:
            no_cost.append(s)
        skus_out.append(dict(sku=s, name=R['names'].get(s, ''),
                              nm=int(nm) if nm else 0,
                              cost=cost, cat='Без категории',
                              gen=guess_gender(R['names'].get(s, ''))))

    nm2sku = {int(v): k for k, v in R['nm_map'].items() if pd.notna(v)}
    try:
        # Мутирует R['F'] на месте (пишет ad/imp/clicks/cart/adqty/adsum в те
        # же ячейки, что и остальные деньги) — должно отработать ДО того, как
        # facts соберутся из R['F'] ниже, иначе реклама останется 0 в KPI и
        # в детализации, даже если camp сам по себе не пуст.
        camp_facts = _fetch_camp_facts(client, days_list, di, nm2sku, si, R['F'], IDX)
    except Exception:
        log.exception("Не удалось получить рекламные данные по дням — блок «Реклама» будет пустым")
        camp_facts = []

    facts = []
    for (sku, day), c in R['F'].items():
        if any(abs(x) > 1e-9 for x in c):
            facts.append([si[sku], di[day]] + [r2(x) for x in c])
    facts.sort()

    tot = {k: 0.0 for k in FACT_COLS}
    for f in facts:
        for j, k in enumerate(FACT_COLS):
            tot[k] += f[2 + j]
    problems = checks(df, tot, None)

    has_cost = any(r.get('cost') is not None for r in skus_out)
    cost_note = ("Себестоимость не указана — прибыль и маржа не считаются." if not has_cost else
                 f"Себестоимость известна для {len(skus_out) - len(no_cost)} из {len(skus_out)} товаров.")

    ad_note = (
        "Расход на рекламу — реальные дневные суммы по кампании (get_campaign_fullstats на каждый день); "
        "доля конкретного артикула внутри кампании разнесена ПОРОВНУ между её номенклатурами — у WB нет "
        "API с разбивкой расхода по артикулу день-в-день, это приближение того же типа, что и у самого "
        "скилла при разборе выгрузок кампаний. Кластеры/фразы по-прежнему доступны отдельно — /ads в боте "
        "или раздел Реклама в мини-аппе (там нет ограничения на дневную разбивку)."
        if camp_facts else
        "Блок «Реклама» пуст — не удалось получить дневные данные по кампаниям (см. логи). "
        "Кластеры/фразы всё ещё доступны отдельно — /ads в боте."
    )

    data = dict(
        days=days_list, logTypes=LOGT, skus=skus_out, facts=facts,
        storage=[r2(R['storage'][d]) for d in days_list],
        withheld=[r2(R['withheld'][d]) for d in days_list],
        other=[r2(R['other'][d]) for d in days_list],
        otherKinds=R['other_kinds'],
        kw=[], fineTypes=R['fine_types'], camp=camp_facts,
        meta=dict(title=title, rows=len(raw_rows), hasCost=has_cost,
                  otherKinds=R['other_kinds'],
                  source=f"Источник: живые данные WB API, {len(raw_rows)} строк, {cutoff}—{d_to}. {ad_note}",
                  costNote=cost_note))

    import json
    tpl = open(TEMPLATE_PATH, encoding='utf-8').read()
    if tpl.count('__DATA__') != 1:
        raise RuntimeError("шаблон повреждён: должно быть ровно одно вхождение __DATA__")
    body = tpl.replace('__DATA__', json.dumps(data, ensure_ascii=False, separators=(',', ':')))
    html = ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<style>body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n'
            '</head>\n<body>\n' + body + '\n</body>\n</html>\n')
    return html, problems
