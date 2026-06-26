from __future__ import annotations

import base64
import logging
import os
import re
import tempfile
import traceback
import unicodedata
import threading
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from flask import Flask, jsonify
from starlette.middleware.wsgi import WSGIMiddleware

try:
    # Inclui rotas de integra-contador e apoio de cadastro sem certificado.
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

# =========================================================
# CONFIGURAÇÃO WEBSHARE / PROXY ROTATIVO
# =========================================================
WEBSHARE_API_TOKEN = os.getenv(
    "WEBSHARE_API_TOKEN",
    "3lmjgsitbybejnavcv9qm3pwzhmfh4seer0xgo20",
).strip()
USAR_POOL_WEBSHARE = os.getenv("USAR_POOL_WEBSHARE", "1").strip() not in ("0", "false", "False", "nao", "não")
WEBSHARE_PAGE_SIZE = int(os.getenv("WEBSHARE_PAGE_SIZE", "100"))
PROXY_CONNECT_TIMEOUT = int(os.getenv("PROXY_CONNECT_TIMEOUT", "20"))
PROXY_READ_TIMEOUT = int(os.getenv("PROXY_READ_TIMEOUT", "60"))

_PROXY_ROTATOR_GLOBAL = None
_PROXY_ROTATOR_LOCK = threading.RLock()

URL_DET_HOME = "https://detsec.sefin.ro.gov.br/certificados"
URL_ENTRAR = "https://detsec.sefin.ro.gov.br/entrar"
URL_REDIRECT_PORTAL = "https://detsec.sefin.ro.gov.br/contribuinte/notificacoes/redirect_portal"
URL_PORTAL_HOME_DEFAULT = "https://portalcontribuinte.sefin.ro.gov.br/app/home/?exibir_modal=true"
URL_CONSULTA_DEBITOS = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/"
URL_CONSULTA_DEBITOS_LISTA = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/lista.jsp"

BASE_INTERNAMENTO = "https://internamentonotas.sefin.ro.gov.br"
BASE_CONTA_CORRENTE = "https://portalcontribuinte.sefin.ro.gov.br/app/contacorrente/"
URL_NCM_REFORMA = "https://mvsistema.com/BuscaNCM/conexao/proxy_ncm.php"


logger = logging.getLogger("analise")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_proxy_url(proxy_url: str) -> str:
    """
    Esconde usuário/senha do proxy nos logs.
    Ex.: http://user:pass@ip:porta -> http://***:***@ip:porta
    """
    if not proxy_url:
        return proxy_url
    return re.sub(r"(https?://)([^:@/]+):([^@/]+)@", r"\1***:***@", str(proxy_url))


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
    resp = requests.get(url, headers=supabase_headers(), params=params, timeout=30)
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



# =========================================================
# WEBSHARE / PROXY ROTATIVO
# =========================================================
class ProxyRotator:
    def __init__(self, proxies_list: List[str]):
        self.proxies = proxies_list.copy()
        self.index = 0
        self.lock = threading.RLock()
        self.falhas_por_proxy: Dict[str, int] = {}
        self.proxy_atual: Optional[str] = None
        logger.info("ProxyRotator inicializado com %s proxies.", len(proxies_list))

    def _format_proxy(self, proxy: str) -> Dict[str, str]:
        proxy = (proxy or "").strip()
        if proxy.startswith("http://") or proxy.startswith("https://"):
            proxy_url = proxy
        else:
            proxy_url = f"http://{proxy}"
        return {"http": proxy_url, "https": proxy_url}

    def get_current_proxy(self) -> Optional[Dict[str, str]]:
        if not self.proxies:
            return None
        with self.lock:
            if not self.proxy_atual:
                proxy = self.proxies[self.index % len(self.proxies)]
                self.index += 1
                self.proxy_atual = proxy
                logger.info("Proxy inicial selecionado: %s", proxy)
            return self._format_proxy(self.proxy_atual)

    def get_next_proxy(self) -> Optional[Dict[str, str]]:
        if not self.proxies:
            return None
        with self.lock:
            proxy = self.proxies[self.index % len(self.proxies)]
            self.index += 1
            self.proxy_atual = proxy
            logger.warning("Trocando para proxy: %s", proxy)
            return self._format_proxy(proxy)

    def marcar_falha(self) -> None:
        with self.lock:
            if self.proxy_atual:
                self.falhas_por_proxy[self.proxy_atual] = self.falhas_por_proxy.get(self.proxy_atual, 0) + 1
                logger.warning(
                    "Falha registrada para proxy %s. Total=%s",
                    self.proxy_atual,
                    self.falhas_por_proxy[self.proxy_atual],
                )
                self.proxy_atual = None

    def get_stats(self) -> Dict[str, int]:
        with self.lock:
            return dict(self.falhas_por_proxy)


def buscar_proxies_webshare() -> List[str]:
    if not USAR_POOL_WEBSHARE:
        logger.info("Pool Webshare desativado por configuração.")
        return []

    if not WEBSHARE_API_TOKEN or WEBSHARE_API_TOKEN == "seu_token_webshare_aqui":
        logger.warning("WEBSHARE_API_TOKEN não configurado.")
        return []

    url = "https://proxy.webshare.io/api/v2/proxy/list/"
    headers = {"Authorization": f"Token {WEBSHARE_API_TOKEN}"}
    logger.info("Buscando lista de proxies na Webshare API...")

    data = None
    ultimo_erro: Optional[Exception] = None
    for mode in ("direct", "backbone"):
        try:
            logger.info("Tentando carregar proxies Webshare com mode=%s...", mode)
            resp = requests.get(
                url,
                headers=headers,
                params={"mode": mode, "page_size": WEBSHARE_PAGE_SIZE},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info("Webshare respondeu usando mode=%s.", mode)
            break
        except Exception as exc:
            ultimo_erro = exc
            logger.warning("Falhou ao carregar Webshare com mode=%s: %s", mode, exc)

    if data is None:
        logger.error("Não foi possível carregar proxies da Webshare: %s", ultimo_erro)
        return []

    proxies_encontrados: List[str] = []
    for proxy_info in data.get("results", []):
        host = proxy_info.get("proxy_address") or proxy_info.get("host") or proxy_info.get("ip")
        port = proxy_info.get("port")
        user = proxy_info.get("username") or proxy_info.get("user")
        pwd = proxy_info.get("password") or proxy_info.get("pass")

        if not host or not port:
            continue

        if user and pwd:
            proxy_addr = f"{quote(str(user), safe='')}:{quote(str(pwd), safe='')}@{host}:{port}"
        else:
            proxy_addr = f"{host}:{port}"
        proxies_encontrados.append(proxy_addr)

    logger.info("Webshare: %s proxies carregados.", len(proxies_encontrados))
    if proxies_encontrados:
        logger.info("Exemplos de proxies: %s", ", ".join(proxies_encontrados[:3]))
    return proxies_encontrados


def obter_proxy_rotator_global() -> Optional[ProxyRotator]:
    global _PROXY_ROTATOR_GLOBAL
    if not USAR_POOL_WEBSHARE:
        return None
    with _PROXY_ROTATOR_LOCK:
        if _PROXY_ROTATOR_GLOBAL is not None:
            return _PROXY_ROTATOR_GLOBAL
        proxies = buscar_proxies_webshare()
        if not proxies:
            logger.warning("Pool Webshare vazio. Usando proxy manual ou IP direto.")
            return None
        _PROXY_ROTATOR_GLOBAL = ProxyRotator(proxies)
        logger.info("Pool rotativo Webshare pronto com %s proxies.", len(proxies))
        return _PROXY_ROTATOR_GLOBAL


def _montar_proxy_url(proxy_ip: str, proxy_porta: int, proxy_usuario: str, proxy_senha: str) -> Optional[str]:
    proxy_ip = (proxy_ip or "").strip()
    if not proxy_ip or not proxy_porta:
        return None
    proxy_usuario = (proxy_usuario or "").strip()
    proxy_senha = (proxy_senha or "").strip()
    if proxy_usuario:
        user_enc = quote(proxy_usuario, safe="")
        pwd_enc = quote(proxy_senha, safe="") if proxy_senha else ""
        return f"http://{user_enc}:{pwd_enc}@{proxy_ip}:{proxy_porta}"
    return f"http://{proxy_ip}:{proxy_porta}"


def aplicar_proxy_na_sessao(
    sess: requests.Session,
    proxy_rotator: Optional[ProxyRotator] = None,
    proxy_ip: str = "",
    proxy_porta: int = 0,
    proxy_usuario: str = "",
    proxy_senha: str = "",
) -> None:
    sess.trust_env = False
    sess.verify = False
    if proxy_rotator:
        proxy_dict = proxy_rotator.get_current_proxy()
        if proxy_dict:
            sess.proxies.clear()
            sess.proxies.update(proxy_dict)
            logger.info("Proxy Webshare aplicado na sessão: %s", _mask_proxy_url(sess.proxies.get("https") or sess.proxies.get("http") or ""))
            return
    proxy_url = _montar_proxy_url(proxy_ip, proxy_porta, proxy_usuario, proxy_senha)
    if proxy_url:
        sess.proxies.clear()
        sess.proxies.update({"http": proxy_url, "https": proxy_url})
        logger.info("Proxy manual aplicado na sessão: %s", _mask_proxy_url(proxy_url))
    else:
        logger.info("Nenhum proxy aplicado. Usando IP direto.")


def request_com_proxy(
    sess: requests.Session,
    method: str,
    url: str,
    proxy_rotator: Optional[ProxyRotator] = None,
    max_retries: int = 3,
    **kwargs: Any,
) -> requests.Response:
    kwargs["verify"] = False
    kwargs["timeout"] = kwargs.get("timeout", (PROXY_CONNECT_TIMEOUT, PROXY_READ_TIMEOUT))
    ultimo_erro: Optional[Exception] = None
    for tentativa in range(max_retries):
        try:
            if tentativa > 0 and proxy_rotator:
                proxy_rotator.marcar_falha()
                novo_proxy = proxy_rotator.get_next_proxy()
                if novo_proxy:
                    sess.proxies.clear()
                    sess.proxies.update(novo_proxy)
            proxy_info = sess.proxies.get("https") or sess.proxies.get("http") or "IP direto"
            logger.info("Requisição via %s | %s %s", _mask_proxy_url(proxy_info), method.upper(), url)
            resp = sess.request(method.upper(), url, **kwargs)
            if tentativa > 0:
                logger.info("Sucesso após tentativa %s/%s", tentativa + 1, max_retries)
            return resp
        except (
            requests.exceptions.ProxyError,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.SSLError,
        ) as exc:
            ultimo_erro = exc
            logger.warning(
                "Erro de rede/proxy na tentativa %s/%s: %s - %s",
                tentativa + 1,
                max_retries,
                type(exc).__name__,
                str(exc)[:300],
            )
            if tentativa < max_retries - 1:
                continue
            raise
    raise ultimo_erro or RuntimeError("Falha na requisição com proxy.")

def criar_sessao(
    cert_path: str,
    key_path: str,
    proxy_rotator: Optional[ProxyRotator] = None,
    proxy_ip: str = "",
    proxy_porta: int = 0,
    proxy_usuario: str = "",
    proxy_senha: str = "",
) -> requests.Session:
    sess = requests.Session()
    sess.cert = (cert_path, key_path)
    sess.trust_env = False
    sess.verify = False
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
    aplicar_proxy_na_sessao(
        sess,
        proxy_rotator=proxy_rotator,
        proxy_ip=proxy_ip,
        proxy_porta=proxy_porta,
        proxy_usuario=proxy_usuario,
        proxy_senha=proxy_senha,
    )
    return sess


def abrir_acesso_digital_e_entrar(sess: requests.Session, proxy_rotator: Optional[ProxyRotator] = None) -> bool:
    resp = request_com_proxy(sess, "GET", URL_DET_HOME, proxy_rotator=proxy_rotator, timeout=30, allow_redirects=True)
    if resp.status_code != 200:
        return False
    soup = BeautifulSoup(resp.text, "lxml")
    form = soup.find("form")
    if not form:
        return False
    action = form.get("action") or URL_ENTRAR
    if not action.startswith("http"):
        action = requests.compat.urljoin(URL_DET_HOME, action)
    entered = request_com_proxy(sess, "GET", action, proxy_rotator=proxy_rotator, timeout=30, allow_redirects=True)
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


def ir_para_portal(sess: requests.Session, proxy_rotator: Optional[ProxyRotator] = None) -> bool:
    redirected = request_com_proxy(sess, "GET", URL_REDIRECT_PORTAL, proxy_rotator=proxy_rotator, timeout=30, allow_redirects=True)
    if redirected.status_code != 200:
        return False
    action, data = _extrair_form_logintoken(redirected.text)
    if action:
        login = request_com_proxy(sess, "POST", action, proxy_rotator=proxy_rotator, data=data, timeout=30, allow_redirects=True)
        if login.status_code == 200 and "LoginToken" not in (login.url or ""):
            return True
        if login.status_code == 200 and "LoginToken" in (login.url or ""):
            next_url = _extrair_redirect_do_logintoken(login.text) or URL_PORTAL_HOME_DEFAULT
            home = request_com_proxy(sess, "GET", next_url, proxy_rotator=proxy_rotator, timeout=30, allow_redirects=True)
            return home.status_code == 200 and "portalcontribuinte.sefin.ro.gov.br" in (home.url or "")
    fallback = request_com_proxy(sess, "GET", URL_PORTAL_HOME_DEFAULT, proxy_rotator=proxy_rotator, timeout=30, allow_redirects=True)
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
        table_text_norm = _norm_text(table.get_text(" ", strip=True)).upper()
        if (
            "DEBITOS NA INSCRICAO ESTADUAL" in table_text_norm
            or ("NR. LANCAMENTO" in table_text_norm and "VALOR ATUALIZADO" in table_text_norm)
            or ("LANÇAMENTO" in table_text_norm and "VALOR ATUALIZADO" in table_text_norm)
        ):
            tabela_alvo = table
            break

    if not tabela_alvo:
        logger.warning("Tabela de débitos não encontrada no HTML da consulta.")
        return []

    linhas = tabela_alvo.find_all("tr")
    debitos: List[Dict[str, str]] = []

    for tr in linhas:
        cols = tr.find_all("td")
        if len(cols) < 10:
            continue

        def txt(idx: int) -> str:
            return cols[idx].get_text(" ", strip=True) if idx < len(cols) else ""

        linha_txt = _norm_text(tr.get_text(" ", strip=True))
        if not linha_txt or "nr. lancamento" in linha_txt or "data vencimento" in linha_txt:
            continue

        link_extrato = tr.find("a", href=re.compile(r"extrato\.jsp"))

        def norm_url(href: Optional[str]) -> str:
            if not href:
                return ""
            href = href.replace("%22", "").strip('"')
            if href.startswith("http"):
                return href
            return requests.compat.urljoin(URL_CONSULTA_DEBITOS_LISTA, href)

        deb = {
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

        # Evita linhas vazias/cabeçalhos falsos.
        if any((deb.get(k) or "").strip() for k in ("nr_lancamento", "referencia", "receita", "valor_atualizado", "url_extrato")):
            debitos.append(deb)

    logger.info("Débitos extraídos da tabela: %s", len(debitos))
    return debitos


def consultar_debitos_ano(sess: requests.Session, ano: int, proxy_rotator: Optional[ProxyRotator] = None) -> Tuple[List[Dict[str, str]], Optional[str]]:
    resp = request_com_proxy(sess, "GET", URL_CONSULTA_DEBITOS, proxy_rotator=proxy_rotator, timeout=30, allow_redirects=True)
    if resp.status_code != 200:
        return [], f"Erro HTTP {resp.status_code} ao abrir Consulta de Debitos"

    soup = BeautifulSoup(resp.text, "lxml")
    input_tipo = soup.find("input", {"name": "tipoDevedor"})
    tipo_devedor = input_tipo.get("value", "1") if input_tipo else "1"
    inscricoes = _listar_inscricoes_estaduais(resp.text)
    if not inscricoes:
        return [], "Nenhuma inscricao estadual disponivel"

    logger.info("Consultando débitos ano=%s em %s inscrição(ões): %s", ano, len(inscricoes), ", ".join(inscricoes))

    todos_debitos: List[Dict[str, str]] = []
    erros: List[str] = []

    for ie_val in inscricoes:
        payload = {
            "inscricaoEstadual": ie_val,
            "ano": str(ano),
            "tipoDevedor": tipo_devedor,
            "Submit": "Consultar Debitos",
        }

        listed = request_com_proxy(
            sess,
            "POST",
            URL_CONSULTA_DEBITOS_LISTA,
            proxy_rotator=proxy_rotator,
            data=payload,
            timeout=30,
            allow_redirects=True,
        )

        if listed.status_code != 200:
            err = f"Erro HTTP {listed.status_code} lista (ano {ano}) IE={ie_val}"
            logger.warning(err)
            erros.append(err)
            continue

        debs = obter_debitos_inscricao_estadual(listed.text)
        logger.info("Ano=%s IE=%s débitos encontrados=%s", ano, ie_val, len(debs))

        for item in debs:
            item["ano"] = str(ano)
            item["ie"] = ie_val

        todos_debitos.extend(debs)

    if todos_debitos:
        return todos_debitos, None

    if erros:
        return [], " | ".join(erros)

    return [], f"Nenhum débito encontrado no ano {ano} para as inscrições disponíveis."


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


def _buscar_chaves_cte(sess: requests.Session, url_cteconsulta: str, proxy_rotator: Optional[ProxyRotator] = None) -> List[str]:
    if not url_cteconsulta:
        return []

    chave_consultada = _extrair_chave_da_query(url_cteconsulta)
    try:
        resp = request_com_proxy(sess, "GET", url_cteconsulta, proxy_rotator=proxy_rotator, timeout=45, allow_redirects=True)
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
    """
    Normaliza texto da SEFIN para comparação.
    Corrige tanto acentos reais (DÉBITOS/INSCRIÇÃO) quanto alguns casos mojibake.
    """
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())

    # Correções comuns de mojibake antes de remover acentos.
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

    # Remove acentos reais: débitos -> debitos, inscrição -> inscricao.
    normalized = "".join(
        ch for ch in unicodedata.normalize("NFD", normalized)
        if unicodedata.category(ch) != "Mn"
    )
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
        "/ncm/reforma",
        "/empresas",
        "/debitos",
        "/extrato-produto",
        "/proxy/status",
        "/proxy/recarregar",
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


@app.get("/ncm/reforma")
def ncm_reforma(ncm: str = Query(...)) -> Dict[str, Any]:
    """Consulta, de forma restrita, as regras federais IBS/CBS de um NCM."""
    codigo = re.sub(r"\D", "", str(ncm or ""))
    if len(codigo) != 8:
        raise HTTPException(400, "ncm deve conter exatamente 8 digitos.")

    try:
        resposta = requests.get(
            URL_NCM_REFORMA,
            params={"ncm": codigo},
            headers={
                "Accept": "application/json",
                "User-Agent": "ComunidadeFiscal/1.0 (+https://comunidadefiscal.com.br)",
            },
            timeout=(5, 20),
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning("NCM_REFORMA_UPSTREAM_ERROR | ncm=%s | err=%s", codigo, str(exc))
        raise HTTPException(502, "Falha ao consultar a fonte federal de NCM.") from exc

    if resposta.status_code == 429:
        raise HTTPException(429, "A fonte federal de NCM limitou temporariamente as consultas.")
    if not resposta.ok:
        logger.warning("NCM_REFORMA_UPSTREAM_HTTP | ncm=%s | status=%s", codigo, resposta.status_code)
        raise HTTPException(502, "A fonte federal de NCM retornou uma resposta invalida.")

    try:
        dados = resposta.json()
    except ValueError as exc:
        logger.warning("NCM_REFORMA_UPSTREAM_JSON | ncm=%s", codigo)
        raise HTTPException(502, "A fonte federal de NCM nao retornou JSON valido.") from exc

    resultados = dados.get("results") if isinstance(dados, dict) else None
    resultado = resultados[0] if isinstance(resultados, list) and resultados else None
    return {"ok": True, "ncm": codigo, "result": resultado}




@app.get("/proxy/status")
def proxy_status() -> Dict[str, Any]:
    rotator = obter_proxy_rotator_global()
    if not rotator:
        return {"ok": True, "usar_webshare": USAR_POOL_WEBSHARE, "total": 0, "proxy_atual": None, "falhas": {}}
    return {
        "ok": True,
        "usar_webshare": USAR_POOL_WEBSHARE,
        "total": len(rotator.proxies),
        "proxy_atual": rotator.proxy_atual,
        "falhas": rotator.get_stats(),
    }


@app.post("/proxy/recarregar")
def proxy_recarregar() -> Dict[str, Any]:
    global _PROXY_ROTATOR_GLOBAL
    with _PROXY_ROTATOR_LOCK:
        _PROXY_ROTATOR_GLOBAL = None
    rotator = obter_proxy_rotator_global()
    return {
        "ok": True,
        "total": len(rotator.proxies) if rotator else 0,
        "proxy_atual": rotator.proxy_atual if rotator else None,
    }


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
    usar_webshare: int = Query(1),
    proxy_ip: str = Query(""),
    proxy_porta: int = Query(0),
    proxy_usuario: str = Query(""),
    proxy_senha: str = Query(""),
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
        proxy_rotator = obter_proxy_rotator_global() if usar_webshare == 1 else None
        sess = criar_sessao(
            cert_path,
            key_path,
            proxy_rotator=proxy_rotator,
            proxy_ip=proxy_ip,
            proxy_porta=proxy_porta,
            proxy_usuario=proxy_usuario,
            proxy_senha=proxy_senha,
        )

        if not abrir_acesso_digital_e_entrar(sess, proxy_rotator=proxy_rotator):
            raise HTTPException(401, "Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess, proxy_rotator=proxy_rotator):
            raise HTTPException(401, "Falha ao abrir Portal.")

        ano_atual = date.today().year
        anos = [ano_atual] + ([ano_atual - 1] if incluir_ano_anterior == 1 else [])

        all_debs: List[Dict[str, str]] = []
        for ano in anos:
            debs, _ = consultar_debitos_ano(sess, ano, proxy_rotator=proxy_rotator)
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
    usar_webshare: int = Query(1),
    proxy_ip: str = Query(""),
    proxy_porta: int = Query(0),
    proxy_usuario: str = Query(""),
    proxy_senha: str = Query(""),
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
        proxy_rotator = obter_proxy_rotator_global() if usar_webshare == 1 else None
        sess = criar_sessao(
            cert_path,
            key_path,
            proxy_rotator=proxy_rotator,
            proxy_ip=proxy_ip,
            proxy_porta=proxy_porta,
            proxy_usuario=proxy_usuario,
            proxy_senha=proxy_senha,
        )

        if not abrir_acesso_digital_e_entrar(sess, proxy_rotator=proxy_rotator):
            raise HTTPException(401, "Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess, proxy_rotator=proxy_rotator):
            raise HTTPException(401, "Falha ao abrir Portal (LoginToken/home).")

        url_extrato_clean = url_extrato.strip().replace("%22", "").split("#", 1)[0]
        resp = request_com_proxy(sess, "GET", url_extrato_clean, proxy_rotator=proxy_rotator, timeout=60, allow_redirects=True)
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
                internamento = request_com_proxy(sess, "GET", url_capa, proxy_rotator=proxy_rotator, timeout=70, allow_redirects=True)
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
                cte_chaves = _buscar_chaves_cte(sess, cte_url, proxy_rotator=proxy_rotator)
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


if __name__ == "__main__":
    uvicorn.run(
        "analise:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        reload=False,
    )
