##### app.py #####
# 1) Lê variáveis do ficheiro .env
# 2) Liga à base de dados PostgreSQL
# 3) Executa as queries do report diário
# 4) Monta os blocos visuais (cards) para o relatório
# 5) Renderiza HTML com template + CSS
# 6) Gera PDF
# 7) Envia e-mail com o PDF em anexo
##################
from __future__ import annotations

import os
import smtplib
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as dt_time
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from calendar_helpers import TZ, is_operational_day, previous_operational_day, is_operational_date

from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader
import psycopg2
from psycopg2.extras import RealDictCursor
from playwright.sync_api import sync_playwright

from queries import (
    QUERY_HAS_REPORT_ACTIVITY,
    QUERY_TEMPO_PRODUCAO_MD,
    QUERY_HORAS_MOINHOS,
    QUERY_KGS_SILOS,
    QUERY_OEE,
    QUERY_TRIT_TOTAL_SILOS_8H,
    QUERY_DESINF_TRIT_KGS_SILOS_DIA_ANTERIOR,
    QUERY_DESINF_TRIT_TOTAL_SILOS_8H,
    QUERY_CALIB_GRANULADO_DIA_ANTERIOR,
    QUERY_DESINF_VINC_DESINFECOES_DIA_ANTERIOR,
    QUERY_CALIB_OEE_TABELA_DIA_ANTERIOR,
    QUERY_DESINF_TRIT_DIA_ANTERIOR,
    QUERY_DESINF_VINC_8H,
)

# Configuração base do projeto
# Carrega o ficheiro .env que está na mesma pasta do app.py
load_dotenv(Path(__file__).with_name(".env"))

# Pasta base do projeto
BASE_DIR = Path(__file__).resolve().parent

# Pasta onde vamos guardar os reports gerados
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)
REPORT_KEEP_COUNT = int(
    os.environ.get("REPORT_KEEP_COUNT", "10")
)

# Pasta dos templates HTML
TEMPLATES_DIR = BASE_DIR / "templates"

# Pasta do CSS estático
STATIC_DIR = BASE_DIR / "static"

# Modelos de dados do relatório
@dataclass
class MetricCard:
    """
    Representa um cartão individual do dashboard/report.
    Exemplo:
        label = "T1(00-08)"
        value = "05h33 (49%)"
    """
    label: str
    value: str
    bg_color: str = "#d9d9e3"
    text_color: str = "#111111"

@dataclass
class MetricBlock:
    """
    Representa um bloco completo do report.
    Cada bloco tem um título e 4 cartões:
        T1, T2, T3 e TOTAL
    """
    key: str
    title: str
    cards: list[MetricCard]

@dataclass
class ReportTableBlock:
    """
    Modelo para tabelas com várias linhas
    """
    key: str
    title: str
    headers: list[str]
    rows: list[dict[str, object]]

@dataclass
class ReportSection:
    """
    Uma secção agrupa vários blocos visuais.
    Pode conter:
    - MetricBlock: blocos de cartões
    - ReportTableBlock: tabelas com várias linhas
    """
    title:str
    blocks: list[MetricBlock | ReportTableBlock]

# Regras visuais / cores
# Cores para OEE Calibração
def get_oee_calib_style(value: float) -> str:
    """
    Thresholds Grafana:
    - < 70  -> vermelho
    - >=70  -> amarelo
    - >=80  -> verde
    """
    if value >= 80:
        return "ok"
    if value >= 70:
        return "warning"
    return "bad"

#Cores para tempo trabalho sem granulado
def get_tempo_sem_granulado_style(seconds: float) -> str:
    """
    Thresholds Grafana:
    - < 3600s  -> verde
    - >=3600s  -> amarelo
    - >=5400s  -> vermelho
    """
    if seconds >= 5400:
        return "bad"
    if seconds >= 3600:
        return "warning"
    return "ok"

# Cores para OEE Trituração
def get_oee_colors(value: float) -> tuple[str, str]:
    """
    Devolve as cores de fundo e texto para OEE,
    de forma semelhante aos thresholds do Grafana.

    Regras:
    - < 70   -> vermelho
    - < 80   -> amarelo
    - >= 80  -> verde
    """
    if value < 70:
        return "#f2495c", "#ffffff"
    if value < 80:
        return "#eab839", "#111111"
    return "#73bf69", "#ffffff"

def default_card_style(is_total: bool) -> tuple[str, str]:
    """
    Estilo por defeito dos cartões.
    - cartões normais: fundo claro
    - TOTAL: fundo cinzento escuro
    """
    if is_total:
        return "#6b6b6b", "#ffffff"
    return "#d9d9e3", "#111111"

# Regras de data do relatório
def get_previous_calendar_day(now: datetime) -> date:
    """
    O relatório de cada execução corresponde sempre
    ao dia civil anterior.
    """
    return now.date() - timedelta(days=1)

# def get_report_date() -> datetime:
#     """
#     Define a data de referência do relatório.

#     Em vez de assumir:
#     - segunda -> sexta
#     - outros dias -> ontem

#     passa a procurar o último dia operacional anterior,
#     ignorando:
#     - domingos
#     - feriados/férias definidos no .env
#     """
#     now = datetime.now(TZ)
#     report_day = previous_operational_day(now)

#     return datetime.combine(report_day, dt_time(0, 0), tzinfo=TZ)

def get_report_ready_at(report_day: date) -> datetime:
    """
    Hora a partir da qual o relatório pode ser gerado.

    O turno mais tardio termina às 23:59 do dia atual.
    Por defeito esperamos até às 08:15.
    """
    ready_hour = int(
        os.environ.get("REPORT_READY_HOUR", "8")
    )

    ready_minute = int(
        os.environ.get("REPORT_READY_MINUTE", "15")
    )

    return datetime.combine(
        report_day + timedelta(days=1),
        dt_time(ready_hour, ready_minute),
        tzinfo=TZ,
    )

def is_report_ready(now: datetime, report_day: date,) -> bool:
    return now >= get_report_ready_at(report_day)

# def get_today_local_date() -> str:
#     """
#     Devolve a data local de hoje em formato dd/mm/YYYY.
#     """
#     return datetime.now(TZ).strftime("%d/%m/%Y")

# Ligação à base de dados
def get_db_connection():
    """
    Abre uma ligação de leitura para todo o relatório.
    """
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        cursor_factory=RealDictCursor,
        connect_timeout=int(
            os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "15")
        ),
        application_name="csp4_daily_report",
        options=(
            "-c statement_timeout="
            + os.environ.get(
                "DB_STATEMENT_TIMEOUT_MS",
                "120000",
            )
        ),
    )

    # Cada SELECT é independente.
    # Uma falha não deixa a ligação numa transação abortada.
    conn.autocommit = True

    return conn
# def get_db_connection():
#     """
#     Abre ligação à base de dados PostgreSQL usando as
#     variáveis definidas no .env.
#     """
#     return psycopg2.connect(
#         host=os.environ["DB_HOST"],
#         port=int(os.environ.get("DB_PORT", "5432")),
#         dbname=os.environ["DB_NAME"],
#         user=os.environ["DB_USER"],
#         password=os.environ["DB_PASSWORD"],
#         cursor_factory=RealDictCursor,
#     )

#  Formatação de valores dentro das células
# Passar de segundos para formato horas:minutos
def format_seconds_hhmmss(value: object) -> str:
    """
    Converte segundos para HH:MM:SS.
    """
    seconds = int(round(float(value or 0)))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def format_kg(value: object) -> str:
    """
    Formata um valor de kg sem casas decimais.
    Ex.: 10425.0 -> '10425 kg'
    """
    if value is None:
        return "0 kg"
    return f"{int(round(float(value))):,}".replace(",", " ") + " kg"

def format_pct(value: object) -> str:
    """
    Formata um valor percentual com 1 casa decimal.
    Ex.: 73.6 -> '73.6 %'
    """
    if value is None:
        return "0 %"
    return f"{float(value):.1f} %"

def execute_query(conn, query_name: str, query: str, params: dict | None, fetch_mode: str,):
    """
    Executa uma query com retry simples e identifica
    claramente qual query falhou.
    """
    max_attempts = int(
        os.environ.get("REPORT_QUERY_ATTEMPTS", "2")
    )

    retry_seconds = int(
        os.environ.get("REPORT_QUERY_RETRY_SECONDS", "5")
    )

    for attempt in range(1, max_attempts + 1):
        try:
            with conn.cursor() as cur:
                cur.execute(query, params or {})

                if fetch_mode == "one":
                    return cur.fetchone()

                if fetch_mode == "all":
                    return cur.fetchall()

                raise ValueError(
                    f"fetch_mode inválido: {fetch_mode}"
                )

        except psycopg2.Error as exc:
            print(
                f"[ERRO SQL] {query_name} falhou "
                f"na tentativa {attempt}/{max_attempts}: "
                f"{exc}",
                flush=True,
            )

            if attempt >= max_attempts:
                raise RuntimeError(
                    f"Query {query_name!r} falhou "
                    f"após {max_attempts} tentativas."
                ) from exc

            time.sleep(retry_seconds)

    raise RuntimeError(
        f"Query {query_name!r} terminou sem resultado."
    )

# Execução genérica de queries
def run_single_row_query(conn, query_name: str, query: str, params: dict | None = None) -> dict[str, object]:
    """
    Executa uma query que deve devolver apenas UMA linha,
    com colunas do tipo:
        T1(00-08), T2(08-16), T3(16-24), TOTAL

    Exemplo de saída:
        {
            "T1(00-08)": "05h33 (49%)",
            "T2(08-16)": "00h45 (36%)",
            "T3(16-24)": "05h05 (28%)",
            "TOTAL": "11h24 (39%)",
        }
    """
    row = execute_query(conn=conn,query_name=query_name,query=query,params=params,fetch_mode="one",)

    # with get_db_connection() as conn:
    #     with conn.cursor() as cur:
    #         if params:
    #             cur.execute(query, params)
    #         else:
    #             cur.execute(query)
    #         row = cur.fetchone()

    if not row:
        #raise RuntimeError("A query não devolveu resultados.")
        raise RuntimeError(f"A query {query_name!r} não devolveu resultados.")

    ordered_labels = ["T1(00-08)", "T2(08-16)", "T3(16-24)", "TOTAL"]
    return {label: row.get(label) for label in ordered_labels}

# Execução genérica de queries com várias linhas
def run_multi_row_query(conn, query_name: str,query: str, params: dict | None = None) -> list[dict[str, object]]:
    # with get_db_connection() as conn:
    #     with conn.cursor() as cur:
    #         if params:
    #             cur.execute(query, params)
    #         else:
    #             cur.execute(query)
    #         rows = cur.fetchall()
    rows = execute_query(conn=conn,query_name=query_name,query=query,params=params,fetch_mode="all",)

    if not rows:
        # raise RuntimeError("A query multi-linha não devolveu resultados.")
        raise RuntimeError(f"A query {query_name!r} não devolveu resultados.")

    return [dict(row) for row in rows]

def has_production_for_date(conn, report_day: date) -> bool:
    """
    Deteta se existiu produção num sábado ou feriado.

    São considerados:
    - kg da Trituração;
    - kg da Desinfeção Trituração;
    - kg da Calibração.

    O limiar mínimo pode ser configurado através de:
    EXCEPTIONAL_DAY_MIN_KG
    """
    params = {
        "report_date": report_day,
    }

    min_kg = float(
        os.environ.get("EXCEPTIONAL_DAY_MIN_KG", "10")
    )

    row = run_single_vinc_row_query(
        conn=conn,
        query_name="has_report_activity",
        query=QUERY_HAS_REPORT_ACTIVITY,
        params={
            "report_date": report_day,
            "min_activity_kg": min_kg,
        },
    )

    has_production = bool(row.get("has_production", False))

    print(f"[INFO] Atividade em "f"{report_day:%d/%m/%Y}: "f"{has_production}",flush=True,)

    return has_production

    # # Produção da Trituração
    # trituracao = run_single_row_query(
    #     QUERY_KGS_SILOS,
    #     params,
    # )

    # # Produção da Desinfeção Trituração
    # desinfecao = run_single_row_query(
    #     QUERY_DESINF_TRIT_KGS_SILOS_DIA_ANTERIOR,
    #     params,
    # )

    # # Produção da Calibração
    # try:
    #     calibracao_rows = run_multi_row_query(
    #         QUERY_CALIB_GRANULADO_DIA_ANTERIOR,
    #         params,
    #     )
    # except RuntimeError:
    #     calibracao_rows = []

    # calibracao_total = next(
    #     (
    #         float(row.get("Total (Kg)", 0) or 0)
    #         for row in calibracao_rows
    #         if str(row.get("produto", "")).strip().lower()
    #         == "total"
    #     ),
    #     0.0,
    # )

    # total_kg = (
    #     float(trituracao.get("TOTAL", 0) or 0)
    #     + float(desinfecao.get("TOTAL", 0) or 0)
    #     + calibracao_total
    # )

    # print(
    #     f"Produção detetada em {report_day:%d/%m/%Y}: "
    #     f"{total_kg:.0f} kg "
    #     f"(limiar {min_kg:.0f} kg)."
    # )

    # return total_kg >= min_kg

def get_report_days_to_send(conn, now: datetime) -> list[date]:
    """
    Nesta execução só pode ser enviado um relatório:
    o do dia civil anterior.

    Regras:
    - dia útil normal: envia;
    - sábado/domingo/HOLIDAYS: envia apenas se houve atividade.
    """
    # standard_day = previous_operational_day(now)
    # report_days = [standard_day]

    report_day = get_previous_calendar_day(now)

    if is_operational_date(report_day):
        print(f"[INFO] {report_day:%d/%m/%Y} ""é um dia operacional normal.",flush=True,)

        return [report_day]

    print(f"[INFO] {report_day:%d/%m/%Y} ""é fim de semana ou HOLIDAY. ""A verificar produção.",flush=True,)

    if has_production_for_date(conn, report_day,):
        return [report_day]

    return []

    # # Começa no dia seguinte ao último dia útil normal.
    # candidate = standard_day + timedelta(days=1)

    # # Percorre os dias até ao dia atual, sem incluir hoje.
    # while candidate < now.date():
    #     candidate_dt = datetime.combine(
    #         candidate,
    #         dt_time(0, 0),
    #         tzinfo=TZ,
    #     )

    #     # Ignora domingo.
    #     # Verifica sábado, feriados ou dias de férias.
    #     is_exceptional_candidate = (
    #         candidate.weekday() != 6
    #         and not is_operational_day(candidate_dt)
    #     )

    #     if (
    #         is_exceptional_candidate
    #         and has_production_for_date(candidate)
    #     ):
    #         report_days.append(candidate)

    #     candidate += timedelta(days=1)

    # # Remove possíveis duplicados e ordena cronologicamente.
    # return sorted(set(report_days))

# Construção dos blocos do relatório
def build_standard_block(key: str, title: str, values: dict[str, object]) -> MetricBlock:
    """
    Constrói um bloco 'normal' do relatório:
    - Tempo Produção MD
    - Horas Trabalhadas
    - Kgs Produzidos

    Estes blocos usam cartões claros e TOTAL em cinzento.
    """
    cards: list[MetricCard] = []

    for label in ["T1(00-08)", "T2(08-16)", "T3(16-24)", "TOTAL"]:
        bg_color, text_color = default_card_style(label == "TOTAL")

        cards.append(
            MetricCard(
                label=label,
                value=str(values.get(label, "")),
                bg_color=bg_color,
                text_color=text_color,
            )
        )

    return MetricBlock(
        key=key,
        title=title,
        cards=cards,
    )

def build_kg_block(key: str, title: str, values: dict[str, object]) -> MetricBlock:
    cards: list[MetricCard] = []

    for label in ["T1(00-08)", "T2(08-16)", "T3(16-24)", "TOTAL"]:
        bg_color, text_color = default_card_style(label == "TOTAL")

        cards.append(
            MetricCard(
                label=label,
                value=format_kg(values.get(label, 0)),
                bg_color=bg_color,
                text_color=text_color,
            )
        )

    return MetricBlock(
        key=key,
        title=title,
        cards=cards,
    )

# Builder da tabela de Calibração
def build_calibracao_granulado_block(rows: list[dict[str, object]]) -> ReportTableBlock:
    formatted_rows: list[dict[str, object]] = []

    for row in rows:
        produto = str(row.get("produto", ""))

        formatted_rows.append({
            "Produto": produto,
            "T1(22-06)": format_kg(row.get("T1(22-06)", 0)),
            "T2(06-14)": format_kg(row.get("T2(06-14)", 0)),
            "T3(14-22)": format_kg(row.get("T3(14-22)", 0)),
            "Total (Kg)": format_kg(row.get("Total (Kg)", 0)),
            "%": "" if produto == "Total" else format_pct(row.get("Percentagem", 0)),
        })

    return ReportTableBlock(
        key="calibracao_granulado_dia_anterior",
        title="Total de Granulado Produzido - Dia Anterior",
        headers=[
            "Produto",
            "T1(22-06)",
            "T2(06-14)",
            "T3(14-22)",
            "Total (Kg)",
            "%",
        ],
        rows=formatted_rows,
    )

# Builder da tabela OEE Calibração - Dia Anterior
def build_calibracao_oee_block(rows: list[dict[str, object]]) -> ReportTableBlock:
    """
    Constrói a tabela de Performance / Disponibilidade / OEE /
    Tempo Trabalho sem granulado.

    A query já devolve as 4 linhas da tabela.
    Aqui só formatamos valores e aplicamos cores.
    """
    headers = [
        "Indicador",
        "T1(22-06)",
        "T2(06-14)",
        "T3(14-22)",
        "Dia",
    ]

    formatted_rows: list[dict[str, object]] = []

    for row in rows:
        indicador = str(row.get("Indicador", ""))
        formatted_row: dict[str, object] = {
            "Indicador": indicador,
            "_styles": {},
        }

        for col in ["T1(22-06)", "T2(06-14)", "T3(14-22)", "Dia"]:
            raw_value = float(row.get(col, 0) or 0)

            # if indicador == "Tempo Trabalho sem granulado":
            if indicador in ["Tempo Trabalho sem granulado", "Tempo Secção Desligada"]:
                formatted_row[col] = format_seconds_hhmmss(raw_value)
                formatted_row["_styles"][col] = get_tempo_sem_granulado_style(raw_value)
            else:
                formatted_row[col] = format_pct(raw_value)

                if indicador == "OEE":
                    formatted_row["_styles"][col] = get_oee_calib_style(raw_value)

        formatted_rows.append(formatted_row)

    return ReportTableBlock(
        key="calibracao_oee_dia_anterior",
        title="Cálculo OEE",
        headers=headers,
        rows=formatted_rows,
    )

# Builder da tabela Desinfeção VINC
def build_desinf_vinc_desinfecoes_block(rows: list[dict[str, object]]) -> ReportTableBlock:
    formatted_rows: list[dict[str, object]] = []

    for row in rows:
        vapex = str(row.get("VAPEX", ""))
        is_total_row = vapex == "TOTAL"

        t1 = int(row.get("T1(00-08)", 0) or 0)
        t2 = int(row.get("T2(08-16)", 0) or 0)
        t3 = int(row.get("T3(16-24)", 0) or 0)
        total = int(row.get("Total", 0) or 0)

        formatted_rows.append({
            "VAPEX": vapex,
            "T1(00-08)": t1,
            "T2(08-16)": t2,
            "T3(16-24)": t3,
            "Total": total,

            # estilos por célula
            "_styles": {} if is_total_row else {
                "T1(00-08)": "ok" if t1 >= 4 else "bad",
                "T2(08-16)": "ok" if t2 >= 4 else "bad",
                "T3(16-24)": "ok" if t3 >= 4 else "bad",
            }
        })

    return ReportTableBlock(
        key="desinf_vinc_desinfecoes_dia_anterior",
        title="Dia Anterior - Nº Desinfeções",
        headers=[
            "VAPEX",
            "T1(00-08)",
            "T2(08-16)",
            "T3(16-24)",
            "Total",
        ],
        rows=formatted_rows,
    )

# Builder de Desinfeção Trituração - Dia Anterior
def build_desinf_trit_desinfecoes_block(rows: list[dict[str, object]]) -> ReportTableBlock:
    formatted_rows: list[dict[str, object]] = []

    for row in rows:
        vapex = str(row.get("VAPEX", ""))
        is_total_row = vapex == "TOTAL"

        t1 = int(row.get("T1(00-08)", 0) or 0)
        t2 = int(row.get("T2(08-16)", 0) or 0)
        t3 = int(row.get("T3(16-24)", 0) or 0)
        total = int(row.get("Total", 0) or 0)

        formatted_rows.append({
            "VAPEX": vapex,
            "T1(00-08)": t1,
            "T2(08-16)": t2,
            "T3(16-24)": t3,
            "Total": total,
            "_styles": {} if is_total_row else {
                "T1(00-08)": "ok" if t1 >= 4 else "bad",
                "T2(08-16)": "ok" if t2 >= 4 else "bad",
                "T3(16-24)": "ok" if t3 >= 4 else "bad",
            }
        })

    return ReportTableBlock(
        key="desinf_trit_desinfecoes_dia_anterior",
        title="Nº Desinfeções",
        headers=[
            "VAPEX",
            "T1(00-08)",
            "T2(08-16)",
            "T3(16-24)",
            "Total",
        ],
        rows=formatted_rows,
    )

# Builder Desinfeção VINC - Total Silos às 8h
def build_desinf_vinc_silos_8h_block(
        values: dict[str, object],
        snapshot_label: str,
) -> ReportTableBlock:
    headers = [
        "SILO 1: 3A7 CS",
        "SILO 2: 2A3 CS",
        "SILO 3: 1A2 CS",
        "SILO 4: 05A1 CS",
        "SILO 5: 1A2 VINC",
        "SILO 6: 05A1 VINC",
    ]

    row = {
        "_styles": {},
        "_header_styles": {
            "SILO 1: 3A7 CS": "green",
            "SILO 2: 2A3 CS": "green",
            "SILO 3: 1A2 CS": "green",
            "SILO 4: 05A1 CS": "green",
            "SILO 5: 1A2 VINC": "blue",
            "SILO 6: 05A1 VINC": "blue",
        },
    }

    for header in headers:
        row[header] = format_kg(values.get(header, 0))

    return ReportTableBlock(
        key="desinf_vinc_silos_8h",
        #title=f"Peso Silos Desinfeção VINC às 8h ({get_today_local_date()})",
        title=(
            "Peso Silos Desinfeção VINC às 8h "
            f"({snapshot_label})"
        ),
        headers=headers,
        rows=[row],
    )

# Builder de blocos com um único cartão grande (ex: Total Silos às 8h)
def run_scalar_query(conn, query_name: str, query: str, params: dict | None = None) -> object:
    """
    Executa uma query que devolve uma única linha e uma única coluna.
    Exemplo:
        SELECT 123 AS "TOTAL"
    """

    row = execute_query(conn=conn,query_name=query_name,query=query,params=params,fetch_mode="one",)

    # with get_db_connection() as conn:
    #     with conn.cursor() as cur:
    #         if params:
    #             cur.execute(query, params)
    #         else:
    #             cur.execute(query)
    #         row = cur.fetchone()

    if not row:
        #raise RuntimeError("A query escalar não devolveu resultados.")
        raise RuntimeError(f"A query {query_name!r} não devolveu resultados.")

    return next(iter(row.values()))

def run_single_vinc_row_query(conn, query_name: str, query: str, params: dict | None = None) -> dict[str, object]:
    """
    Executa uma query de uma linha, mas devolve TODAS as colunas.
    Usar para tabelas que não têm T1/T2/T3/TOTAL.
    """
    # with get_db_connection() as conn:
    #     with conn.cursor() as cur:
    #         if params:
    #             cur.execute(query, params)
    #         else:
    #             cur.execute(query)
    #         row = cur.fetchone()

    row = execute_query(conn=conn,query_name=query_name,query=query,params=params,fetch_mode="one",)

    if not row:
        # raise RuntimeError("A query não devolveu resultados.")
        raise RuntimeError(f"A query {query_name!r} não devolveu resultados.")

    return dict(row)

def build_single_total_block(title: str, value: object, suffix: str = "") -> MetricBlock:
    """
    Constrói um bloco com um único cartão grande.
    """
    #text_value = f"{value}{suffix}" if suffix else str(value)

    return MetricBlock(
        key="total_silos_8h",
        title=title,
        cards=[
            MetricCard(
                label="TOTAL",
                value=format_kg(value),
                bg_color="#d9d9e3",
                text_color="#111111",
            )
        ],
    )

def build_oee_block(values: dict[str, object]) -> MetricBlock:
    """
    Constrói o bloco de OEE.
    Aqui cada cartão recebe cor em função do valor do OEE.
    """
    cards: list[MetricCard] = []

    for label in ["T1(00-08)", "T2(08-16)", "T3(16-24)", "TOTAL"]:
        numeric_value = float(values.get(label, 0) or 0)
        bg_color, text_color = get_oee_colors(numeric_value)

        cards.append(
            MetricCard(
                label=label,
                #value=f"{numeric_value:.1f} %",
                value=format_pct(numeric_value),
                bg_color=bg_color,
                text_color=text_color,
            )
        )

    return MetricBlock(
        key="oee_trituracao",
        title="Dia Anterior - Cálculo OEE",
        cards=cards,
    )

# Construção das secções do relatório diário
# Esta função é responsável por:
# 1) Executar as queries reais
# 2) Separar os blocos por secção
# 3) Devolver uma estrutura organizada para o PDF e para o e-mail
def get_daily_sections(conn, report_date: datetime) -> list[ReportSection]:
    #today_label = get_today_local_date()
    # report_date_label = get_report_date().strftime("%d/%m/%Y")
    report_date_label = report_date.strftime("%d/%m/%Y")

    # O estado dos silos é medido às 08h do dia seguinte.
    # Exemplo:
    # relatório sexta -> sábado às 08h
    snapshot_label = (
        report_date + timedelta(days=1)
    ).strftime("%d/%m/%Y")

    query_params = {
        "report_date": report_date.date()
    }

    # Secção: Trituração
    total_silos_8h = run_scalar_query(
        conn,
        "trit_total_silos_8h",
        QUERY_TRIT_TOTAL_SILOS_8H,
        query_params,
    )
    tempo_values = run_single_row_query(
        conn,
        "tempo_producao_md",
        QUERY_TEMPO_PRODUCAO_MD, 
        query_params,
    )
    horas_values = run_single_row_query(
        conn,
        "horas_moinhos",
        QUERY_HORAS_MOINHOS, 
        query_params,
    )
    kgs_values = run_single_row_query(
        conn,
        "kgs_silos_1a5",
        QUERY_KGS_SILOS, 
        query_params,
    )
    oee_values = run_single_row_query(
        conn,
        "oee_trituracao",
        QUERY_OEE, 
        query_params,
    )

    trituracao_blocks = [
        build_standard_block(
            "tempo_producao_md",
            "Tempo Produção MD",
            tempo_values,
        ),
        build_standard_block(
            "horas_moinhos",
            "Nº Horas Trabalhadas (Moinhos)",
            horas_values,
        ),
        build_kg_block(
            "kgs_silos",
            "Kgs Produzidos (Silos 1 a 5)",
            kgs_values,
        ),
        build_oee_block(oee_values),
        build_single_total_block(
            f"Total Silos AD 1 a 5 às 8h ({snapshot_label})",
            total_silos_8h,
        ),
    ]

    # Secção: Desinfeção Trituração
    desinf_kgs_values = run_single_row_query(
        conn,
        "desinf_kgs_silos_6a10",
        QUERY_DESINF_TRIT_KGS_SILOS_DIA_ANTERIOR,
        query_params,
    )

    desinf_total_silos_8h = run_scalar_query(
        conn,
        "desinf_total_silos_8h",
        QUERY_DESINF_TRIT_TOTAL_SILOS_8H,
        query_params,
    )

    desinf_trit_rows = run_multi_row_query(
        conn,
        "desinf_trit_desinfecoes",
        QUERY_DESINF_TRIT_DIA_ANTERIOR,
        query_params,
    )

    desinf_blocks = [
        build_kg_block(
            "desinf_kgs_silos",
            "Kgs Produzidos (Silos 6 a 10)",
            desinf_kgs_values,
        ),
        build_single_total_block(
            f"Total Silos PD 6 a 10 às 8h ({snapshot_label})",
            desinf_total_silos_8h,
        ),
        build_desinf_trit_desinfecoes_block(desinf_trit_rows),
    ]

    # Secção: Calibração
    calibracao_rows = run_multi_row_query(
        conn,
        "calibracao_granulado",
        QUERY_CALIB_GRANULADO_DIA_ANTERIOR,
        query_params,
    )

    calibracao_oee_rows = run_multi_row_query(
        conn,
        "calibracao_oee",
        QUERY_CALIB_OEE_TABELA_DIA_ANTERIOR,
        query_params,
    )

    calibracao_blocks = [
        build_calibracao_granulado_block(calibracao_rows),
        build_calibracao_oee_block(calibracao_oee_rows),
    ]

    # Secção: Desinfeção VINC
    desinf_vinc_silos_values = run_single_vinc_row_query(
        conn,
        "desinf_vinc_silos_8h",
        QUERY_DESINF_VINC_8H,
        query_params,
    )
    
    desinf_vinc_rows = run_multi_row_query(
        conn,
        "desinf_vinc_desinfecoes",
        QUERY_DESINF_VINC_DESINFECOES_DIA_ANTERIOR,
        query_params,
    )

    desinf_vinc_blocks = [
        build_desinf_vinc_desinfecoes_block(desinf_vinc_rows),
        build_desinf_vinc_silos_8h_block(
            desinf_vinc_silos_values,
            snapshot_label,
            ),
    ]

    # Resultado final
    # O template HTML vai receber esta lista de secções e renderizar
    return [
        ReportSection(
            title=f"Trituração ({report_date_label})",
            blocks=trituracao_blocks,
        ),
        ReportSection(
            title=f"Desinfeção Trituração ({report_date_label})",
            blocks=desinf_blocks,
        ),
        ReportSection(
            title=f"Calibração ({report_date_label})",
            blocks=calibracao_blocks,
        ),
        ReportSection(
            title=f"Desinfeção VINC ({report_date_label})",
            blocks=desinf_vinc_blocks,
        ),
    ]

# Renderização HTML para e-mail
def build_email_html(report_date: datetime) -> str:
    """
    Corpo simples do e-mail.
    O detalhe segue apenas no PDF em anexo.
    """
    return f"""
    <html>
      <body style="font-family: Arial, sans-serif;">
        <h2>Relatório Diário Granulados - {report_date.strftime('%d/%m/%Y')}</h2>
        <p>Segue em anexo o relatório diário de granulados em PDF.</p>
      </body>
    </html>
    """

# Renderização HTML
def render_html(report_date: datetime, sections: list[ReportSection]) -> str:
    """
    Renderiza o HTML final do report com base num template Jinja2.
    O CSS é carregado a partir da pasta static/.
    """
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    template = env.get_template("daily_report.html")

    css_path = (STATIC_DIR / "report.css").resolve().as_uri()

    return template.render(
        report_date=report_date.strftime("%d/%m/%Y"),
        #blocks=blocks,
        sections=sections,
        css_path=css_path,
    )

def export_debug_html(html: str, report_date: datetime) -> Path:
    """
    Guarda uma cópia HTML local do report para debug visual.
    Útil para abrir no browser e ajustar layout antes do PDF.
    """
    # path = REPORTS_DIR / f"trituracao_{report_date.strftime('%d_%m_%Y')}.html"
    path = REPORTS_DIR / f"relatorio_diario_{report_date.strftime('%d_%m_%Y')}.html"
    path.write_text(html, encoding="utf-8")
    return path

# Geração de PDF
def export_pdf(html: str, report_date: datetime) -> Path:
    # path = REPORTS_DIR / f"trituracao_{report_date.strftime('%d_%m_%Y')}.pdf"
    path = REPORTS_DIR / f"relatorio_diario_{report_date.strftime('%d_%m_%Y')}.pdf"
    # html_path = REPORTS_DIR / f"trituracao_{report_date.strftime('%d_%m_%Y')}.html"
    html_path = REPORTS_DIR / f"relatorio_diario_{report_date.strftime('%d_%m_%Y')}.html"

    html_path.write_text(html, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.resolve().as_uri(), wait_until="load")
        page.pdf(
            path=str(path),
            format = "A3", # mudança para formato A3
            # format="A4",
            landscape=True,
            print_background=True,
            margin={
                "top": "10mm",
                "right": "10mm",
                "bottom": "10mm",
                "left": "10mm",
            },
        )
        browser.close()
    
    # Apaga o HTML temporário logo após gerar o PDF
    if html_path.exists():
        html_path.unlink()

    return path

# Texto simples para fallback do e-mail
def build_plain_text(report_date: datetime) -> str:
    """
    Corpo simples em texto puro para clientes de e-mail
    que não renderizam HTML corretamente.
    """
    return (
        f"Relatório Diário Granulados - {report_date.strftime('%d/%m/%Y')}\n\n"
        "Segue em anexo o relatório diário de granulados em PDF."
    )

# Envio de e-mail
def send_email(subject: str, html_body: str, text_body: str, attachments: list[Path]) -> None:
    """
    Envia o e-mail via SMTP, com corpo em texto + HTML
    e com os anexos fornecidos.
    """
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    smtp_sender = os.environ.get("SMTP_SENDER", smtp_user)
    smtp_to = [addr.strip() for addr in os.environ["SMTP_TO"].split(",") if addr.strip()]
    use_starttls = os.environ.get("SMTP_STARTTLS", "true").lower() == "true"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_sender
    msg["To"] = ", ".join(smtp_to)
    msg["Date"] = formatdate(localtime=True)

    # Corpo em texto simples
    msg.set_content(text_body)

    # Corpo HTML
    msg.add_alternative(html_body, subtype="html")

    # Anexos
    for attachment in attachments:
        with attachment.open("rb") as f:
            data = f.read()

        if attachment.suffix.lower() == ".pdf":
            maintype, subtype = "application", "pdf"
        elif attachment.suffix.lower() == ".html":
            maintype, subtype = "text", "html"
        else:
            maintype, subtype = "application", "octet-stream"

        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.name,
        )

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        if use_starttls:
            server.starttls()
            server.ehlo()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)

def cleanup_old_reports() -> None:
    """
    Conserva apenas os REPORT_KEEP_COUNT PDFs mais recentes.
    """
    pdf_files = sorted(REPORTS_DIR.glob("*.pdf"),
        key=lambda path: path.stat().st_mtime,reverse=True,)

    for old_pdf in pdf_files[REPORT_KEEP_COUNT:]:
        try:
            old_pdf.unlink()

            print(f"[INFO] PDF antigo apagado: {old_pdf}",flush=True,)
        except OSError as exc:
            print(f"[WARN] Não foi possível apagar "f"{old_pdf}: {exc}",flush=True,)

# Função principal
def main() -> None:
    """
    Executa diariamente e avalia apenas o dia anterior.

    - sexta é enviada sábado após o fim do T3;
    - sábado é enviado domingo se houve produção;
    - domingo é enviado segunda se houve produção;
    - HOLIDAYS só são enviados se houve produção.
    """
    now = datetime.now(TZ)
    report_day = get_previous_calendar_day(now)

    ready_at = get_report_ready_at(report_day)

    if not is_report_ready(now, report_day):
        print(f"[INFO] O relatório de "f"{report_day:%d/%m/%Y} ainda não está pronto. "
            f"Executar depois de "f"{ready_at:%d/%m/%Y %H:%M}.",flush=True,)
        return

    errors: list[str] = []

    with get_db_connection() as conn:
        report_days = get_report_days_to_send(conn, now,)

        if not report_days:
            print(f"[INFO] Sem relatório para enviar. "f"{report_day:%d/%m/%Y} foi "
                "fim de semana/HOLIDAY sem atividade.",flush=True,)
            return

        for report_day in report_days:
            report_date = datetime.combine(report_day, dt_time(0, 0), tzinfo=TZ,)

            pdf_path: Path | None = None

            try:
                print(f"[INFO] A gerar relatório de "f"{report_day:%d/%m/%Y}.",flush=True,)

                sections = get_daily_sections(conn,report_date,)

                pdf_html = render_html(report_date,sections,)

                email_html = build_email_html(report_date,)

                pdf_path = export_pdf(pdf_html,report_date,)

                send_email_enabled = (
                    os.environ
                    .get("SEND_EMAIL", "true")
                    .lower()
                    == "true"
                )

                if send_email_enabled:
                    send_email(
                        subject=("Relatório Diário Granulados - "f"{report_date:%d/%m/%Y}"),
                        html_body=email_html,
                        text_body=build_plain_text(report_date),
                        attachments=[pdf_path],
                    )

                    print(f"[OK] E-mail de " f"{report_day:%d/%m/%Y} ""enviado com sucesso.",flush=True,)
                else:
                    print("[INFO] SEND_EMAIL=false; ""e-mail não enviado.",flush=True,)

                print(f"[OK] PDF criado: {pdf_path}",flush=True,)
                cleanup_old_reports()

            except Exception as exc:
                error = (f"Falha no relatório de " f"{report_day:%d/%m/%Y}: " f"{exc}")

                print(f"[ERRO] {error}",flush=True,)

                errors.append(error)

            # finally:
            #     if pdf_path and pdf_path.exists():
            #         pdf_path.unlink()

            #         print(f"[INFO] PDF apagado: " f"{pdf_path}",flush=True,)

    if errors:
        raise RuntimeError(" | ".join(errors))


if __name__ == "__main__":
    main()
