# -*- coding: utf-8 -*-
"""
analise.py — FastAPI (Render)

✅ Rotas:
  GET /health
  GET /empresas?user=...
  GET /debitos?user=...&codi=...&incluir_ano_anterior=1
  GET /extrato-produto?user=...&codi=...&url_extrato=...&chave=...

🔧 ALTERAÇÃO (somente):
  - /extrato-produto agora:
      ✅ lê TODAS as NF-e do extrato (tabela)
      ✅ para cada NF-e: abre internamento e retorna APENAS "Itens da nota"
      ✅ corta em 10 colunas (até "Valor Base Calc. ICMS")
      ✅ também abre o cteconsulta.jsp (penúltima coluna) e captura chave(s) de CT-e, se houver
"""

from __future__ import annotations

import os
import re
import base64
import tempfile
import traceback
import logging
from logging.handlers import RotatingFileHandler
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn


# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════
SUPABASE_URL  = os.getenv("SUPABASE_URL", "https://hysrxadnigzqadnlkynq.supabase.co").strip()
SUPABASE_KEY  = os.getenv(
    "SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA",
).strip()

TABELA_CERTS  = os.getenv("TABELA_CERTS", "certifica_dfe").strip()
DEBUG_ERRORS  = os.getenv("DEBUG_ERRORS", "1") == "1"
LOG_FILE      = os.getenv("LOG_FILE", "/tmp/analise.log" if os.getenv("RENDER") else "analise.log")

URL_DET_HOME            = "https://detsec.sefin.ro.gov.br/certificados"
URL_ENTRAR              = "https://detsec.sefin.ro.gov.br/entrar"
URL_REDIRECT_PORTAL     = "https://detsec.sefin.ro.gov.br/contribuinte/notificacoes/redirect_portal"
URL_PORTAL_HOME_DEFAULT = "https://portalcontribuinte.sefin.ro.gov.br/app/home/?exibir_modal=true"
URL_CONSULTA_DEBITOS    = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/"
URL_CONSULTA_DEBITOS_LISTA = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/lista.jsp"
BASE_INTERNAMENTO       = "https://internamentonotas.sefin.ro.gov.br"
BASE_CONTA_CORRENTE     = "https://portalcontribuinte.sefin.ro.gov.br/app/contacorrente/"


# ══════════════════════════════════════════════════════════════════
# LOG
# ══════════════════════════════════════════════════════════════════
logger = logging.getLogger("analise")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════
# SUPABASE
# ══════════════════════════════════════════════════════════════════
def _require_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY or SUPABASE_KEY == "CHANGE_ME":
        raise HTTPException(500, "Configure SUPABASE_URL e SUPABASE_KEY no ENV do Render.")


def supabase_headers() -> Dict[str, str]:
    _require_supabase()
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def carregar_certificados(user_filter: str) -> List[Dict[str, Any]]:
    url = f"{SUPABASE_URL}/rest/v1/{TABELA_CERTS}"
    params: Dict[str, str] = {
        "select": 'id,pem,key,empresa,codi,user,vencimento,"cnpj/cpf"',
        "user":   f"eq.{user_filter}",
        "order":  "id.desc",
        "limit":  "100",
    }
    r = requests.get(url, headers=supabase_headers(), params=params, timeout=30)
    if r.status_code >= 300:
        raise HTTPException(400, f"Supabase REST falhou: {r.text}")
    return r.json() or []


def selecionar_cert_por_codi(certs: List[Dict[str, Any]], codi: str) -> Dict[str, Any]:
    codi = (codi or "").strip()
    if not certs:
        raise HTTPException(404, "Nenhum certificado encontrado para este user.")
    if not codi:
        return certs[0]
    for c in certs:
        if str(c.get("codi") or "").strip() == codi:
            return c
    raise HTTPException(404, f"Não encontrei certificado com CODI={codi} para este user.")


# ══════════════════════════════════════════════════════════════════
# CERT TEMP + SESSION
# ══════════════════════════════════════════════════════════════════
def criar_arquivos_cert_temp(cert_row: Dict[str, Any]) -> Tuple[str, str]:
    pem_b64 = cert_row.get("pem") or ""
    key_b64 = cert_row.get("key") or ""
    if not pem_b64 or not key_b64:
        raise HTTPException(400, "Certificado inválido: pem/key vazios no Supabase.")
    cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    key_file  = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    cert_file.write(base64.b64decode(pem_b64)); cert_file.close()
    key_file.write(base64.b64decode(key_b64));  key_file.close()
    return cert_file.name, key_file.name


def criar_sessao(cert_path: str, key_path: str) -> requests.Session:
    s = requests.Session()
    s.cert = (cert_path, key_path)
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


# ══════════════════════════════════════════════════════════════════
# DET / PORTAL
# ══════════════════════════════════════════════════════════════════
def abrir_acesso_digital_e_entrar(sess: requests.Session) -> bool:
    r = sess.get(URL_DET_HOME, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        return False
    soup = BeautifulSoup(r.text, "lxml")
    form = soup.find("form")
    if not form:
        return False
    action = form.get("action") or URL_ENTRAR
    if not action.startswith("http"):
        action = requests.compat.urljoin(URL_DET_HOME, action)
    r_ent = sess.get(action, timeout=30, allow_redirects=True)
    if r_ent.status_code != 200:
        return False
    return "/certificado/acessos" in (r_ent.url or "")


def _extrair_form_logintoken(html: str) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if "portalcontribuinte.sefin.ro.gov.br" in action or "LoginToken" in action:
            if not action.startswith("http"):
                action = requests.compat.urljoin(URL_REDIRECT_PORTAL, action)
            data: Dict[str, str] = {
                inp.get("name"): inp.get("value", "") or ""
                for inp in form.find_all("input")
                if inp.get("name")
            }
            return action, data
    return None, None


def _extrair_redirect_do_logintoken(html: str) -> Optional[str]:
    m = re.search(
        r"location\s*=\s*['\"]"
        r"(https://portalcontribuinte\.sefin\.ro\.gov\.br[^'\"]+)['\"]", html
    )
    if m:
        return m.group(1)
    m = re.search(r"location\.href\s*=\s*['\"](/app/home[^'\"]*)['\"]", html)
    if m:
        return "https://portalcontribuinte.sefin.ro.gov.br" + m.group(1)
    return None


def ir_para_portal(sess: requests.Session) -> bool:
    r_red = sess.get(URL_REDIRECT_PORTAL, timeout=30, allow_redirects=True)
    if r_red.status_code != 200:
        return False
    action_form, data_form = _extrair_form_logintoken(r_red.text)
    if action_form:
        r_login = sess.post(action_form, data=data_form, timeout=30, allow_redirects=True)
        if r_login.status_code == 200 and "LoginToken" not in (r_login.url or ""):
            return True
        if r_login.status_code == 200 and "LoginToken" in (r_login.url or ""):
            next_url = _extrair_redirect_do_logintoken(r_login.text) or URL_PORTAL_HOME_DEFAULT
            r_home = sess.get(next_url, timeout=30, allow_redirects=True)
            return r_home.status_code == 200 and "portalcontribuinte.sefin.ro.gov.br" in (r_home.url or "")
    r_portal = sess.get(URL_PORTAL_HOME_DEFAULT, timeout=30, allow_redirects=True)
    return r_portal.status_code == 200 and "LoginToken" not in (r_portal.url or "")


# ══════════════════════════════════════════════════════════════════
# DÉBITOS
# ══════════════════════════════════════════════════════════════════
def _listar_inscricoes_estaduais(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    sel = soup.find("select", {"name": "inscricaoEstadual"})
    if not sel:
        return []
    seen: set = set()
    out = []
    for opt in sel.find_all("option"):
        v = (opt.get("value") or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def obter_debitos_inscricao_estadual(html_deb: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_deb, "lxml")
    tabela_alvo = None
    for tab in soup.find_all("table"):
        ths = tab.find_all("th")
        if ths and "DÉBITOS NA INSCRIÇÃO ESTADUAL" in ths[0].get_text(" ", strip=True).upper():
            tabela_alvo = tab
            break
    if not tabela_alvo:
        return []
    linhas = tabela_alvo.find_all("tr")
    if len(linhas) <= 2:
        return []

    debitos: List[Dict[str, str]] = []
    for tr in linhas[2:]:
        tds = tr.find_all("td")
        if len(tds) < 11:
            continue

        def txt(i: int) -> str:
            return tds[i].get_text(" ", strip=True) if i < len(tds) else ""

        link_extrato = tr.find("a", href=re.compile(r"extrato\.jsp"))

        def norm_url(href: Optional[str]) -> str:
            if not href:
                return ""
            href = href.replace("%22", "").strip('"')
            return href if href.startswith("http") else requests.compat.urljoin(URL_CONSULTA_DEBITOS_LISTA, href)

        debitos.append({
            "nr_lancamento":   txt(2),
            "parcela":         txt(3),
            "referencia":      txt(4),
            "complemento":     txt(5),
            "receita":         txt(6),
            "situacao":        txt(7),
            "data_vencimento": txt(8),
            "valor_lancamento": txt(9),
            "valor_atualizado": txt(10),
            "url_extrato": norm_url(link_extrato.get("href") if link_extrato else ""),
        })
    return debitos


def consultar_debitos_ano(sess: requests.Session, ano: int) -> Tuple[List[Dict[str, str]], Optional[str]]:
    r = sess.get(URL_CONSULTA_DEBITOS, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        return [], f"Erro HTTP {r.status_code} ao abrir Consulta de Débitos"

    soup = BeautifulSoup(r.text, "lxml")
    input_tipo = soup.find("input", {"name": "tipoDevedor"})
    tipo_devedor = input_tipo.get("value", "1") if input_tipo else "1"
    inscricoes = _listar_inscricoes_estaduais(r.text)
    if not inscricoes:
        return [], "Nenhuma inscrição estadual disponível (select vazio)"

    last_err = None
    for ie_val in inscricoes:
        payload = {
            "inscricaoEstadual": ie_val,
            "ano": str(ano),
            "tipoDevedor": tipo_devedor,
            "Submit": "Consultar Débitos",
        }
        r2 = sess.post(URL_CONSULTA_DEBITOS_LISTA, data=payload, timeout=30, allow_redirects=True)
        if r2.status_code != 200:
            last_err = f"Erro HTTP {r2.status_code} lista (ano {ano}) IE={ie_val}"
            continue
        debs = obter_debitos_inscricao_estadual(r2.text)
        for d in debs:
            d["ano"] = str(ano)
            d["ie"]  = ie_val
        return debs, None

    return [], last_err or f"Falha ao consultar lista (ano {ano})"


# ══════════════════════════════════════════════════════════════════
# EXTRATO (conta-corrente) → TOKEN / USUARIO / CHAVES NFE / LINK CTE
# ══════════════════════════════════════════════════════════════════
def _extrair_token_e_usuario(html_extrato: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Suporta dois formatos:
      (A) extrato antigo: var TOKEN='...' e "Olá <strong>CPF -"
      (B) conta corrente: var TOKEN='...' e var CPF_CLIENTE=('...')...
    """
    # TOKEN
    m1 = re.search(r"var\s+TOKEN\s*=\s*'([^']+)'", html_extrato)
    token = m1.group(1).strip() if m1 else None

    # Formato antigo (saudação)
    m2 = re.search(r"Ol[áa]\s*<strong>\s*([0-9]{11})\s*-", html_extrato, flags=re.I)
    if m2:
        return token, m2.group(1).strip()

    # Formato conta corrente: CPF_CLIENTE
    m3 = re.search(r"var\s+CPF_CLIENTE\s*=\s*\(\s*'([^']*)'\s*\|\|\s*''\s*\)", html_extrato)
    if m3:
        cpf = re.sub(r"\D", "", m3.group(1) or "")
        if cpf:
            return token, cpf

    return token, None


def _extrair_notas_do_extrato_contacorrente(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """
    Extrai todas as NF-e exibidas na tabela do extrato da conta corrente.

    - chave_nfe: vem do data-chave (link do extrato) OU da primeira <td> da linha
    - cte_modal_rel: vem do onclick abrirModal('cteconsulta.jsp?chave=...')

    No HTML:
      <a class="abrir-cte" data-chave="...." onclick="abrirCapaInternamento(this)">Exibir</a>
      <a onclick="abrirModal('cteconsulta.jsp?chave=....'); return false;">Exibir</a>
    """
    notas: List[Dict[str, Any]] = []

    # pega todas as linhas e tenta achar a chave principal
    for tr in soup.find_all("tr"):
        chave_nfe = None
        a_extrato = tr.find("a", {"class": "abrir-cte"})
        if a_extrato:
            chave_nfe = (a_extrato.get("data-chave") or "").strip()

        if not chave_nfe:
            tds = tr.find_all("td")
            if tds:
                t0 = (tds[0].get_text(" ", strip=True) or "").strip()
                if re.fullmatch(r"\d{44}", t0):
                    chave_nfe = t0

        if not chave_nfe or not re.fullmatch(r"\d{44}", chave_nfe):
            continue

        # tenta capturar o link do modal de CTE na mesma linha
        cte_modal_rel = ""
        for a in tr.find_all("a"):
            onclick = a.get("onclick") or ""
            m = re.search(r"abrirModal\(\s*'([^']*cteconsulta\.jsp\?chave=\d{44}[^']*)'\s*\)", onclick)
            if m:
                cte_modal_rel = m.group(1).strip()
                break

        notas.append({
            "chave_nfe": chave_nfe,
            "cte_modal_rel": cte_modal_rel,  # relativo (cteconsulta.jsp?chave=...)
        })

    # dedup mantendo ordem
    seen = set()
    out = []
    for n in notas:
        k = n["chave_nfe"]
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out


def _url_capa_internamento(usuario: str, chave: str, token: Optional[str]) -> str:
    base = f"{BASE_INTERNAMENTO}/capa_internamentos/{usuario}/{chave}"
    return base + "?token=" + requests.utils.quote(token, safe="") if token else base


def _resolver_url_cteconsulta(url_extrato_clean: str, rel: str) -> str:
    """
    rel normalmente é "cteconsulta.jsp?chave=..."
    A página é /app/contacorrente/extrato.jsp...
    """
    if not rel:
        return ""
    if rel.startswith("http"):
        return rel
    # força base da conta corrente para evitar urljoin estranho com query
    return requests.compat.urljoin(BASE_CONTA_CORRENTE, rel)


def _buscar_chaves_cte(sess: requests.Session, url_cteconsulta: str) -> List[str]:
    """
    Abre o cteconsulta.jsp e tenta extrair chaves (44 dígitos).
    Pode retornar vazio quando não há CTe.
    """
    if not url_cteconsulta:
        return []
    try:
        r = sess.get(url_cteconsulta, timeout=45, allow_redirects=True)
        if r.status_code != 200:
            return []
        chaves = re.findall(r"\b\d{44}\b", r.text or "")
        # dedup
        seen = set()
        out = []
        for c in chaves:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════
# PARSER: SOMENTE "ITENS DA NOTA" (10 colunas)
# ══════════════════════════════════════════════════════════════════
def _norm_text(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    for src, dst in [
        ("á","a"),("à","a"),("â","a"),("ã","a"),
        ("é","e"),("ê","e"),("è","e"),
        ("í","i"),("î","i"),
        ("ó","o"),("ô","o"),("õ","o"),
        ("ú","u"),("û","u"),
        ("ç","c"),
    ]:
        t = t.replace(src, dst)
    return t


def _limpar_header(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip())


def _parse_itens_da_nota_primeiras_10_colunas(soup: BeautifulSoup) -> Dict[str, Any]:
    # acha o H4 da seção
    h4 = None
    for x in soup.find_all(["h4", "h3", "h2"]):
        if "itens da nota" in _norm_text(x.get_text(" ", strip=True)):
            h4 = x
            break

    if not h4:
        return {
            "tabela_encontrada": False,
            "motivo": "heading 'Itens da nota' nao encontrado",
            "headers": [],
            "itens": [],
            "totais": {"qtd_linhas": 0, "qtd_colunas": 0},
            "diagnostico": {"headers_full_n": 0, "headers_cut_n": 0},
        }

    table = h4.find_next("table")
    if not table:
        return {
            "tabela_encontrada": False,
            "motivo": "table apos heading 'Itens da nota' nao encontrada",
            "headers": [],
            "itens": [],
            "totais": {"qtd_linhas": 0, "qtd_colunas": 0},
            "diagnostico": {"headers_full_n": 0, "headers_cut_n": 0},
        }

    ths = table.find_all("th")
    headers_full = [_limpar_header(th.get_text(" ", strip=True)) for th in ths]
    headers_cut = headers_full[:10]

    itens: List[Dict[str, Any]] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        cols_full = [td.get_text(" ", strip=True) for td in tds]
        if len(cols_full) < 2:
            continue

        cols_cut = cols_full[:10]

        item0 = (cols_cut[0].strip() if cols_cut else "")
        if not re.fullmatch(r"\d+", item0 or ""):
            continue

        row = {"cols_raw": cols_cut}
        for i, h in enumerate(headers_cut):
            row[h] = (cols_cut[i].strip() if i < len(cols_cut) else None) or None

        itens.append(row)

    return {
        "tabela_encontrada": True,
        "motivo": None,
        "headers": headers_cut,
        "itens": itens,
        "totais": {
            "qtd_linhas": len(itens),
            "qtd_colunas": len(headers_cut),
        },
        "diagnostico": {
            "headers_full_n": len(headers_full),
            "headers_cut_n": len(headers_cut),
        },
    }


def parse_internamento(html_intern: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html_intern, "lxml")
    return _parse_itens_da_nota_primeiras_10_colunas(soup)


# ══════════════════════════════════════════════════════════════════
# APP FASTAPI
# ══════════════════════════════════════════════════════════════════
app = FastAPI(title="analise — Débitos + Extrato (Somente Itens da nota: 10 colunas)")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
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

    base = {"ok": False, "error": str(exc)}
    if DEBUG_ERRORS:
        base.update({
            "type":      exc.__class__.__name__,
            "path":      str(request.url.path),
            "query":     dict(request.query_params),
            "traceback": traceback.format_exc(),
            "log_file":  LOG_FILE,
        })
    return JSONResponse(status_code=500, content=base)


@app.get("/")
def root():
    return {
        "ok":      True,
        "service": "analise",
        "date_utc": _now_iso(),
        "routes":  ["/health", "/empresas", "/debitos", "/extrato-produto"],
    }


@app.get("/health")
def health():
    return {"ok": True, "date_utc": _now_iso()}


@app.get("/empresas")
def empresas(user: str = Query(...)):
    if not user or "@" not in user:
        raise HTTPException(400, "user inválido.")
    certs = carregar_certificados(user)
    seen: set = set()
    out = []
    for c in certs:
        codi = str(c.get("codi") or "").strip()
        if not codi or codi in seen:
            continue
        seen.add(codi)
        out.append({
            "codi":       codi,
            "empresa":    (c.get("empresa") or "").strip(),
            "cnpj":       (c.get("cnpj/cpf") or "").strip(),
            "vencimento": (c.get("vencimento") or ""),
        })
    return {"ok": True, "user": user, "total": len(out), "empresas": out}


@app.get("/debitos")
def route_debitos(
    user:                str = Query(...),
    codi:                str = Query(...),
    incluir_ano_anterior: int = Query(1),
):
    if not user or "@" not in user:
        raise HTTPException(400, "user inválido.")
    if not codi:
        raise HTTPException(400, "codi obrigatório.")

    certs = carregar_certificados(user)
    cert  = selecionar_cert_por_codi(certs, codi)

    cert_path = key_path = None
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
        for a in anos:
            debs, _ = consultar_debitos_ano(sess, a)
            all_debs.extend(debs or [])

        return {
            "ok":      True,
            "user":    user,
            "codi":    str(cert.get("codi") or ""),
            "empresa": (cert.get("empresa") or ""),
            "debitos": all_debs,
        }

    finally:
        for p in (cert_path, key_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


@app.get("/extrato-produto")
def extrato_produto(
    user:        str = Query(...),
    codi:        str = Query(...),
    url_extrato: str = Query(...),
    chave:       str = Query(""),
    max_notas:   int = Query(60),
    buscar_cte:  int = Query(1),
):
    """
    - Se 'chave' vier preenchida: processa só ela.
    - Se não vier: pega TODAS as chaves NFe da tabela do extrato.
    - Para cada chave: abre capa_internamentos e retorna apenas "Itens da nota" (10 colunas).
    - Se buscar_cte=1: abre também cteconsulta.jsp e captura chaves (44 dígitos).
    """
    if not user or "@" not in user:
        raise HTTPException(400, "user inválido.")
    if not codi:
        raise HTTPException(400, "codi obrigatório.")
    if not url_extrato.startswith("http"):
        raise HTTPException(400, "url_extrato deve começar com http/https.")
    if max_notas < 1:
        max_notas = 1
    if max_notas > 200:
        max_notas = 200

    certs = carregar_certificados(user)
    cert  = selecionar_cert_por_codi(certs, codi)

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(401, "Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(401, "Falha ao abrir Portal (LoginToken/home).")

        url_extrato_clean = url_extrato.strip().replace("%22", "").split("#", 1)[0]
        r = sess.get(url_extrato_clean, timeout=60, allow_redirects=True)
        if r.status_code != 200:
            raise HTTPException(400, f"Erro ao abrir extrato.jsp: HTTP {r.status_code}")

        token, usuario = _extrair_token_e_usuario(r.text)
        if not usuario:
            raise HTTPException(400, "Não consegui extrair USUARIO/CPF do extrato.jsp (conta corrente).")

        soup_extrato = BeautifulSoup(r.text, "lxml")
        notas_meta = _extrair_notas_do_extrato_contacorrente(soup_extrato)

        # se veio "chave", filtra
        chave = (chave or "").strip()
        if chave:
            if not re.fullmatch(r"\d{44}", chave):
                raise HTTPException(400, "chave inválida: deve ter 44 dígitos.")
            # mantém somente a escolhida (se não existir na tabela, ainda tenta processar)
            found = [n for n in notas_meta if n["chave_nfe"] == chave]
            notas_meta = found if found else [{"chave_nfe": chave, "cte_modal_rel": ""}]

        if not notas_meta:
            return {
                "ok": True,
                "user": user,
                "codi": str(cert.get("codi") or ""),
                "empresa": cert.get("empresa") or "",
                "result": {
                    "ok": True,
                    "final_url_extrato": r.url,
                    "token_found": bool(token),
                    "usuario": usuario,
                    "total_notas": 0,
                    "notas": [],
                }
            }

        notas_meta = notas_meta[:max_notas]

        notas_out: List[Dict[str, Any]] = []
        for meta in notas_meta:
            chave_nfe = meta["chave_nfe"]
            url_capa = _url_capa_internamento(usuario, chave_nfe, token)

            # 1) internamento -> itens da nota
            itens_payload: Dict[str, Any]
            try:
                r2 = sess.get(url_capa, timeout=70, allow_redirects=True)
                if r2.status_code != 200:
                    itens_payload = {
                        "ok": False,
                        "http_status": r2.status_code,
                        "final_url": r2.url,
                        "tabela_encontrada": False,
                        "motivo": f"Falha ao abrir internamento: HTTP {r2.status_code}",
                        "headers": [],
                        "itens": [],
                        "totais": {"qtd_linhas": 0, "qtd_colunas": 0},
                        "diagnostico": {"headers_full_n": 0, "headers_cut_n": 0},
                    }
                else:
                    parsed = parse_internamento(r2.text)
                    itens_payload = {
                        "ok": True,
                        "http_status": r2.status_code,
                        "final_url": r2.url,
                        **parsed,
                    }
            except Exception as e:
                itens_payload = {
                    "ok": False,
                    "http_status": None,
                    "final_url": url_capa,
                    "tabela_encontrada": False,
                    "motivo": f"Erro ao abrir/parsear internamento: {e}",
                    "headers": [],
                    "itens": [],
                    "totais": {"qtd_linhas": 0, "qtd_colunas": 0},
                    "diagnostico": {"headers_full_n": 0, "headers_cut_n": 0},
                }

            # 2) cteconsulta -> chaves CTe
            cte_chaves: List[str] = []
            if buscar_cte == 1:
                url_cte = _resolver_url_cteconsulta(url_extrato_clean, meta.get("cte_modal_rel") or "")
                cte_chaves = _buscar_chaves_cte(sess, url_cte)

            notas_out.append({
                "chave_nfe": chave_nfe,
                "internamento_url": url_capa,
                "cte_url": _resolver_url_cteconsulta(url_extrato_clean, meta.get("cte_modal_rel") or "") if buscar_cte == 1 else "",
                "cte_chaves": cte_chaves,
                "itens_da_nota": itens_payload,
            })

        return {
            "ok":      True,
            "user":    user,
            "codi":    str(cert.get("codi") or ""),
            "empresa": cert.get("empresa") or "",
            "result": {
                "ok": True,
                "final_url_extrato": r.url,
                "token_found": bool(token),
                "usuario": usuario,
                "total_notas": len(notas_out),
                "notas": notas_out,
            },
        }

    finally:
        for p in (cert_path, key_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    uvicorn.run(
        "analise:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        reload=False,
    )
