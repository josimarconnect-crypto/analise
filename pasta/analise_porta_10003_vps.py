from __future__ import annotations

import argparse
import base64
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import time
import traceback
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

import requests
import uvicorn
from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from flask import Flask, jsonify
from starlette.middleware.wsgi import WSGIMiddleware

try:
    from parcelamento import app as parcelamento_app  # type: ignore
except Exception:
    parcelamento_app = None

try:
    from danfe import app as danfe_app  # type: ignore
except Exception:
    danfe_app = None


SUPABASE_URL = os.getenv("SUPABASE_URL", "https://hysrxadnigzqadnlkynq.supabase.co").strip()
SUPABASE_KEY = os.getenv(
    "SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA",
).strip()
TABELA_CERTS = os.getenv("TABELA_CERTS", "certifica_dfe").strip()
DEBUG_ERRORS = os.getenv("DEBUG_ERRORS", "1") == "1"
LOG_FILE = os.getenv("LOG_FILE", "/tmp/analise.log" if os.getenv("RENDER") else "analise.log")
RUNTIME_HOST = (os.getenv("HOST") or os.getenv("BIND_HOST") or "0.0.0.0").strip() or "0.0.0.0"
try:
    RUNTIME_PORT = int(os.getenv("PORT", "10003"))
except ValueError:
    RUNTIME_PORT = 10003

# IP publico usado apenas para montar os links exibidos no painel/log.
# Para a porta ficar acessivel externamente, o servidor deve escutar em 0.0.0.0.
PUBLIC_HOST = (os.getenv("PUBLIC_HOST") or os.getenv("PUBLIC_IP") or "51.81.105.124").strip()
ADMIN_USER = (os.getenv("ADMIN_USER") or os.getenv("PAINEL_USER") or "").strip()
ADMIN_PASSWORD = (os.getenv("ADMIN_PASSWORD") or os.getenv("PAINEL_PASSWORD") or "").strip()
PROXY_HOST = (os.getenv("PROXY_HOST") or os.getenv("PROXY_IP") or "").strip()
PROXY_PORT = (os.getenv("PROXY_PORT") or "").strip()
PROXY_USER = (os.getenv("PROXY_USER") or "").strip()
PROXY_PASSWORD = (os.getenv("PROXY_PASSWORD") or "").strip()
PROXY_SCHEME = (os.getenv("PROXY_SCHEME") or "http").strip().lower() or "http"
APP_START_TIME = datetime.now(timezone.utc)

URL_DET_HOME = "https://detsec.sefin.ro.gov.br/certificados"
URL_ENTRAR = "https://detsec.sefin.ro.gov.br/entrar"
URL_REDIRECT_PORTAL = "https://detsec.sefin.ro.gov.br/contribuinte/notificacoes/redirect_portal"
URL_PORTAL_HOME_DEFAULT = "https://portalcontribuinte.sefin.ro.gov.br/app/home/?exibir_modal=true"
URL_CONSULTA_DEBITOS = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/"
URL_CONSULTA_DEBITOS_LISTA = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/lista.jsp"

BASE_INTERNAMENTO = "https://internamentonotas.sefin.ro.gov.br"
BASE_CONTA_CORRENTE = "https://portalcontribuinte.sefin.ro.gov.br/app/contacorrente/"


logger = logging.getLogger("analise")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)

security = HTTPBasic(auto_error=False)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _admin_auth_enabled() -> bool:
    return bool(ADMIN_USER and ADMIN_PASSWORD)


def _parse_optional_port(value: Any, label: str = "porta") -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        port = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} deve ser um numero inteiro.") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError(f"{label} deve ficar entre 1 e 65535.")
    return str(port)


def _proxy_configured() -> bool:
    return bool(PROXY_HOST)


def _proxy_url() -> str:
    if not PROXY_HOST:
        return ""
    host = PROXY_HOST.strip()
    scheme = PROXY_SCHEME if PROXY_SCHEME in {"http", "https", "socks4", "socks5"} else "http"
    if "://" in host:
        base = host
    else:
        auth = ""
        if PROXY_USER:
            auth = requests.utils.quote(PROXY_USER, safe="")
            if PROXY_PASSWORD:
                auth += ":" + requests.utils.quote(PROXY_PASSWORD, safe="")
            auth += "@"
        if ":" in host and not host.startswith("[") and host.count(":") == 1:
            base = f"{scheme}://{auth}{host}"
        else:
            port = f":{PROXY_PORT}" if PROXY_PORT else ""
            base = f"{scheme}://{auth}{host}{port}"
    return base


def _proxy_dict() -> Optional[Dict[str, str]]:
    url = _proxy_url()
    if not url:
        return None
    return {"http": url, "https": url}


def _proxy_status_text() -> str:
    if not PROXY_HOST:
        return "desativado"
    if PROXY_PORT:
        return f"{PROXY_HOST}:{PROXY_PORT}"
    return PROXY_HOST


def _apply_proxy_config(
    proxy_host: str = "",
    proxy_port: str = "",
    proxy_user: str = "",
    proxy_password: str = "",
    proxy_scheme: str = "http",
    keep_existing_password: bool = False,
) -> None:
    global PROXY_HOST, PROXY_PORT, PROXY_USER, PROXY_PASSWORD, PROXY_SCHEME
    PROXY_HOST = (proxy_host or "").strip()
    PROXY_PORT = _parse_optional_port(proxy_port, "porta do proxy")
    PROXY_USER = (proxy_user or "").strip()
    if PROXY_USER and keep_existing_password and not proxy_password:
        PROXY_PASSWORD = PROXY_PASSWORD or ""
    else:
        PROXY_PASSWORD = proxy_password or ""
    PROXY_SCHEME = (proxy_scheme or "http").strip().lower() or "http"
    if PROXY_SCHEME not in {"http", "https", "socks4", "socks5"}:
        PROXY_SCHEME = "http"
    for key, value in {
        "PROXY_HOST": PROXY_HOST,
        "PROXY_IP": PROXY_HOST,
        "PROXY_PORT": PROXY_PORT,
        "PROXY_USER": PROXY_USER,
        "PROXY_PASSWORD": PROXY_PASSWORD,
        "PROXY_SCHEME": PROXY_SCHEME,
    }.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


def require_admin(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> Optional[str]:
    if not _admin_auth_enabled():
        return None
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Autenticacao do painel requerida.",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(credentials.username or "", ADMIN_USER)
    password_ok = secrets.compare_digest(credentials.password or "", ADMIN_PASSWORD)
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="Usuario ou senha invalidos.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _read_log_tail(max_lines: int = 300) -> str:
    max_lines = max(10, min(int(max_lines or 300), 1000))
    log_path = Path(LOG_FILE)
    if not log_path.exists():
        return f"Arquivo de log ainda nao criado: {LOG_FILE}"
    try:
        with log_path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256_000))
            text = fh.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return f"Nao foi possivel ler o log {LOG_FILE}: {exc}"
    return "\n".join(text.splitlines()[-max_lines:]) or "Sem logs ainda."


def _host_for_local_url(host: str) -> str:
    host = (host or "").strip()
    if host in {"", "0.0.0.0", "::"}:
        return "127.0.0.1"
    return host.strip("[]")


def _panel_url() -> str:
    display_host = PUBLIC_HOST or _host_for_local_url(RUNTIME_HOST)
    return f"http://{display_host}:{RUNTIME_PORT}/painel"


def _runtime_status() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "analise",
        "online": True,
        "date_utc": _now_iso(),
        "started_at_utc": APP_START_TIME.isoformat(),
        "uptime_seconds": int((datetime.now(timezone.utc) - APP_START_TIME).total_seconds()),
        "host": RUNTIME_HOST,
        "public_host": PUBLIC_HOST,
        "port": RUNTIME_PORT,
        "panel_url": _panel_url(),
        "admin_auth": _admin_auth_enabled(),
        "proxy_enabled": _proxy_configured(),
        "proxy": _proxy_status_text(),
        "proxy_host": PROXY_HOST,
        "proxy_port": PROXY_PORT,
        "proxy_user": PROXY_USER,
        "proxy_scheme": PROXY_SCHEME,
        "log_file": LOG_FILE,
    }


def _panel_html() -> str:
    return """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Analise - Painel</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Arial, Helvetica, sans-serif;
      background: #0f172a;
      color: #e5e7eb;
    }
    body {
      margin: 0;
      min-height: 100vh;
      background: #0f172a;
    }
    main {
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      padding: 24px 0;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }
    h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 36px;
      padding: 0 12px;
      border: 1px solid #334155;
      border-radius: 6px;
      background: #111827;
      font-size: 14px;
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #22c55e;
      box-shadow: 0 0 0 4px rgba(34, 197, 94, 0.16);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .metric {
      border: 1px solid #334155;
      border-radius: 6px;
      background: #111827;
      padding: 12px;
      min-height: 76px;
    }
    .metric span {
      display: block;
      color: #94a3b8;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
      margin-bottom: 8px;
    }
    .metric strong {
      display: block;
      font-size: 18px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .config {
      border: 1px solid #334155;
      border-radius: 6px;
      background: #111827;
      padding: 12px;
      margin-bottom: 16px;
    }
    .config form {
      display: grid;
      grid-template-columns: 1.4fr 0.7fr 1fr 1fr 0.7fr auto auto;
      gap: 10px;
      align-items: end;
    }
    .config label {
      display: grid;
      gap: 6px;
      color: #94a3b8;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .config input,
    .config select {
      min-height: 36px;
      border: 1px solid #475569;
      border-radius: 6px;
      background: #020617;
      color: #e5e7eb;
      padding: 0 10px;
      font: 14px Arial, Helvetica, sans-serif;
    }
    .config button {
      min-height: 38px;
      border: 1px solid #64748b;
      border-radius: 6px;
      background: #1f2937;
      color: #f8fafc;
      padding: 0 12px;
      font: 14px Arial, Helvetica, sans-serif;
      cursor: pointer;
    }
    .danger {
      min-height: 36px;
      border: 1px solid #991b1b;
      border-radius: 6px;
      background: #7f1d1d;
      color: #fff;
      padding: 0 12px;
      font: 14px Arial, Helvetica, sans-serif;
      cursor: pointer;
    }
    .message {
      min-height: 20px;
      color: #93c5fd;
      font-size: 13px;
      margin-top: 10px;
    }
    pre {
      margin: 0;
      min-height: 460px;
      max-height: calc(100vh - 240px);
      overflow: auto;
      border: 1px solid #334155;
      border-radius: 6px;
      background: #020617;
      color: #d1d5db;
      padding: 14px;
      font: 13px/1.45 Consolas, "Courier New", monospace;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    @media (max-width: 860px) {
      header {
        align-items: flex-start;
        flex-direction: column;
      }
      .grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .config form {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    @media (max-width: 520px) {
      .grid {
        grid-template-columns: 1fr;
      }
      .config form {
        grid-template-columns: 1fr;
      }
      h1 {
        font-size: 21px;
      }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Analise</h1>
      <div class="status"><span class="dot"></span><span id="state">online</span></div>
      <button class="danger" id="stop-server" type="button">Parar</button>
    </header>
    <section class="grid" aria-label="Status do servidor">
      <div class="metric"><span>Host</span><strong id="host">-</strong></div>
      <div class="metric"><span>Porta</span><strong id="port">-</strong></div>
      <div class="metric"><span>Uptime</span><strong id="uptime">-</strong></div>
      <div class="metric"><span>Autenticacao</span><strong id="auth">-</strong></div>
      <div class="metric"><span>Proxy</span><strong id="proxy">-</strong></div>
    </section>
    <section class="config" aria-label="Configuracao de proxy">
      <form id="proxy-form">
        <label>Proxy IP/Host<input id="proxy-host" autocomplete="off"></label>
        <label>Porta<input id="proxy-port" inputmode="numeric" autocomplete="off"></label>
        <label>Usuario<input id="proxy-user" autocomplete="off"></label>
        <label>Senha<input id="proxy-password" type="password" autocomplete="off"></label>
        <label>Tipo<select id="proxy-scheme">
          <option value="http">http</option>
          <option value="https">https</option>
          <option value="socks4">socks4</option>
          <option value="socks5">socks5</option>
        </select></label>
        <button type="submit">Salvar</button>
        <button type="button" id="proxy-clear">Limpar</button>
      </form>
      <div class="message" id="proxy-message"></div>
    </section>
    <pre id="logs">Carregando logs...</pre>
  </main>
  <script>
    const stateEl = document.getElementById("state");
    const hostEl = document.getElementById("host");
    const portEl = document.getElementById("port");
    const uptimeEl = document.getElementById("uptime");
    const authEl = document.getElementById("auth");
    const proxyEl = document.getElementById("proxy");
    const logsEl = document.getElementById("logs");
    const proxyForm = document.getElementById("proxy-form");
    const proxyHostInput = document.getElementById("proxy-host");
    const proxyPortInput = document.getElementById("proxy-port");
    const proxyUserInput = document.getElementById("proxy-user");
    const proxyPasswordInput = document.getElementById("proxy-password");
    const proxySchemeInput = document.getElementById("proxy-scheme");
    const proxyClearButton = document.getElementById("proxy-clear");
    const proxyMessage = document.getElementById("proxy-message");
    const stopServerButton = document.getElementById("stop-server");
    let proxyFormHydrated = false;

    function formatUptime(seconds) {
      const s = Number(seconds || 0);
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const r = s % 60;
      return `${h}h ${m}m ${r}s`;
    }

    async function refresh() {
      try {
        const statusResp = await fetch("/api/status", { cache: "no-store" });
        const logsResp = await fetch("/api/logs?lines=400", { cache: "no-store" });
        const status = await statusResp.json();
        const logs = await logsResp.text();
        stateEl.textContent = status.online ? "online" : "offline";
        hostEl.textContent = status.host || "-";
        portEl.textContent = String(status.port || "-");
        uptimeEl.textContent = formatUptime(status.uptime_seconds);
        authEl.textContent = status.admin_auth ? "ativa" : "desativada";
        proxyEl.textContent = status.proxy || "-";
        if (!proxyFormHydrated) {
          proxyHostInput.value = status.proxy_host || "";
          proxyPortInput.value = status.proxy_port || "";
          proxyUserInput.value = status.proxy_user || "";
          proxySchemeInput.value = status.proxy_scheme || "http";
          proxyFormHydrated = true;
        }
        logsEl.textContent = logs || "Sem logs ainda.";
      } catch (err) {
        stateEl.textContent = "sem conexao";
        logsEl.textContent = String(err);
      }
    }

    proxyForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      proxyMessage.textContent = "Salvando...";
      try {
        const resp = await fetch("/api/config/proxy", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            proxy_host: proxyHostInput.value.trim(),
            proxy_port: proxyPortInput.value.trim(),
            proxy_user: proxyUserInput.value.trim(),
            proxy_password: proxyPasswordInput.value,
            proxy_scheme: proxySchemeInput.value || "http",
          }),
        });
        if (!resp.ok) {
          throw new Error(await resp.text());
        }
        proxyPasswordInput.value = "";
        proxyMessage.textContent = "Proxy salvo.";
        proxyFormHydrated = false;
        await refresh();
      } catch (err) {
        proxyMessage.textContent = String(err);
      }
    });

    proxyClearButton.addEventListener("click", () => {
      proxyHostInput.value = "";
      proxyPortInput.value = "";
      proxyUserInput.value = "";
      proxyPasswordInput.value = "";
      proxySchemeInput.value = "http";
      proxyForm.requestSubmit();
    });

    stopServerButton.addEventListener("click", async () => {
      if (!confirm("Parar o servidor agora?")) {
        return;
      }
      proxyMessage.textContent = "Parando servidor...";
      try {
        await fetch("/api/shutdown", { method: "POST" });
      } catch (err) {
        proxyMessage.textContent = String(err);
      }
    });

    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>"""


def _cors_headers_for_request(request: Request) -> Dict[str, str]:
    origin = request.headers.get("origin") or "*"
    headers = {"Access-Control-Allow-Origin": origin}
    if origin != "*":
        headers["Vary"] = "Origin"
    return headers


def _require_supabase() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY or SUPABASE_KEY == "CHANGE_ME":
        raise HTTPException(500, "Configure SUPABASE_URL e SUPABASE_KEY no ambiente.")


def supabase_headers() -> Dict[str, str]:
    _require_supabase()
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


def carregar_certificados(user_filter: str) -> List[Dict[str, Any]]:
    url = f"{SUPABASE_URL}/rest/v1/{TABELA_CERTS}"
    params: Dict[str, str] = {
        "select": 'id,pem,key,empresa,codi,user,vencimento,"cnpj/cpf"',
        "user": f"eq.{user_filter}",
        "order": "id.desc",
        "limit": "1000",
    }
    resp = requests.get(url, headers=supabase_headers(), params=params, timeout=30, proxies=_proxy_dict())
    if resp.status_code >= 300:
        raise HTTPException(400, f"Supabase REST falhou: {resp.text}")
    return resp.json() or []


def selecionar_cert_por_codi(certs: List[Dict[str, Any]], codi: str) -> Dict[str, Any]:
    codi = (codi or "").strip()
    if not certs:
        raise HTTPException(404, "Nenhum certificado encontrado para este user.")
    if not codi:
        return certs[0]
    for cert in certs:
        if str(cert.get("codi") or "").strip() == codi:
            return cert
    raise HTTPException(404, f"Nao encontrei certificado com CODI={codi} para este user.")


def criar_arquivos_cert_temp(cert_row: Dict[str, Any]) -> Tuple[str, str]:
    pem_b64 = cert_row.get("pem") or ""
    key_b64 = cert_row.get("key") or ""
    if not pem_b64 or not key_b64:
        raise HTTPException(400, "Certificado invalido: pem/key vazios no Supabase.")
    cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    cert_file.write(base64.b64decode(pem_b64))
    cert_file.close()
    key_file.write(base64.b64decode(key_b64))
    key_file.close()
    return cert_file.name, key_file.name


def criar_sessao(cert_path: str, key_path: str) -> requests.Session:
    sess = requests.Session()
    sess.cert = (cert_path, key_path)
    proxies = _proxy_dict()
    if proxies:
        sess.proxies.update(proxies)
    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return sess


def abrir_acesso_digital_e_entrar(sess: requests.Session) -> bool:
    resp = sess.get(URL_DET_HOME, timeout=30, allow_redirects=True)
    if resp.status_code != 200:
        return False
    soup = BeautifulSoup(resp.text, "lxml")
    form = soup.find("form")
    if not form:
        return False
    action = form.get("action") or URL_ENTRAR
    if not action.startswith("http"):
        action = requests.compat.urljoin(URL_DET_HOME, action)
    entered = sess.get(action, timeout=30, allow_redirects=True)
    if entered.status_code != 200:
        return False
    return "/certificado/acessos" in (entered.url or "")


def _extrair_form_logintoken(html: str) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if "portalcontribuinte.sefin.ro.gov.br" in action or "LoginToken" in action:
            if not action.startswith("http"):
                action = requests.compat.urljoin(URL_REDIRECT_PORTAL, action)
            data = {
                inp.get("name"): inp.get("value", "") or ""
                for inp in form.find_all("input")
                if inp.get("name")
            }
            return action, data
    return None, None


def _extrair_redirect_do_logintoken(html: str) -> Optional[str]:
    match = re.search(
        r"location\s*=\s*['\"](https://portalcontribuinte\.sefin\.ro\.gov\.br[^'\"]+)['\"]",
        html,
    )
    if match:
        return match.group(1)
    match = re.search(r"location\.href\s*=\s*['\"](/app/home[^'\"]*)['\"]", html)
    if match:
        return "https://portalcontribuinte.sefin.ro.gov.br" + match.group(1)
    return None


def ir_para_portal(sess: requests.Session) -> bool:
    redirected = sess.get(URL_REDIRECT_PORTAL, timeout=30, allow_redirects=True)
    if redirected.status_code != 200:
        return False
    action, data = _extrair_form_logintoken(redirected.text)
    if action:
        login = sess.post(action, data=data, timeout=30, allow_redirects=True)
        if login.status_code == 200 and "LoginToken" not in (login.url or ""):
            return True
        if login.status_code == 200 and "LoginToken" in (login.url or ""):
            next_url = _extrair_redirect_do_logintoken(login.text) or URL_PORTAL_HOME_DEFAULT
            home = sess.get(next_url, timeout=30, allow_redirects=True)
            return home.status_code == 200 and "portalcontribuinte.sefin.ro.gov.br" in (home.url or "")
    fallback = sess.get(URL_PORTAL_HOME_DEFAULT, timeout=30, allow_redirects=True)
    return fallback.status_code == 200 and "LoginToken" not in (fallback.url or "")


def _listar_inscricoes_estaduais(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    select = soup.find("select", {"name": "inscricaoEstadual"})
    if not select:
        return []
    out: List[str] = []
    seen = set()
    for option in select.find_all("option"):
        value = (option.get("value") or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def obter_debitos_inscricao_estadual(html_deb: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_deb, "lxml")
    tabela_alvo = None
    for table in soup.find_all("table"):
        headers = table.find_all("th")
        if headers and "DEBITOS NA INSCRICAO ESTADUAL" in _norm_text(headers[0].get_text(" ", strip=True)).upper():
            tabela_alvo = table
            break
    if not tabela_alvo:
        return []

    linhas = tabela_alvo.find_all("tr")
    if len(linhas) <= 2:
        return []

    debitos: List[Dict[str, str]] = []
    for tr in linhas[2:]:
        cols = tr.find_all("td")
        if len(cols) < 11:
            continue

        def txt(idx: int) -> str:
            return cols[idx].get_text(" ", strip=True) if idx < len(cols) else ""

        link_extrato = tr.find("a", href=re.compile(r"extrato\.jsp"))

        def norm_url(href: Optional[str]) -> str:
            if not href:
                return ""
            href = href.replace("%22", "").strip('"')
            if href.startswith("http"):
                return href
            return requests.compat.urljoin(URL_CONSULTA_DEBITOS_LISTA, href)

        debitos.append(
            {
                "nr_lancamento": txt(2),
                "parcela": txt(3),
                "referencia": txt(4),
                "complemento": txt(5),
                "receita": txt(6),
                "situacao": txt(7),
                "data_vencimento": txt(8),
                "valor_lancamento": txt(9),
                "valor_atualizado": txt(10),
                "url_extrato": norm_url(link_extrato.get("href") if link_extrato else ""),
            }
        )
    return debitos


def consultar_debitos_ano(sess: requests.Session, ano: int) -> Tuple[List[Dict[str, str]], Optional[str]]:
    resp = sess.get(URL_CONSULTA_DEBITOS, timeout=30, allow_redirects=True)
    if resp.status_code != 200:
        return [], f"Erro HTTP {resp.status_code} ao abrir Consulta de Debitos"

    soup = BeautifulSoup(resp.text, "lxml")
    input_tipo = soup.find("input", {"name": "tipoDevedor"})
    tipo_devedor = input_tipo.get("value", "1") if input_tipo else "1"
    inscricoes = _listar_inscricoes_estaduais(resp.text)
    if not inscricoes:
        return [], "Nenhuma inscricao estadual disponivel"

    last_err = None
    for ie_val in inscricoes:
        payload = {
            "inscricaoEstadual": ie_val,
            "ano": str(ano),
            "tipoDevedor": tipo_devedor,
            "Submit": "Consultar Debitos",
        }
        listed = sess.post(URL_CONSULTA_DEBITOS_LISTA, data=payload, timeout=30, allow_redirects=True)
        if listed.status_code != 200:
            last_err = f"Erro HTTP {listed.status_code} lista (ano {ano}) IE={ie_val}"
            continue
        debs = obter_debitos_inscricao_estadual(listed.text)
        for item in debs:
            item["ano"] = str(ano)
            item["ie"] = ie_val
        return debs, None

    return [], last_err or f"Falha ao consultar lista (ano {ano})"


def _extrair_token_e_usuario(html_extrato: str) -> Tuple[Optional[str], Optional[str]]:
    token_match = re.search(r"var\s+TOKEN\s*=\s*'([^']+)'", html_extrato)
    token = token_match.group(1).strip() if token_match else None

    user_match = re.search(r"Ol[Ã¡a]\s*<strong>\s*([0-9]{11})\s*-", html_extrato, flags=re.I)
    if user_match:
        return token, user_match.group(1).strip()

    cpf_match = re.search(r"var\s+CPF_CLIENTE\s*=\s*\(\s*'([^']*)'\s*\|\|\s*''\s*\)", html_extrato)
    if cpf_match:
        cpf = re.sub(r"\D", "", cpf_match.group(1) or "")
        if cpf:
            return token, cpf

    return token, None


def _extrair_notas_do_extrato_contacorrente(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    notas: List[Dict[str, Any]] = []
    for tr in soup.find_all("tr"):
        chave_nfe = None

        a_extrato = tr.find("a", {"class": "abrir-cte"})
        if a_extrato:
            value = (a_extrato.get("data-chave") or "").strip()
            if re.fullmatch(r"\d{44}", value):
                chave_nfe = value

        if not chave_nfe:
            cols = tr.find_all("td")
            if cols:
                t0 = (cols[0].get_text(" ", strip=True) or "").strip()
                if re.fullmatch(r"\d{44}", t0):
                    chave_nfe = t0

        if not chave_nfe:
            line_txt = tr.get_text(" ", strip=True) or ""
            match = re.search(r"\b(\d{44})\b", line_txt)
            if match:
                chave_nfe = match.group(1)

        if not chave_nfe or not re.fullmatch(r"\d{44}", chave_nfe):
            continue

        cte_modal_rel = ""
        for anchor in tr.find_all("a"):
            onclick = anchor.get("onclick") or ""
            match = re.search(r"abrirModal\(\s*'([^']*cteconsulta\.jsp\?chave=\d{44}[^']*)'\s*\)", onclick)
            if match:
                cte_modal_rel = match.group(1).strip()
                break

        notas.append({"chave_nfe": chave_nfe, "cte_modal_rel": cte_modal_rel})

    out: List[Dict[str, Any]] = []
    seen = set()
    for nota in notas:
        chave = nota["chave_nfe"]
        if chave in seen:
            continue
        seen.add(chave)
        out.append(nota)
    return out


def _url_capa_internamento(usuario: str, chave: str, token: Optional[str]) -> str:
    base = f"{BASE_INTERNAMENTO}/capa_internamentos/{usuario}/{chave}"
    if not token:
        return base
    return base + "?token=" + requests.utils.quote(token, safe="")


def _resolver_url_cteconsulta(rel: str) -> str:
    if not rel:
        return ""
    if rel.startswith("http"):
        return rel
    return requests.compat.urljoin(BASE_CONTA_CORRENTE, rel)


def _extrair_chave_da_query(url: str) -> Optional[str]:
    if not url:
        return None
    match = re.search(r"[?&]chave=(\d{44})", url)
    return match.group(1) if match else None


def _buscar_chaves_cte(sess: requests.Session, url_cteconsulta: str) -> List[str]:
    if not url_cteconsulta:
        return []

    chave_consultada = _extrair_chave_da_query(url_cteconsulta)
    try:
        resp = sess.get(url_cteconsulta, timeout=45, allow_redirects=True)
        if resp.status_code != 200:
            return []
        chaves = re.findall(r"\b\d{44}\b", resp.text or "")
        out: List[str] = []
        seen = set()
        for chave in chaves:
            if chave_consultada and chave == chave_consultada:
                continue
            if chave in seen:
                continue
            seen.add(chave)
            out.append(chave)
        return out
    except Exception:
        return []


def _norm_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    for src, dst in [
        ("Ã¡", "a"),
        ("Ã ", "a"),
        ("Ã¢", "a"),
        ("Ã£", "a"),
        ("Ã©", "e"),
        ("Ãª", "e"),
        ("Ã¨", "e"),
        ("Ã­", "i"),
        ("Ã®", "i"),
        ("Ã³", "o"),
        ("Ã´", "o"),
        ("Ãµ", "o"),
        ("Ãº", "u"),
        ("Ã»", "u"),
        ("Ã§", "c"),
    ]:
        normalized = normalized.replace(src, dst)
    return normalized


def _limpar_header(header: str) -> str:
    return re.sub(r"\s+", " ", (header or "").strip())


def _force_bc_icms_from_col22(row: Dict[str, Any], headers_cut: List[str], cols_cut: List[str]) -> None:
    key_target = "Valor Base Calc. ICMS"
    key_source = "Valor Sub Total (BC-01)"
    src_val = row.get(key_source)
    if src_val is None:
        src_val = cols_cut[21].strip() if len(cols_cut) > 21 else None
    if src_val is None:
        return
    row[key_target] = (str(src_val).strip() if src_val is not None else None) or None
    try:
        idx = headers_cut.index(key_target)
        if idx < len(cols_cut):
            cols_cut[idx] = row[key_target] or ""
    except ValueError:
        pass


def _parse_itens_da_nota_primeiras_10_colunas(soup: BeautifulSoup) -> Dict[str, Any]:
    heading = None
    for item in soup.find_all(["h4", "h3", "h2"]):
        if "itens da nota" in _norm_text(item.get_text(" ", strip=True)):
            heading = item
            break

    if not heading:
        return {
            "tabela_encontrada": False,
            "motivo": "heading 'Itens da nota' nao encontrado",
            "headers": [],
            "itens": [],
            "totais": {"qtd_linhas": 0, "qtd_colunas": 0},
            "diagnostico": {"headers_full_n": 0, "headers_cut_n": 0},
        }

    table = heading.find_next("table")
    if not table:
        return {
            "tabela_encontrada": False,
            "motivo": "table apos heading 'Itens da nota' nao encontrada",
            "headers": [],
            "itens": [],
            "totais": {"qtd_linhas": 0, "qtd_colunas": 0},
            "diagnostico": {"headers_full_n": 0, "headers_cut_n": 0},
        }

    headers_full = [_limpar_header(th.get_text(" ", strip=True)) for th in table.find_all("th")]
    headers_cut = headers_full[:22]
    itens: List[Dict[str, Any]] = []

    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        cols_full = [td.get_text(" ", strip=True) for td in tds]
        if len(cols_full) < 2:
            continue
        cols_cut = cols_full[:22]
        item0 = cols_cut[0].strip() if cols_cut else ""
        if not re.fullmatch(r"\d+", item0 or ""):
            continue

        row: Dict[str, Any] = {"cols_raw": cols_cut}
        for idx, header in enumerate(headers_cut):
            row[header] = (cols_cut[idx].strip() if idx < len(cols_cut) else None) or None

        _force_bc_icms_from_col22(row, headers_cut, cols_cut)
        row["cols_raw"] = cols_cut
        itens.append(row)

    return {
        "tabela_encontrada": True,
        "motivo": None,
        "headers": headers_cut,
        "itens": itens,
        "totais": {"qtd_linhas": len(itens), "qtd_colunas": len(headers_cut)},
        "diagnostico": {"headers_full_n": len(headers_full), "headers_cut_n": len(headers_cut)},
    }


def parse_internamento(html_intern: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html_intern, "lxml")
    return _parse_itens_da_nota_primeiras_10_colunas(soup)


app = FastAPI(title="analise")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup_log() -> None:
    logger.info(
        "START | service=analise | host=%s | port=%s | painel=%s | auth=%s | proxy=%s",
        RUNTIME_HOST,
        RUNTIME_PORT,
        _panel_url(),
        "on" if _admin_auth_enabled() else "off",
        _proxy_status_text(),
    )


@app.middleware("http")
async def _access_log(request: Request, call_next):
    started = time.perf_counter()
    path = request.url.path
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("HTTP | %s %s | failed", request.method, path)
        raise
    if path not in {"/api/logs", "/api/status", "/favicon.ico"}:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info("HTTP | %s %s | %s | %.1fms", request.method, path, response.status_code, elapsed_ms)
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    try:
        logger.error(
            "UNHANDLED | path=%s | query=%s | err=%s\n%s",
            str(request.url.path),
            dict(request.query_params),
            str(exc),
            traceback.format_exc(),
        )
    except Exception:
        pass

    status_code = 500
    if isinstance(exc, requests.exceptions.Timeout):
        status_code = 504
    elif isinstance(exc, requests.exceptions.RequestException):
        status_code = 502

    payload: Dict[str, Any] = {"ok": False, "error": str(exc)}
    if isinstance(exc, requests.exceptions.RequestException):
        payload["detail"] = "Falha ao conectar em servico externo usado pela consulta."
    if DEBUG_ERRORS:
        payload.update(
            {
                "type": exc.__class__.__name__,
                "path": str(request.url.path),
                "query": dict(request.query_params),
                "traceback": traceback.format_exc(),
                "log_file": LOG_FILE,
            }
        )
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers=_cors_headers_for_request(request),
    )


@app.get("/")
def root() -> Dict[str, Any]:
    routes = [
        "/health",
        "/painel",
        "/api/status",
        "/api/logs",
        "/api/config/proxy",
        "/api/shutdown",
        "/empresas",
        "/debitos",
        "/extrato-produto",
    ]
    if danfe_app is not None:
        routes.extend(
            [
                "/danfe/health",
                "/danfe/danfse/pdf",
            ]
        )
    if parcelamento_app is not None:
        routes.extend(
            [
                "/integra-parcelamento/health",
                "/integra-parcelamento/parcelamentos/consultar",
                "/integra-parcelamento/parcelamentos/emitir",
                "/integra-parcelamento/parcelamentos/consultar-com-sitfis",
                "/integra-parcelamento/situacao/consultar",
            ]
        )
    return {"ok": True, "service": "analise", "date_utc": _now_iso(), "routes": routes}


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "date_utc": _now_iso()}


@app.get("/painel", response_class=HTMLResponse)
def painel(_: Optional[str] = Depends(require_admin)) -> HTMLResponse:
    return HTMLResponse(_panel_html())


@app.get("/api/status")
def api_status(_: Optional[str] = Depends(require_admin)) -> Dict[str, Any]:
    return _runtime_status()


@app.get("/api/logs", response_class=PlainTextResponse)
def api_logs(lines: int = Query(300, ge=10, le=1000), _: Optional[str] = Depends(require_admin)) -> PlainTextResponse:
    return PlainTextResponse(_read_log_tail(lines))


@app.post("/api/config/proxy")
async def api_config_proxy(request: Request, _: Optional[str] = Depends(require_admin)) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "JSON invalido para configurar proxy.") from exc

    proxy_host = str(payload.get("proxy_host") or "").strip()
    proxy_port = str(payload.get("proxy_port") or "").strip()
    proxy_user = str(payload.get("proxy_user") or "").strip()
    proxy_password = str(payload.get("proxy_password") or "")
    proxy_scheme = str(payload.get("proxy_scheme") or "http").strip().lower()

    try:
        if proxy_host:
            _apply_proxy_config(
                proxy_host,
                proxy_port,
                proxy_user,
                proxy_password,
                proxy_scheme,
                keep_existing_password=True,
            )
        else:
            _apply_proxy_config("", "", "", "", "http")
    except argparse.ArgumentTypeError as exc:
        raise HTTPException(400, str(exc)) from exc

    logger.info("CONFIG | proxy=%s", _proxy_status_text())
    return _runtime_status()


@app.post("/api/shutdown")
def api_shutdown(_: Optional[str] = Depends(require_admin)) -> Dict[str, Any]:
    logger.info("SHUTDOWN | requested from painel")

    def _exit_process() -> None:
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=_exit_process, name="analise-shutdown", daemon=True).start()
    return {"ok": True, "message": "Servidor encerrando."}


@app.get("/empresas")
def empresas(user: str = Query(...)) -> Dict[str, Any]:
    if not user or "@" not in user:
        raise HTTPException(400, "user invalido.")

    certs = carregar_certificados(user)
    out: List[Dict[str, Any]] = []
    seen = set()
    for cert in certs:
        codi = str(cert.get("codi") or "").strip()
        if not codi or codi in seen:
            continue
        seen.add(codi)
        out.append(
            {
                "codi": codi,
                "empresa": (cert.get("empresa") or "").strip(),
                "cnpj": (cert.get("cnpj/cpf") or "").strip(),
                "vencimento": cert.get("vencimento") or "",
            }
        )
    return {"ok": True, "user": user, "total": len(out), "empresas": out}


@app.get("/debitos")
def route_debitos(
    user: str = Query(...),
    codi: str = Query(...),
    incluir_ano_anterior: int = Query(1),
) -> Dict[str, Any]:
    if not user or "@" not in user:
        raise HTTPException(400, "user invalido.")
    if not codi:
        raise HTTPException(400, "codi obrigatorio.")

    certs = carregar_certificados(user)
    cert = selecionar_cert_por_codi(certs, codi)
    cert_path = None
    key_path = None

    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(401, "Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(401, "Falha ao abrir Portal.")

        ano_atual = date.today().year
        anos = [ano_atual] + ([ano_atual - 1] if incluir_ano_anterior == 1 else [])

        all_debs: List[Dict[str, str]] = []
        for ano in anos:
            debs, _ = consultar_debitos_ano(sess, ano)
            all_debs.extend(debs or [])

        return {
            "ok": True,
            "user": user,
            "codi": str(cert.get("codi") or ""),
            "empresa": cert.get("empresa") or "",
            "debitos": all_debs,
        }
    finally:
        for path in (cert_path, key_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


@app.get("/extrato-produto")
def extrato_produto(
    user: str = Query(...),
    codi: str = Query(...),
    url_extrato: str = Query(...),
    chave: str = Query(""),
    max_notas: int = Query(200),
    buscar_cte: int = Query(1),
) -> Dict[str, Any]:
    if not user or "@" not in user:
        raise HTTPException(400, "user invalido.")
    if not codi:
        raise HTTPException(400, "codi obrigatorio.")
    if not url_extrato.startswith("http"):
        raise HTTPException(400, "url_extrato deve comecar com http/https.")

    max_notas = max(1, min(max_notas, 500))
    certs = carregar_certificados(user)
    cert = selecionar_cert_por_codi(certs, codi)

    cert_path = None
    key_path = None

    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(401, "Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(401, "Falha ao abrir Portal (LoginToken/home).")

        url_extrato_clean = url_extrato.strip().replace("%22", "").split("#", 1)[0]
        resp = sess.get(url_extrato_clean, timeout=60, allow_redirects=True)
        if resp.status_code != 200:
            raise HTTPException(400, f"Erro ao abrir extrato.jsp: HTTP {resp.status_code}")

        token, usuario = _extrair_token_e_usuario(resp.text)
        if not usuario:
            raise HTTPException(400, "Nao consegui extrair USUARIO/CPF do extrato.jsp.")

        soup_extrato = BeautifulSoup(resp.text, "lxml")
        notas_meta = _extrair_notas_do_extrato_contacorrente(soup_extrato)

        chave = (chave or "").strip()
        if chave:
            if not re.fullmatch(r"\d{44}", chave):
                raise HTTPException(400, "chave invalida: deve ter 44 digitos.")
            found = [nota for nota in notas_meta if nota["chave_nfe"] == chave]
            notas_meta = found if found else [{"chave_nfe": chave, "cte_modal_rel": ""}]

        if not notas_meta:
            return {
                "ok": True,
                "user": user,
                "codi": str(cert.get("codi") or ""),
                "empresa": cert.get("empresa") or "",
                "result": {
                    "ok": True,
                    "final_url_extrato": resp.url,
                    "token_found": bool(token),
                    "usuario": usuario,
                    "total_notas_nfe": 0,
                    "notas_nfe": [],
                    "cte_chaves_total": [],
                },
            }

        notas_meta = notas_meta[:max_notas]
        notas_out: List[Dict[str, Any]] = []
        cte_total_set = set()

        for meta in notas_meta:
            chave_nfe = meta["chave_nfe"]
            url_capa = _url_capa_internamento(usuario, chave_nfe, token)

            try:
                internamento = sess.get(url_capa, timeout=70, allow_redirects=True)
                if internamento.status_code != 200:
                    itens_payload = {
                        "ok": False,
                        "http_status": internamento.status_code,
                        "final_url": internamento.url,
                        "tabela_encontrada": False,
                        "motivo": f"Falha ao abrir internamento: HTTP {internamento.status_code}",
                        "headers": [],
                        "itens": [],
                        "totais": {"qtd_linhas": 0, "qtd_colunas": 0},
                        "diagnostico": {"headers_full_n": 0, "headers_cut_n": 0},
                    }
                else:
                    parsed = parse_internamento(internamento.text)
                    itens_payload = {
                        "ok": True,
                        "http_status": internamento.status_code,
                        "final_url": internamento.url,
                        **parsed,
                    }
            except Exception as exc:
                itens_payload = {
                    "ok": False,
                    "http_status": None,
                    "final_url": url_capa,
                    "tabela_encontrada": False,
                    "motivo": f"Erro ao abrir/parsear internamento: {exc}",
                    "headers": [],
                    "itens": [],
                    "totais": {"qtd_linhas": 0, "qtd_colunas": 0},
                    "diagnostico": {"headers_full_n": 0, "headers_cut_n": 0},
                }

            cte_url = ""
            cte_chaves: List[str] = []
            if buscar_cte == 1:
                cte_url = _resolver_url_cteconsulta(meta.get("cte_modal_rel") or "")
                cte_chaves = _buscar_chaves_cte(sess, cte_url)
                for cte in cte_chaves:
                    cte_total_set.add(cte)

            notas_out.append(
                {
                    "chave_nfe": chave_nfe,
                    "internamento_url": url_capa,
                    "cte_url": cte_url,
                    "cte_chaves": cte_chaves,
                    "itens_da_nota": itens_payload,
                }
            )

        return {
            "ok": True,
            "user": user,
            "codi": str(cert.get("codi") or ""),
            "empresa": cert.get("empresa") or "",
            "result": {
                "ok": True,
                "final_url_extrato": resp.url,
                "token_found": bool(token),
                "usuario": usuario,
                "total_notas_nfe": len(notas_out),
                "notas_nfe": notas_out,
                "cte_chaves_total": sorted(list(cte_total_set)),
            },
        }
    finally:
        for path in (cert_path, key_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


if danfe_app is not None:
    app.mount("/danfe", WSGIMiddleware(danfe_app))
else:
    danfe_fallback = Flask("danfe_fallback")

    @danfe_fallback.get("/health")
    def _danfe_health_fallback():
        return jsonify({"ok": False, "error": "Modulo danfe nao disponivel."}), 503

    app.mount("/danfe", WSGIMiddleware(danfe_fallback))


if parcelamento_app is not None:
    app.mount("", WSGIMiddleware(parcelamento_app))


def _parse_port_value(value: Any) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("porta deve ser um numero inteiro.") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("porta deve ficar entre 1 e 65535.")
    return port


def _apply_runtime_config(
    host: str,
    port: int,
    admin_user: str = "",
    admin_password: str = "",
    public_host: str = "",
    proxy_host: str = "",
    proxy_port: str = "",
    proxy_user: str = "",
    proxy_password: str = "",
    proxy_scheme: str = "http",
) -> None:
    global RUNTIME_HOST, RUNTIME_PORT, ADMIN_USER, ADMIN_PASSWORD, PUBLIC_HOST
    RUNTIME_HOST = (host or "0.0.0.0").strip() or "0.0.0.0"
    RUNTIME_PORT = _parse_port_value(port)
    PUBLIC_HOST = (public_host or PUBLIC_HOST or "").strip()
    ADMIN_USER = (admin_user or "").strip()
    ADMIN_PASSWORD = admin_password or ""
    _apply_proxy_config(proxy_host, proxy_port, proxy_user, proxy_password, proxy_scheme)
    os.environ["HOST"] = RUNTIME_HOST
    os.environ["PORT"] = str(RUNTIME_PORT)
    if PUBLIC_HOST:
        os.environ["PUBLIC_HOST"] = PUBLIC_HOST
    if ADMIN_USER:
        os.environ["ADMIN_USER"] = ADMIN_USER
    if ADMIN_PASSWORD:
        os.environ["ADMIN_PASSWORD"] = ADMIN_PASSWORD
    if PROXY_HOST:
        os.environ["PROXY_HOST"] = PROXY_HOST
    if PROXY_PORT:
        os.environ["PROXY_PORT"] = PROXY_PORT
    if PROXY_USER:
        os.environ["PROXY_USER"] = PROXY_USER
    if PROXY_PASSWORD:
        os.environ["PROXY_PASSWORD"] = PROXY_PASSWORD
    if PROXY_SCHEME:
        os.environ["PROXY_SCHEME"] = PROXY_SCHEME


def _build_uvicorn_log_config() -> Dict[str, Any]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "plain": {
                "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "plain",
                "stream": "ext://sys.stderr",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "plain",
                "filename": LOG_FILE,
                "maxBytes": 2_000_000,
                "backupCount": 3,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default", "file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["default", "file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["default", "file"], "level": "INFO", "propagate": False},
        },
    }


def run_server(
    host: str,
    port: int,
    admin_user: str = "",
    admin_password: str = "",
    public_host: str = "",
    proxy_host: str = "",
    proxy_port: str = "",
    proxy_user: str = "",
    proxy_password: str = "",
    proxy_scheme: str = "http",
    open_browser: bool = False,
) -> None:
    _apply_runtime_config(
        host,
        port,
        admin_user,
        admin_password,
        public_host,
        proxy_host,
        proxy_port,
        proxy_user,
        proxy_password,
        proxy_scheme,
    )
    logger.info(
        "RUN | host=%s | port=%s | painel=%s | auth=%s | proxy=%s",
        RUNTIME_HOST,
        RUNTIME_PORT,
        _panel_url(),
        "on" if _admin_auth_enabled() else "off",
        _proxy_status_text(),
    )
    logger.info(
        "PAINEL | local=%s | rede=http://%s:%s/painel | gui=python analise.py --gui",
        _panel_url(),
        PUBLIC_HOST or "IP_DA_VPS",
        RUNTIME_PORT,
    )
    if open_browser:
        def _open_panel_browser() -> None:
            try:
                import webbrowser

                webbrowser.open(_panel_url())
            except Exception as exc:
                logger.warning("Nao foi possivel abrir o navegador automaticamente: %s", exc)

        threading.Timer(1.0, _open_panel_browser).start()
    uvicorn.run(
        app,
        host=RUNTIME_HOST,
        port=RUNTIME_PORT,
        reload=False,
        log_config=_build_uvicorn_log_config(),
    )


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Servidor da API Analise.")
    parser.add_argument("--host", "--ip", dest="host", default=RUNTIME_HOST, help="IP/host para abrir a porta.")
    parser.add_argument("-p", "--port", dest="port", type=_parse_port_value, default=RUNTIME_PORT, help="Porta da API.")
    parser.add_argument("--public-host", "--public-ip", dest="public_host", default=PUBLIC_HOST, help="IP publico usado nos links do painel.")
    parser.add_argument("--admin-user", default=ADMIN_USER, help="Usuario do painel web.")
    parser.add_argument("--admin-password", default=ADMIN_PASSWORD, help="Senha do painel web.")
    parser.add_argument("--proxy-host", "--proxy-ip", dest="proxy_host", default=PROXY_HOST, help="IP/host do proxy.")
    parser.add_argument("--proxy-port", default=PROXY_PORT, type=lambda value: _parse_optional_port(value, "porta do proxy"), help="Porta do proxy.")
    parser.add_argument("--proxy-user", default=PROXY_USER, help="Usuario do proxy.")
    parser.add_argument("--proxy-password", default=PROXY_PASSWORD, help="Senha do proxy.")
    parser.add_argument("--proxy-scheme", default=PROXY_SCHEME, choices=["http", "https", "socks4", "socks5"], help="Protocolo do proxy.")
    parser.add_argument("--gui", action="store_true", help="Abre uma janela grafica para iniciar e ver logs.")
    parser.add_argument("--open-browser", action="store_true", help="Abre o painel web no navegador apos iniciar.")
    return parser.parse_args(argv)


def launch_gui() -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as exc:
        print(f"Nao foi possivel abrir a interface grafica: {exc}")
        run_server(RUNTIME_HOST, RUNTIME_PORT, ADMIN_USER, ADMIN_PASSWORD, PUBLIC_HOST, open_browser=True)
        return

    try:
        root = tk.Tk()
    except Exception as exc:
        print(f"Nao foi possivel iniciar a janela grafica: {exc}")
        run_server(RUNTIME_HOST, RUNTIME_PORT, ADMIN_USER, ADMIN_PASSWORD, PUBLIC_HOST, open_browser=True)
        return

    root.title("Analise - Servidor")
    root.geometry("920x700")
    state: Dict[str, Any] = {"server": None, "thread": None}

    host_var = tk.StringVar(value=RUNTIME_HOST)
    port_var = tk.StringVar(value=str(RUNTIME_PORT))
    user_var = tk.StringVar(value=ADMIN_USER)
    password_var = tk.StringVar(value=ADMIN_PASSWORD)
    proxy_host_var = tk.StringVar(value=PROXY_HOST)
    proxy_port_var = tk.StringVar(value=PROXY_PORT)
    proxy_user_var = tk.StringVar(value=PROXY_USER)
    proxy_password_var = tk.StringVar(value=PROXY_PASSWORD)
    proxy_scheme_var = tk.StringVar(value=PROXY_SCHEME)
    status_var = tk.StringVar(value="Parado")

    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)
    frame.columnconfigure(3, weight=1)
    frame.rowconfigure(7, weight=1)

    ttk.Label(frame, text="IP/Host").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(frame, textvariable=host_var).grid(row=0, column=1, sticky="ew", pady=4)
    ttk.Label(frame, text="Porta").grid(row=0, column=2, sticky="w", padx=(12, 8), pady=4)
    ttk.Entry(frame, textvariable=port_var, width=12).grid(row=0, column=3, sticky="ew", pady=4)

    ttk.Label(frame, text="Usuario").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(frame, textvariable=user_var).grid(row=1, column=1, sticky="ew", pady=4)
    ttk.Label(frame, text="Senha").grid(row=1, column=2, sticky="w", padx=(12, 8), pady=4)
    ttk.Entry(frame, textvariable=password_var, show="*").grid(row=1, column=3, sticky="ew", pady=4)

    ttk.Label(frame, text="Proxy IP/Host").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(frame, textvariable=proxy_host_var).grid(row=2, column=1, sticky="ew", pady=4)
    ttk.Label(frame, text="Proxy Porta").grid(row=2, column=2, sticky="w", padx=(12, 8), pady=4)
    ttk.Entry(frame, textvariable=proxy_port_var, width=12).grid(row=2, column=3, sticky="ew", pady=4)

    ttk.Label(frame, text="Proxy Usuario").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(frame, textvariable=proxy_user_var).grid(row=3, column=1, sticky="ew", pady=4)
    ttk.Label(frame, text="Proxy Senha").grid(row=3, column=2, sticky="w", padx=(12, 8), pady=4)
    ttk.Entry(frame, textvariable=proxy_password_var, show="*").grid(row=3, column=3, sticky="ew", pady=4)

    ttk.Label(frame, text="Proxy Tipo").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
    proxy_scheme = ttk.Combobox(
        frame,
        textvariable=proxy_scheme_var,
        values=("http", "https", "socks4", "socks5"),
        state="readonly",
        width=10,
    )
    proxy_scheme.grid(row=4, column=1, sticky="w", pady=4)

    buttons = ttk.Frame(frame)
    buttons.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(8, 4))
    buttons.columnconfigure(3, weight=1)

    def start_server() -> None:
        thread = state.get("thread")
        if thread is not None and thread.is_alive():
            messagebox.showinfo("Analise", "Servidor ja esta ativo.")
            return
        try:
            port = _parse_port_value(port_var.get())
        except argparse.ArgumentTypeError as exc:
            messagebox.showerror("Porta invalida", str(exc))
            return
        try:
            proxy_port = _parse_optional_port(proxy_port_var.get(), "porta do proxy")
        except argparse.ArgumentTypeError as exc:
            messagebox.showerror("Proxy invalido", str(exc))
            return
        admin_user = user_var.get().strip()
        admin_password = password_var.get()
        if not admin_user or not admin_password:
            messagebox.showerror("Dados incompletos", "Informe usuario e senha do painel.")
            return

        _apply_runtime_config(
            host_var.get(),
            port,
            admin_user,
            admin_password,
            PUBLIC_HOST,
            proxy_host_var.get(),
            proxy_port,
            proxy_user_var.get(),
            proxy_password_var.get(),
            proxy_scheme_var.get(),
        )
        config = uvicorn.Config(
            app,
            host=RUNTIME_HOST,
            port=RUNTIME_PORT,
            reload=False,
            log_config=_build_uvicorn_log_config(),
        )
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, name="analise-uvicorn", daemon=True)
        state["server"] = server
        state["thread"] = server_thread
        logger.info(
            "GUI | start | host=%s | port=%s | painel=%s | proxy=%s",
            RUNTIME_HOST,
            RUNTIME_PORT,
            _panel_url(),
            _proxy_status_text(),
        )
        server_thread.start()
        status_var.set(f"Online em {_panel_url()}")

    def stop_server() -> None:
        server = state.get("server")
        if server is not None:
            logger.info("GUI | stop requested")
            server.should_exit = True
            status_var.set("Parando...")

    def open_panel() -> None:
        import webbrowser

        webbrowser.open(_panel_url())

    ttk.Button(buttons, text="Iniciar", command=start_server).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text="Parar", command=stop_server).grid(row=0, column=1, padx=(0, 8))
    ttk.Button(buttons, text="Abrir painel", command=open_panel).grid(row=0, column=2, padx=(0, 8))
    ttk.Label(buttons, textvariable=status_var).grid(row=0, column=3, sticky="e")

    ttk.Label(frame, text=f"Log: {LOG_FILE}").grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 4))
    logs = tk.Text(frame, wrap="word", height=24, bg="#020617", fg="#e5e7eb", insertbackground="#e5e7eb")
    logs.grid(row=7, column=0, columnspan=4, sticky="nsew")
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=logs.yview)
    scrollbar.grid(row=7, column=4, sticky="ns")
    logs.configure(yscrollcommand=scrollbar.set, state="disabled")

    def refresh_logs() -> None:
        thread = state.get("thread")
        server = state.get("server")
        if thread is not None and server is not None and not thread.is_alive() and not server.should_exit:
            status_var.set("Parado. Verifique os logs.")
            state["server"] = None
        logs.configure(state="normal")
        logs.delete("1.0", "end")
        logs.insert("end", _read_log_tail(500))
        logs.see("end")
        logs.configure(state="disabled")
        root.after(1500, refresh_logs)

    def on_close() -> None:
        stop_server()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_logs()
    root.mainloop()


def main(argv: Optional[List[str]] = None) -> None:
    raw_args = sys.argv[1:] if argv is None else argv
    # Sem argumentos, inicia direto como servidor na porta padrao 10003.
    # Use --gui se quiser abrir a interface grafica.
    if not raw_args and os.name == "nt" and not os.getenv("RENDER"):
        run_server(RUNTIME_HOST, RUNTIME_PORT, ADMIN_USER, ADMIN_PASSWORD, PUBLIC_HOST)
        return

    args = _parse_args(raw_args)
    if args.gui:
        launch_gui()
        return
    run_server(
        args.host,
        args.port,
        args.admin_user,
        args.admin_password,
        args.public_host,
        args.proxy_host,
        args.proxy_port,
        args.proxy_user,
        args.proxy_password,
        args.proxy_scheme,
        args.open_browser,
    )


if __name__ == "__main__":
    main()
