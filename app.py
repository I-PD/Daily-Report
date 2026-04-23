from __future__ import annotations

#import csv
import os
#import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Iterable
from dotenv import load_dotenv
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
import xlsxwriter

from queries import (
    QUERY_TEMPO_PRODUCAO_MD,
    QUERY_HORAS_MOINHOS,
    QUERY_KGS_SILOS,
    QUERY_OEE,
)

load_dotenv(Path(__file__).with_name(".env"))
BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

@dataclass
class ReportTable:
    key: str
    title: str
    sheet_name: str
    values: dict[str, object]

def get_report_date() -> datetime:
    today = datetime.now()
    if today.weekday() == 0:
        return today - timedelta(days=3)
    return today - timedelta(days=1)

def parse_time_pct(value: str) -> tuple[str, int]:
    match = re.fullmatch(r"\s*([0-9]{2}h[0-9]{2})\s*\((\d+)%\)\s*", value)
    if not match:
        raise ValueError(f"Formato inesperado vindo da query: {value!r}")
    return match.group(1), int(match.group(2))

def get_db_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        cursor_factory=RealDictCursor,
    )
def run_single_row_query(query: str, params: dict | None = None) -> dict[str, object]:
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

def get_tables_data() -> list[ReportTable]:
    use_mock = os.environ.get("USE_MOCK_DATA", "false").lower() == "true"

    if use_mock:
        return [
            ReportTable(
                key="tempo_producao_md",
                title="Tempo Produção MD",
                sheet_name="01_Tempo_MD",
                values={
                    "T1(08-16)": "05h33 (49%)",
                    "T2(16-24)": "00h45 (36%)",
                    "T3(00-08)": "05h05 (28%)",
                    "TOTAL": "11h24 (39%)",
                },
            ),
            ReportTable(
                key="horas_moinhos",
                title="Nº Horas Trabalhadas Moinhos",
                sheet_name="02_Horas_Moinhos",
                values={
                    "T1(08-16)": "07h10",
                    "T2(16-24)": "06h50",
                    "T3(00-08)": "07h20",
                    "TOTAL": "21h20",
                },
            ),
            ReportTable(
                key="kgs_silos",
                title="Kgs Produzidos nos Silos",
                sheet_name="03_Kgs_Silos",
                values={
                    "T1(08-16)": 12000,
                    "T2(16-24)": 9800,
                    "T3(00-08)": 11150,
                    "TOTAL": 32950,
                },
            ),
            ReportTable(
                key="oee_trituracao",
                title="OEE da Secção",
                sheet_name="04_OEE",
                values={
                    "T1(08-16)": 68.4,
                    "T2(16-24)": 54.2,
                    "T3(00-08)": 60.1,
                    "TOTAL": 61.0,
                },
            ),
        ]

    return [
        ReportTable(
            key="tempo_producao_md",
            title="Tempo Produção MD",
            sheet_name="01_Tempo_MD",
            values=run_single_row_query(QUERY_TEMPO_PRODUCAO_MD),
        ),
        ReportTable(
            key="horas_moinhos",
            title="Nº Horas Trabalhadas Moinhos",
            sheet_name="02_Horas_Moinhos",
            values=run_single_row_query(QUERY_HORAS_MOINHOS),
        ),
        ReportTable(
            key="kgs_silos",
            title="Kgs Produzidos nos Silos",
            sheet_name="03_Kgs_Silos",
            values=run_single_row_query(QUERY_KGS_SILOS),
        ),
        ReportTable(
            key="oee_trituracao",
            title="OEE da Secção",
            sheet_name="04_OEE",
            values=run_single_row_query(QUERY_OEE),
        ),
    ]

def build_html_summary(report_date: datetime, tables: list[ReportTable]) -> str:
    parts = [
        "<html><head><meta charset='UTF-8'></head><body style='font-family:Arial;'>",
        f"<h2>Relatório Diário - Trituração - {report_date.strftime('%d/%m/%Y')}</h2>",
        "<p>Segue em anexo o ficheiro Excel com o detalhe.</p>",
    ]

    for table in tables:
        parts.append(f"<h3>{table.title}</h3>")
        parts.append(
            """
            <table style="border-collapse:collapse; margin-bottom:20px;">
              <tr>
                <th style="border:1px solid #999; padding:8px; background:#dce6f1;">T1(08-16)</th>
                <th style="border:1px solid #999; padding:8px; background:#dce6f1;">T2(16-24)</th>
                <th style="border:1px solid #999; padding:8px; background:#dce6f1;">T3(00-08)</th>
                <th style="border:1px solid #999; padding:8px; background:#666; color:#fff;">TOTAL</th>
              </tr>
            """
        )
        parts.append("<tr>")
        for label in ["T1(08-16)", "T2(16-24)", "T3(00-08)", "TOTAL"]:
            value = table.values.get(label, "")
            parts.append(
                f"<td style='border:1px solid #999; padding:8px; text-align:center;'>{value}</td>"
            )
        parts.append("</tr></table>")

    parts.append("</body></html>")
    return "".join(parts)

def export_html(html: str, report_date: datetime) -> Path:
    path = BASE_DIR / f"trituracao_{report_date.strftime('%d_%m_%Y')}.html"
    path.write_text(html, encoding="utf-8")
    return path

def export_xlsx(tables: list[ReportTable], report_date: datetime) -> Path:
    path = REPORTS_DIR / f"trituracao_{report_date.strftime('%d_%m_%Y')}.xlsx"
    workbook = xlsxwriter.Workbook(path.as_posix())

    title_fmt = workbook.add_format({
        "bold": True,
        "font_size": 14,
        "align": "center",
        "valign": "vcenter",
        "font_color": "#6AA7FF", 
        "bg_color": "#0F2238",
        "border": 1,
    })
    header_fmt = workbook.add_format({
        "bold": True,
        "align": "center",
        "valign": "vcenter",
        "bg_color": "#DCE6F1",
        "border": 1,
    })
    normal_fmt = workbook.add_format({
        "align": "center",
        "valign": "vcenter",
        "border": 1,
    })
    total_fmt = workbook.add_format({
        "bold": True,
        "font_color": "#FFFFFF",
        "bg_color": "#666666",
        "align": "center",
        "valign": "vcenter",
        "border": 1,
    })
    percent_fmt = workbook.add_format({
        "align": "center",
        "valign": "vcenter",
        "border": 1,
        "num_format": "0.0",
    })
    number_fmt = workbook.add_format({
        "align": "center",
        "valign": "vcenter",
        "border": 1,
        "num_format": "#,##0.00",
    })

    for table in tables:
        sheet = workbook.add_worksheet(table.sheet_name[:31])

        sheet.merge_range(
            "A1:E1",
            f"{table.title} - {report_date.strftime('%d/%m/%Y')}",
            title_fmt,
        )
        sheet.write_row("A3", ["Indicador", "T1(08-16)", "T2(16-24)", "T3(00-08)", "TOTAL"], header_fmt)

        sheet.write("A4", table.title, normal_fmt)

        for col_idx, label in enumerate(["T1(08-16)", "T2(16-24)", "T3(00-08)", "TOTAL"], start=1):
            value = table.values.get(label, "")

            cell_fmt = total_fmt if label == "TOTAL" else normal_fmt

            if isinstance(value, (int, float)):
                if table.key == "oee_trituracao":
                    fmt = total_fmt if label == "TOTAL" else percent_fmt
                    sheet.write_number(3, col_idx, float(value), fmt)
                else:
                    fmt = total_fmt if label == "TOTAL" else number_fmt
                    sheet.write_number(3, col_idx, float(value), fmt)
            else:
                sheet.write(3, col_idx, str(value), cell_fmt)

        sheet.set_column("A:A", 28)
        sheet.set_column("B:E", 16)
        sheet.set_row(0, 26)

    workbook.close()
    return path

def build_plain_text(report_date: datetime, tables: list[ReportTable]) -> str:
    lines = [f"Relatório Diário - Trituração - {report_date.strftime('%d/%m/%Y')}", ""]
    for table in tables:
        lines.append(table.title)
        for label in ["T1(08-16)", "T2(16-24)", "T3(00-08)", "TOTAL"]:
            lines.append(f"  - {label}: {table.values.get(label, '')}")
        lines.append("")
    return "\n".join(lines)


def send_email(subject: str, html_body: str, text_body: str, attachments: list[Path]) -> None:
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
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    for attachment in attachments:
        with attachment.open("rb") as f:
            data = f.read()
        if attachment.suffix.lower() == ".html":
            maintype, subtype = "text", "html"
        elif attachment.suffix.lower() == ".csv":
            maintype, subtype = "text", "csv"
        elif attachment.suffix.lower() == ".xlsx":
            maintype, subtype = (
                "application",
                "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=attachment.name)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        if use_starttls:
            server.starttls()
            server.ehlo()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)

def main() -> None:
    report_date = get_report_date()
    tables = get_tables_data()

    html = build_html_summary(report_date, tables)
    xlsx_path = export_xlsx(tables, report_date)

    if os.environ.get("SEND_EMAIL", "true").lower() == "true":
        send_email(
            subject=f"Relatório Diário - Trituração - {report_date.strftime('%d/%m/%Y')}",
            html_body=html,
            text_body=build_plain_text(report_date, tables),
            attachments=[xlsx_path],
        )
        print("E-mail enviado com sucesso.")
    else:
        print("SEND_EMAIL=false, e-mail não enviado.")

    print(f"XLSX criado: {xlsx_path}")

if __name__ == "__main__":
    main()