from __future__ import annotations

import base64
import logging
import os
import re
import tempfile
import traceback
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

import requests
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def criar_sessao(cert_path: str, key_path: str) -> requests.Session:
    sess = requests.Session()
    sess.cert = (cert_path, key_path)
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

    user_match = re.search(r"Ol[áa]\s*<strong>\s*([0-9]{11})\s*-", html_extrato, flags=re.I)
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
        ("á", "a"),
        ("à", "a"),
        ("â", "a"),
        ("ã", "a"),
        ("é", "e"),
        ("ê", "e"),
        ("è", "e"),
        ("í", "i"),
        ("î", "i"),
        ("ó", "o"),
        ("ô", "o"),
        ("õ", "o"),
        ("ú", "u"),
        ("û", "u"),
        ("ç", "c"),
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

    payload: Dict[str, Any] = {"ok": False, "error": str(exc)}
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
    return JSONResponse(status_code=500, content=payload)


@app.get("/")
def root() -> Dict[str, Any]:
    routes = [
        "/health",
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


if __name__ == "__main__":
    uvicorn.run(
        "analise:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        reload=False,
    )
