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
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader
import psycopg2
from psycopg2.extras import RealDictCursor
from playwright.sync_api import sync_playwright

from queries import (
    QUERY_TEMPO_PRODUCAO_MD,
    QUERY_HORAS_MOINHOS,
    QUERY_KGS_SILOS,
    QUERY_OEE,
    QUERY_TRIT_TOTAL_SILOS_8H,
    QUERY_DESINF_TRIT_KGS_SILOS_DIA_ANTERIOR,
    QUERY_DESINF_TRIT_TOTAL_SILOS_8H,
)

# Configuração base do projeto
# Carrega o ficheiro .env que está na mesma pasta do app.py
load_dotenv(Path(__file__).with_name(".env"))

# Pasta base do projeto
BASE_DIR = Path(__file__).resolve().parent

# Pasta onde vamos guardar os reports gerados
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

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
        label = "T1(08-16)"
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
class ReportSection:
    """
    Uma secção agrupa vários blocos visuais.
    Exemplo:
     - Secção "Trituração"
       - Tempo Produção MD
       - Horas Trabalhadas
       - Kgs Produzidos
       - OEE
     - Secção "Desinfeção Trituração"
       - Kgs Produzidos Silos 6 a 10
       - Total Silos PD às 8h
    """
    title:str
    blocks: list[MetricBlock]

# Regras visuais / cores
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
def get_report_date() -> datetime:
    """
    Define a data de referência do relatório:
    - se hoje é segunda-feira -> devolve sexta-feira
    - caso contrário -> devolve ontem
    """
    today = datetime.now()
    if today.weekday() == 0:  # segunda-feira
        return today - timedelta(days=3)
    return today - timedelta(days=1)

def get_today_local_date() -> str:
    """
    Devolve a data local de hoje em formato dd/mm/YYYY.
    """
    return datetime.now().strftime("%d/%m/%Y")

# Ligação à base de dados
def get_db_connection():
    """
    Abre ligação à base de dados PostgreSQL usando as
    variáveis definidas no .env.
    """
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        cursor_factory=RealDictCursor,
    )

#  Formatação de valores dentro das células
def format_kg(value: object) -> str:
    """
    Formata um valor de kg sem casas decimais.
    Ex.: 10425.0 -> '10425 kg'
    """
    if value is None:
        return "0 kg"
    return f"{int(round(float(value)))} kg"


def format_pct(value: object) -> str:
    """
    Formata um valor percentual com 1 casa decimal.
    Ex.: 73.6 -> '73.6 %'
    """
    if value is None:
        return "0 %"
    return f"{float(value):.1f} %"

# Execução genérica de queries
def run_single_row_query(query: str, params: dict | None = None) -> dict[str, object]:
    """
    Executa uma query que deve devolver apenas UMA linha,
    com colunas do tipo:
        T1(08-16), T2(16-24), T3(00-08), TOTAL

    Exemplo de saída:
        {
            "T1(08-16)": "05h33 (49%)",
            "T2(16-24)": "00h45 (36%)",
            "T3(00-08)": "05h05 (28%)",
            "TOTAL": "11h24 (39%)",
        }
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if params:
                cur.execute(query, params)
            else:
                cur.execute(query)
            row = cur.fetchone()

    if not row:
        raise RuntimeError("A query não devolveu resultados.")

    ordered_labels = ["T1(08-16)", "T2(16-24)", "T3(00-08)", "TOTAL"]
    return {label: row.get(label) for label in ordered_labels if label in row}

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

    for label in ["T1(08-16)", "T2(16-24)", "T3(00-08)", "TOTAL"]:
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

    for label in ["T1(08-16)", "T2(16-24)", "T3(00-08)", "TOTAL"]:
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

def run_scalar_query(query: str, params: dict | None = None) -> object:
    """
    Executa uma query que devolve uma única linha e uma única coluna.
    Exemplo:
        SELECT 123 AS "TOTAL"
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if params:
                cur.execute(query, params)
            else:
                cur.execute(query)
            row = cur.fetchone()

    if not row:
        raise RuntimeError("A query escalar não devolveu resultados.")

    return next(iter(row.values()))

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

    for label in ["T1(08-16)", "T2(16-24)", "T3(00-08)", "TOTAL"]:
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

# A partir daqui, o relatório deixa de ser uma lista única de blocos
# e passa a estar organizado por secções:
# - Trituração
# - Desinfeção Trituração
def get_daily_sections() -> list[ReportSection]:
    today_label = get_today_local_date()
    report_date_label = get_report_date().strftime("%d/%m/%Y")
    # Secção: Trituração
    # Dados referentes ao dia anterior:
    # - Tempo Produção MD
    # - Nº Horas Trabalhadas Moinhos
    # - Kgs Produzidos Silos 1 a 5
    # - OEE
    # Dados referentes ao dia atual às 08h:
    # - Total Silos AD 1 a 5

    total_silos_8h = run_scalar_query(QUERY_TRIT_TOTAL_SILOS_8H)
    tempo_values = run_single_row_query(QUERY_TEMPO_PRODUCAO_MD)
    horas_values = run_single_row_query(QUERY_HORAS_MOINHOS)
    kgs_values = run_single_row_query(QUERY_KGS_SILOS)
    oee_values = run_single_row_query(QUERY_OEE)

    trituracao_blocks = [
        build_standard_block(
            "tempo_producao_md",
            "Dia Anterior - Tempo Produção MD",
            tempo_values,
        ),
        build_standard_block(
            "horas_moinhos",
            "Dia Anterior - Nº Horas Trabalhadas (Moinhos)",
            horas_values,
        ),
        build_kg_block(
            "kgs_silos",
            "Dia Anterior - Kgs Produzidos (Silos 1 a 5)",
            kgs_values,
        ),
        build_oee_block(oee_values),
        build_single_total_block(
            f"Total Silos AD 1 a 5 às 8h ({today_label})",
            total_silos_8h,
        ),
    ]

    # Secção: Desinfeção Trituração
    # Dados referentes ao dia anterior:
    # - Kgs Produzidos Silos 6 a 10
    # Dados referentes ao dia atual às 08h:
    # - Total Silos PD 6 a 10

    desinf_kgs_values = run_single_row_query(
        QUERY_DESINF_TRIT_KGS_SILOS_DIA_ANTERIOR
    )
    desinf_total_silos_8h = run_scalar_query(
        QUERY_DESINF_TRIT_TOTAL_SILOS_8H
    )

    desinf_blocks = [
        build_kg_block(
            "desinf_kgs_silos",
            "Dia Anterior - Kgs Produzidos (Silos 6 a 10)",
            desinf_kgs_values,
        ),
        build_single_total_block(
            f"Total Silos PD 6 a 10 às 8h ({today_label})",
            desinf_total_silos_8h,
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
    ]

# Renderização HTML para e-mail
# def render_email_html(report_date: datetime, blocks: list[MetricBlock]) -> str:
#     """
#     Gera o HTML do corpo do e-mail num formato simples e estável,
#     mais adequado para Outlook e outros clientes de e-mail.

#     Este HTML é separado do HTML do PDF:
#     - e-mail -> simples, tabelas clássicas
#     - PDF    -> layout tipo dashboard
#     """
#     parts = [
#         "<html><head><meta charset='UTF-8'></head>",
#         "<body style='font-family: Arial, sans-serif; color:#111111; background:#ffffff;'>",
#         f"<h2 style='margin-bottom:8px;'>Relatório Diário - Trituração - {report_date.strftime('%d/%m/%Y')}</h2>",
#         "<p style='margin-top:0;'>Segue em anexo o relatório diário em PDF.</p>",
#     ]

#     for block in blocks:
#         parts.append(
#             f"<h3 style='margin:20px 0 8px 0; color:#111111;'>{block.title}</h3>"
#         )

#         # Caso especial: bloco com um único cartão
#         if len(block.cards) == 1:
#             card = block.cards[0]
#             parts.append(
#                 f"""
#                 <table style="border-collapse:collapse; width:100%; max-width:900px; margin-bottom:18px;">
#                   <tr>
#                     <th style="border:1px solid #cfcfcf; padding:8px; background:#666666; color:#ffffff; text-align:center;">
#                       {card.label}
#                     </th>
#                   </tr>
#                   <tr>
#                     <td style="
#                         border:1px solid #cfcfcf;
#                         padding:18px 10px;
#                         text-align:center;
#                         background:{card.bg_color};
#                         color:{card.text_color};
#                         font-size:18px;
#                         font-weight:bold;
#                     ">
#                         {card.value}
#                     </td>
#                   </tr>
#                 </table>
#                 """
#             )
#             continue

#         # Blocos normais com 4 cartões
#         parts.append(
#             """
#             <table style="border-collapse:collapse; width:100%; max-width:900px; margin-bottom:18px;">
#               <tr>
#                 <th style="border:1px solid #cfcfcf; padding:8px; background:#e9e9ef; text-align:center;">T1(08-16)</th>
#                 <th style="border:1px solid #cfcfcf; padding:8px; background:#e9e9ef; text-align:center;">T2(16-24)</th>
#                 <th style="border:1px solid #cfcfcf; padding:8px; background:#e9e9ef; text-align:center;">T3(00-08)</th>
#                 <th style="border:1px solid #cfcfcf; padding:8px; background:#666666; color:#ffffff; text-align:center;">TOTAL</th>
#               </tr>
#               <tr>
#             """
#         )

#         for card in block.cards:
#             parts.append(
#                 f"""
#                 <td style="
#                     border:1px solid #cfcfcf;
#                     padding:14px 10px;
#                     text-align:center;
#                     background:{card.bg_color};
#                     color:{card.text_color};
#                     font-size:16px;
#                     font-weight:bold;
#                 ">
#                     <div style="font-size:12px; font-weight:normal; margin-bottom:8px;">{card.label}</div>
#                     <div>{card.value}</div>
#                 </td>
#                 """
#             )

#         parts.append("</tr></table>")

#     parts.append("</body></html>")
#     return "".join(parts)

def render_email_html(report_date: datetime, sections: list[ReportSection]) -> str:
    parts = [
        "<html><head><meta charset='UTF-8'></head>",
        "<body style='font-family: Arial, sans-serif; color:#111111; background:#ffffff;'>",
        f"<h2 style='margin-bottom:8px;'>Relatório Diário - {report_date.strftime('%d/%m/%Y')}</h2>",
        "<p style='margin-top:0;'>Segue em anexo o relatório diário em PDF.</p>",
    ]

    for section in sections:
        parts.append(
            f"<h3 style='margin:24px 0 10px 0; color:#111111; border-bottom:2px solid #d0d0d0; padding-bottom:6px;'>"
            f"{section.title}</h3>"
        )

        for block in section.blocks:
            parts.append(
                f"<h4 style='margin:18px 0 8px 0; color:#111111;'>{block.title}</h4>"
            )

            # Caso especial: bloco com um único cartão
            if len(block.cards) == 1:
                card = block.cards[0]
                parts.append(
                    f"""
                    <table style="border-collapse:collapse; width:100%; max-width:900px; margin-bottom:18px;">
                      <tr>
                        <th style="border:1px solid #cfcfcf; padding:8px; background:#666666; color:#ffffff; text-align:center;">
                          {card.label}
                        </th>
                      </tr>
                      <tr>
                        <td style="
                            border:1px solid #cfcfcf;
                            padding:18px 10px;
                            text-align:center;
                            background:{card.bg_color};
                            color:{card.text_color};
                            font-size:18px;
                            font-weight:bold;
                        ">
                            {card.value}
                        </td>
                      </tr>
                    </table>
                    """
                )
                continue

            # Blocos normais com T1, T2, T3 e TOTAL
            parts.append(
                """
                <table style="border-collapse:collapse; width:100%; max-width:900px; margin-bottom:18px;">
                  <tr>
                    <th style="border:1px solid #cfcfcf; padding:8px; background:#e9e9ef; text-align:center;">T1(08-16)</th>
                    <th style="border:1px solid #cfcfcf; padding:8px; background:#e9e9ef; text-align:center;">T2(16-24)</th>
                    <th style="border:1px solid #cfcfcf; padding:8px; background:#e9e9ef; text-align:center;">T3(00-08)</th>
                    <th style="border:1px solid #cfcfcf; padding:8px; background:#666666; color:#ffffff; text-align:center;">TOTAL</th>
                  </tr>
                  <tr>
                """
            )

            for card in block.cards:
                parts.append(
                    f"""
                    <td style="
                        border:1px solid #cfcfcf;
                        padding:14px 10px;
                        text-align:center;
                        background:{card.bg_color};
                        color:{card.text_color};
                        font-size:16px;
                        font-weight:bold;
                    ">
                        <div style="font-size:12px; font-weight:normal; margin-bottom:8px;">{card.label}</div>
                        <div>{card.value}</div>
                    </td>
                    """
                )

            parts.append("</tr></table>")

    parts.append("</body></html>")
    return "".join(parts)

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
    path = REPORTS_DIR / f"trituracao_{report_date.strftime('%d_%m_%Y')}.html"
    path.write_text(html, encoding="utf-8")
    return path

# Geração de PDF
def export_pdf(html: str, report_date: datetime) -> Path:
    path = REPORTS_DIR / f"trituracao_{report_date.strftime('%d_%m_%Y')}.pdf"
    html_path = REPORTS_DIR / f"trituracao_{report_date.strftime('%d_%m_%Y')}.html"
    html_path.write_text(html, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.resolve().as_uri(), wait_until="load")
        page.pdf(
            path=str(path),
            format="A4",
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

    return path

# Texto simples para fallback do e-mail
def build_plain_text(report_date: datetime) -> str:
    """
    Corpo simples em texto puro para clientes de e-mail
    que não renderizam HTML corretamente.
    """
    return (
        f"Relatório Diário - Trituração - {report_date.strftime('%d/%m/%Y')}\n\n"
        "Segue em anexo o relatório diário em PDF."
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

# Função principal
def main() -> None:
    """
    Fluxo principal:
    1) calcula data do report
    2) vai buscar os blocos
    3) renderiza HTML do PDF
    4) renderiza HTML do e-mail (separado, mais simples)
    5) guarda HTML de debug do PDF
    6) gera PDF
    7) envia por e-mail
    """
    report_date = get_report_date()
    #blocks = get_daily_blocks()
    #Usamse secções, não uma lista única de blocos
    sections = get_daily_sections()

    #pdf_html = render_html(report_date, blocks)
    pdf_html = render_html(report_date, sections)
    #email_html = render_email_html(report_date, blocks)
    email_html = render_email_html(report_date, sections)

    debug_html_path = export_debug_html(pdf_html, report_date)
    pdf_path = export_pdf(pdf_html, report_date)

    if os.environ.get("SEND_EMAIL", "true").lower() == "true":
        send_email(
            subject=f"Relatório Diário - {report_date.strftime('%d/%m/%Y')}",
            html_body=email_html,
            text_body=build_plain_text(report_date),
            attachments=[pdf_path],
        )
        print("E-mail enviado com sucesso.")
    else:
        print("SEND_EMAIL=false, e-mail não enviado.")

    print(f"HTML debug criado: {debug_html_path}")
    print(f"PDF criado: {pdf_path}")

if __name__ == "__main__":
    main()