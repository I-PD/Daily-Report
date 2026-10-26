"""Relatório exclusivo de domingo: snapshot VINC dos silos 1 a 10 às 12h.

Uso seguro:
  python sunday_vinc.py --preview              # NÃO envia e-mail
  python sunday_vinc.py --preview --date 2026-10-25
  python sunday_vinc.py --send                 # Apenas domingo, desde as 12h

Não importa nem executa app.py; assim nunca dispara o relatório diário.
"""
from __future__ import annotations

import argparse
import os
import smtplib
from contextlib import closing
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

from queries import QUERY_DESINF_VINC_12H

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TZ = ZoneInfo("Europe/Lisbon")
REPORTS_DIR = BASE_DIR / "reports"

# Mesmos nomes e ordem do SELECT em QUERY_DESINF_VINC_12H.
SILOS_1A6 = [
    "SILO 1: 3A7 CS", "SILO 2: 2A3 CS", "SILO 3: 1A2 CS",
    "SILO 4: 05A1 CS", "SILO 5: 1A2 VINC", "SILO 6: 05A1 VINC",
]
SILOS_7A10 = [
    "SILO 7: 05A1 XF", "SILO 8: 1A2 XF",
    "SILO 9: 2A3 XF", "SILO 10: 3A7 XF",
]

def get_sunday_snapshot(now: datetime, selected_day: date | None) -> datetime:
    """Um snapshot fixo de domingo às 12:00, em Europe/Lisbon."""
    if selected_day is None:
        days_since_sunday = (now.weekday() + 1) % 7
        selected_day = now.date() - timedelta(days=days_since_sunday)
        if selected_day == now.date() and now.time() < time(12, 0):
            selected_day -= timedelta(days=7)

    if selected_day.weekday() != 6:
        raise ValueError(f"A data {selected_day} não corresponde a domingo.")
    return datetime.combine(selected_day, time(12, 0), tzinfo=TZ)


def read_silos(snapshot_at: datetime) -> dict[str, object]:
    """Executa APENAS a query de domingo; não corre qualquer outro relatório."""
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        cursor_factory=RealDictCursor,
        connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "15")),
        options="-c statement_timeout=" + os.environ.get("DB_STATEMENT_TIMEOUT_MS", "120000"),
        application_name="csp4_sunday_vinc",
    )
    with closing(conn):
        with conn.cursor() as cursor:
            cursor.execute(QUERY_DESINF_VINC_12H, {"snapshot_at": snapshot_at})
            row = cursor.fetchone()
    if not row:
        raise RuntimeError("A QUERY_DESINF_VINC_12H não devolveu resultados.")
    return dict(row)


def format_kg(value: object) -> str:
    """Mesma apresentação numérica do relatório diário."""
    if value is None:
        return "0 kg"
    return f"{int(round(float(value))):,}".replace(",", " ") + " kg"


def render_table(headers: list[str], values: dict[str, object]) -> str:
    """Duas tabelas de largura 100%; as 4 células de baixo ficam mais largas."""
    # Cabeçalhos verdes nos CS (1-4), azuis nos VINC/XF (5-10).
    th_cells = []
    td_cells = []
    for header in headers:
        silo_id = int(header.split(':', 1)[0].replace('SILO', '').strip())
        # Silos 1-4: verde
        # Silos 5-6: azul
        # Silos 7-10: vermelho
        if silo_id <= 4:
            bg = "#73bf69"
        elif silo_id <= 6:
            bg = "#5794f2"
        else:
            bg = "#ee4b2b"
        th_cells.append(
            f'<th style="border:1px solid #999999;background:{bg};'
            'color:#fff;padding:7px 2px;text-align:center;font-size:12px;'
            f'font-weight:bold;">{escape(header)}</th>'
        )
        td_cells.append(
            '<td style="border:1px solid #999999;background:#d9d9e3;'
            'color:#111;padding:9px 2px;text-align:center;font-size:13px;'
            f'font-weight:bold;">{escape(format_kg(values.get(header, 0)))}</td>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="width:100%;table-layout:fixed;border-collapse:collapse;margin:0 0 12px;">'
        '<thead><tr>' + ''.join(th_cells) + '</tr></thead>'
        '<tbody><tr>' + ''.join(td_cells) + '</tr></tbody></table>'
    )


def build_sunday_html(snapshot_at: datetime, values: dict[str, object]) -> str:
    """E-mail contém APENAS as duas tabelas VINC, sem outras secções."""
    return (
        '<!DOCTYPE html><html lang="pt"><head><meta charset="UTF-8"></head>'
        '<body style="margin:0;padding:14px;background:white;color:#111;'
        'font-family:Arial,sans-serif;">'
        f'<h2 style="margin:0 0 12px;font-size:18px;">'
        f'Silos Desinfeção VINC — {snapshot_at:%d/%m/%Y} às 12h</h2>'
        + render_table(SILOS_1A6, values)
        + render_table(SILOS_7A10, values)
        + '</body></html>'
    )


def get_sunday_recipients() -> list[str]:
    """Não usa SMTP_TO (destinatários do diário). Falha se não estiver configurado."""
    recipients = [x.strip() for x in os.environ.get("SMTP_TO_SUNDAY", "").split(",") if x.strip()]
    if not recipients:
        raise ValueError("Definir SMTP_TO_SUNDAY no .env antes de enviar o domingo.")
    return recipients


def send_sunday_email(snapshot_at: datetime, body: str, recipients: list[str]) -> None:
    """SMTP separado dos destinatários diários, usando credenciais comuns."""
    smtp_user = os.environ["SMTP_USER"]
    msg = EmailMessage()
    msg["Subject"] = f"Silos Desinfeção VINC — Domingo {snapshot_at:%d/%m/%Y} 12h"
    msg["From"] = os.environ.get("SMTP_SENDER", smtp_user)
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(
        f"Silos 1 a 10 — domingo {snapshot_at:%d/%m/%Y}, às 12h.\n"
        "O detalhe está nas duas tabelas do e-mail HTML."
    )
    msg.add_alternative(body, subtype="html")

    with smtplib.SMTP(
        os.environ["SMTP_HOST"],
        int(os.environ.get("SMTP_PORT", "587")),
        timeout=int(os.environ.get("SMTP_TIMEOUT_SECONDS", "20")),
    ) as server:
        server.ehlo()
        if os.environ.get("SMTP_STARTTLS", "true").lower() == "true":
            server.starttls()
            server.ehlo()
        server.login(smtp_user, os.environ["SMTP_PASSWORD"])
        server.send_message(msg)


def main() -> None:
    parser = argparse.ArgumentParser(description="E-mail de domingo VINC, 12h")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Gerar HTML local; NUNCA enviar e-mail")
    mode.add_argument("--send", action="store_true", help="Enviar; apenas domingo a partir das 12h")
    parser.add_argument("--date", type=date.fromisoformat, help="Data YYYY-MM-DD; só com --preview")
    args = parser.parse_args()

    now = datetime.now(TZ)
    if args.send:
        if args.date is not None:
            parser.error("--date só é permitido com --preview")
        if now.weekday() != 6 or now.time() < time(12, 0):
            parser.error("--send só é permitido ao domingo, a partir das 12h (Lisboa)")
        # Impede envios inadvertidos para a lista do relatório diário.
        recipients = get_sunday_recipients()
        if os.environ.get("SEND_EMAIL", "true").lower() != "true":
            print("[INFO] SEND_EMAIL=false; envio desativado.", flush=True)
            return
    else:
        recipients = []

    snapshot_at = get_sunday_snapshot(now, args.date)
    print(f"[INFO] Snapshot VINC: {snapshot_at:%d/%m/%Y %H:%M} Europe/Lisbon", flush=True)
    values = read_silos(snapshot_at)
    body = build_sunday_html(snapshot_at, values)

    if args.preview:
        REPORTS_DIR.mkdir(exist_ok=True)
        destination = REPORTS_DIR / f"preview_silos_vinc_domingo_{snapshot_at:%Y_%m_%d}.html"
        destination.write_text(body, encoding="utf-8")
        print(f"[OK] Pré-visualização criada: {destination}", flush=True)
        print("[OK] Modo preview: NENHUM e-mail enviado.", flush=True)
        return

    send_sunday_email(snapshot_at, body, recipients)
    print(f"[OK] E-mail de domingo enviado a {len(recipients)} destinatário(s).", flush=True)


if __name__ == "__main__":
    main()
