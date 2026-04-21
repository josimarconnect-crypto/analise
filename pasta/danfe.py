# app.py
# API DANFSe Oficial (Ambiente Nacional NFS-e) via mTLS (cert A1 em pem/key no Supabase)
#
# Endpoints:
#   GET  /health
#   POST /danfse/pdf   -> retorna application/pdf
#
# Render: use gunicorn (ver Procfile abaixo)

from __future__ import annotations

import os
import re
import time
import base64
import tempfile
import logging
from typing import Dict, Any, Optional, Tuple

import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ==========================
# Config
# ==========================
APP_NAME = "danfse-api"

SUPABASE_URL = "https://hysrxadnigzqadnlkynq.supabase.co"
SUPABASE_SERVICE_ROLE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc0MzcxNDA4MCwiZXhwIjoyMDU5MjkwMDgwfQ.cbeC4ROB7GXbKUU47nDpnQFeIYaEcvUr8_szTRxFZOs"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA"

TABELA_CERTS = os.getenv("TABELA_CERTS", "certifica_dfe").strip()

# Endpoint DANFSe (path padrão)
DANFSE_PATH_TEMPLATE = os.getenv("DANFSE_PATH_TEMPLATE", "/danfse/{chave}").strip()

# Timeout e retries
DEFAULT_TIMEOUT = int(os.getenv("DANFSE_TIMEOUT", "60"))
DEFAULT_TRIES = int(os.getenv("DANFSE_TRIES", "5"))
DEFAULT_BACKOFF = float(os.getenv("DANFSE_BACKOFF", "1.5"))

# Logs
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# ==========================
# Flask
# ==========================
app = Flask(__name__)
CORS(app)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger(APP_NAME)


# ==========================
# Helpers gerais
# ==========================
def only_digits(s: str) -> str:
    return re.sub(r"\D+", "", s or "")

def json_error(msg: str, status: int = 400, extra: Optional[Dict[str, Any]] = None):
    payload = {"erro": msg}
    if extra:
        payload.update(extra)
    return jsonify(payload), status

def supabase_headers(is_json: bool = False) -> Dict[str, str]:
    key = SUPABASE_SERVICE_ROLE or SUPABASE_ANON_KEY
    if not (SUPABASE_URL and key):
        # não explode aqui; valida no endpoint para mensagem melhor
        key = key or ""
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    if is_json:
        h["Content-Type"] = "application/json"
    return h

def build_base_url(env: str) -> str:
    e = (env or "producao").strip().lower()
    if e in ("restrita", "producao_restrita", "homolog", "homologacao"):
        return "https://adn.producaorestrita.nfse.gov.br"
    return "https://adn.nfse.gov.br"

def build_url(base: str, path_template: str, chave: str) -> str:
    b = (base or "").strip().rstrip("/")
    p = (path_template or "").strip()
    if not p.startswith("/"):
        p = "/" + p
    return b + p.replace("{chave}", chave)

def preview_text(resp: requests.Response, limit: int = 1200) -> str:
    try:
        return (resp.text or "")[:limit]
    except Exception:
        return "<nao foi possivel ler body>"


# ==========================
# Supabase: buscar certificado do user + cnpj
# ==========================
def fetch_cert_row(user_email: str, cnpj: str) -> Dict[str, Any]:
    """
    Busca 1 registro na tabela certifica_dfe pelo user + cnpj/cpf.
    Campos esperados: pem, key (base64).
    """
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL não definido no ambiente.")
    if not (SUPABASE_SERVICE_ROLE or SUPABASE_ANON_KEY):
        raise RuntimeError("Defina SUPABASE_SERVICE_ROLE (recomendado) ou SUPABASE_ANON_KEY no ambiente.")

    url = f"{SUPABASE_URL}/rest/v1/{TABELA_CERTS}"
    params = {
        "select": 'id,empresa,codi,user,"cnpj/cpf",pem,key,vencimento,fazer',
        "user": f"eq.{user_email}",
        '"cnpj/cpf"': f"eq.{cnpj}",
        "limit": "1",
    }

    r = requests.get(url, headers=supabase_headers(), params=params, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Supabase retornou {r.status_code}: {r.text[:500]}")

    data = r.json() or []
    if not data:
        raise RuntimeError("Certificado não encontrado para este user + CNPJ na tabela certifica_dfe.")

    row = data[0]
    if not row.get("pem") or not row.get("key"):
        raise RuntimeError("Registro encontrado, mas campos pem/key estão vazios.")
    return row


def create_temp_cert_files(cert_row: Dict[str, Any]) -> Tuple[str, str, tempfile.TemporaryDirectory]:
    """
    Converte pem/key (base64) em arquivos temporários para requests (mTLS).
    Retorna (cert_path, key_path, tempdir_obj).
    """
    pem_b64 = cert_row.get("pem") or ""
    key_b64 = cert_row.get("key") or ""

    try:
        pem_bytes = base64.b64decode(pem_b64)
        key_bytes = base64.b64decode(key_b64)
    except Exception as e:
        raise RuntimeError(f"Falha ao decodificar base64 do pem/key: {e}")

    td = tempfile.TemporaryDirectory(prefix="mtls_")
    cert_path = os.path.join(td.name, "client_cert.pem")
    key_path = os.path.join(td.name, "client_key.pem")

    with open(cert_path, "wb") as f:
        f.write(pem_bytes)
    with open(key_path, "wb") as f:
        f.write(key_bytes)

    return cert_path, key_path, td


# ==========================
# HTTP: requests Session com Retry (gateway)
# ==========================
def make_session() -> requests.Session:
    s = requests.Session()

    retry = Retry(
        total=0,  # vamos controlar manualmente no loop
        connect=0,
        read=0,
        status=0,
        backoff_factor=0,
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "Accept": "application/pdf",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DANFSeBackend/1.0",
        "Connection": "keep-alive",
    })
    return s


def http_get_with_retry(
    session: requests.Session,
    url: str,
    cert_tuple: Tuple[str, str],
    timeout: int,
    tries: int,
    backoff_base: float,
) -> requests.Response:
    """
    Retry em 502/503/504 + RequestException.
    """
    last_resp: Optional[requests.Response] = None
    last_exc: Optional[Exception] = None

    for i in range(1, max(1, tries) + 1):
        try:
            log.info("DANFSe GET tentativa %s/%s | url=%s", i, tries, url)
            resp = session.get(
                url,
                cert=cert_tuple,
                timeout=timeout,
                allow_redirects=True,
            )
            last_resp = resp

            if resp.status_code in (502, 503, 504):
                wait = backoff_base * i
                log.warning("Gateway %s. Aguardando %.1fs e retry...", resp.status_code, wait)
                time.sleep(wait)
                continue

            return resp

        except requests.RequestException as e:
            last_exc = e
            wait = backoff_base * i
            log.warning("Erro de rede: %s | aguardando %.1fs e retry...", e, wait)
            time.sleep(wait)

    if last_resp is not None:
        return last_resp
    raise last_exc or RuntimeError("Falha desconhecida (sem resposta).")


# ==========================
# Rotas
# ==========================
@app.get("/health")
def health():
    return jsonify({"ok": True, "service": APP_NAME})


@app.post("/danfse/pdf")
def danfse_pdf():
    """
    Espera JSON:
      {
        "user": "email@...",
        "cnpj": "14 digitos",
        "chave": "50 digitos (DANFSe)",
        "env": "producao" | "restrita"   (opcional)
      }
    Retorna application/pdf (bytes) ou JSON erro.
    """
    try:
        body = request.get_json(silent=True) or {}
        user_email = (body.get("user") or "").strip()
        cnpj = only_digits(body.get("cnpj") or "")
        chave = only_digits(body.get("chave") or "")
        env = (body.get("env") or "producao").strip().lower()

        timeout = int(body.get("timeout") or DEFAULT_TIMEOUT)
        tries = int(body.get("tries") or DEFAULT_TRIES)
        backoff = float(body.get("backoff") or DEFAULT_BACKOFF)

        if not user_email or "@" not in user_email:
            return json_error("Campo 'user' (email) é obrigatório.", 400)
        if len(cnpj) != 14:
            return json_error("Campo 'cnpj' deve ter 14 dígitos.", 400)
        if len(chave) != 50:
            return json_error("Campo 'chave' deve ter 50 dígitos (DANFSe).", 400)

        # 1) Busca cert no Supabase
        cert_row = fetch_cert_row(user_email, cnpj)

        # 2) Cria arquivos temporários
        cert_path, key_path, td = create_temp_cert_files(cert_row)

        try:
            # 3) Monta URL
            base = build_base_url(env)
            url = build_url(base, DANFSE_PATH_TEMPLATE, chave)

            # 4) GET com mTLS + retry
            s = make_session()
            resp = http_get_with_retry(
                session=s,
                url=url,
                cert_tuple=(cert_path, key_path),
                timeout=timeout,
                tries=tries,
                backoff_base=backoff,
            )
        finally:
            # sempre limpar os temporários
            td.cleanup()

        ctype = (resp.headers.get("Content-Type") or "").lower()
        content = resp.content or b""

        # Se erro HTTP, devolve JSON com preview (ajuda debug no Bubble)
        if resp.status_code >= 400:
            log.warning("DANFSe erro HTTP %s | ctype=%s | bodyPreview=%s",
                        resp.status_code, ctype, preview_text(resp, 900))
            return json_error(
                f"Servidor DANFSe retornou HTTP {resp.status_code}.",
                502 if resp.status_code in (502, 503, 504) else 400,
                extra={
                    "status": resp.status_code,
                    "content_type": resp.headers.get("Content-Type"),
                    "body_preview": preview_text(resp, 1200),
                    "url": url,
                }
            )

        # Valida PDF
        is_pdf = ("pdf" in ctype) or (content[:4] == b"%PDF")
        if not is_pdf:
            log.warning("Resposta não é PDF | ctype=%s | bodyPreview=%s", ctype, preview_text(resp, 900))
            return json_error(
                "Resposta não parece ser PDF (rota/caminho pode estar errado).",
                400,
                extra={
                    "content_type": resp.headers.get("Content-Type"),
                    "body_preview": preview_text(resp, 1200),
                    "url": url,
                }
            )

        filename = f"DANFSE_{chave[-12:]}.pdf"
        headers = {
            "Content-Type": "application/pdf",
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
        }
        return Response(content, status=200, headers=headers)

    except Exception as e:
        log.exception("Erro inesperado /danfse/pdf: %s", e)
        return json_error(f"Erro inesperado: {e}", 500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
