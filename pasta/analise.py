# -*- coding: utf-8 -*-
"""
extrato_api.py (FastAPI) — Render
✅ CORREÇÃO: mescla ("merge") entre:
  - Tabela "Itens da nota"  -> contém NCM (coluna NCM)
  - Tabela "Cálculo Itens"  -> contém cálculo/impostos por item (e Produto 8231 etc)
MERGE padrão: por "Item" (mais estável).

Rotas:
  GET /empresas?user=...
  GET /debitos?user=...&codi=...&incluir_ano_anterior=1
  GET /extrato?user=...&codi=...&url_extrato=...

ENV (Render):
  SUPABASE_URL
  SUPABASE_KEY
  TABELA_CERTS=certifica_dfe (opcional)
  DEBUG_ERRORS=1 (opcional)
  LOG_FILE=/tmp/extrato_api.log (opcional)

Start Command:
  PYTHONPATH=. uvicorn extrato_api:app --host 0.0.0.0 --port $PORT

requirements.txt:
  fastapi==0.115.6
  uvicorn==0.30.6
  requests==2.32.3
  beautifulsoup4==4.12.3
  lxml==5.2.2
"""

from __future__ import annotations

import os
import re
import json
import base64
import tempfile
import traceback
import logging
from logging.handlers import RotatingFileHandler
from datetime import date, datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import requests
from bs4 import BeautifulSoup

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn


# =========================================================
# CONFIG
# =========================================================
SUPABASE_URL = "https://hysrxadnigzqadnlkynq.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA"
TABELA_CERTS = os.getenv("TABELA_CERTS", "certifica_dfe").strip()

DEBUG_ERRORS = os.getenv("DEBUG_ERRORS", "1") == "1"

LOG_FILE = os.getenv(
    "LOG_FILE",
    "/tmp/extrato_api.log" if "RENDER" in os.getenv("RENDER", "") else "extrato_api.log"
)

# URLs DET/PORTAL (RO)
URL_DET_HOME = "https://detsec.sefin.ro.gov.br/certificados"
URL_ENTRAR = "https://detsec.sefin.ro.gov.br/entrar"
URL_REDIRECT_PORTAL = "https://detsec.sefin.ro.gov.br/contribuinte/notificacoes/redirect_portal"
URL_PORTAL_HOME_DEFAULT = "https://portalcontribuinte.sefin.ro.gov.br/app/home/?exibir_modal=true"

URL_CONSULTA_DEBITOS = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/"
URL_CONSULTA_DEBITOS_LISTA = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/lista.jsp"


# =========================================================
# LOG
# =========================================================
logger = logging.getLogger("extrato_api")
logger.setLevel(logging.INFO)

if not logger.handlers:
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_exc(prefix: str, exc: Exception):
    try:
        logger.error("%s | %s\n%s", prefix, str(exc), traceback.format_exc())
    except Exception:
        pass


# =========================================================
# FASTAPI
# =========================================================
app = FastAPI(title="Extrato NF-e por Produto (Portal RO) — API")

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
            traceback.format_exc()
        )
    except Exception:
        pass

    if not DEBUG_ERRORS:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": str(exc),
            "type": exc.__class__.__name__,
            "path": str(request.url.path),
            "query": dict(request.query_params),
            "traceback": traceback.format_exc(),
            "log_file": LOG_FILE,
        },
    )


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "extrato_api",
        "date_utc": _now_iso(),
        "routes": ["/health", "/empresas", "/debitos", "/extrato"],
        "log_file": LOG_FILE,
        "debug_errors": DEBUG_ERRORS,
        "tabela_certs": TABELA_CERTS,
    }


@app.get("/health")
def health():
    return {"ok": True, "date_utc": _now_iso()}


# =========================================================
# SUPABASE CERTS
# =========================================================
def _require_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=400, detail="Configure SUPABASE_URL e SUPABASE_KEY nas ENV do Render.")


def supabase_headers() -> Dict[str, str]:
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def carregar_certificados_validos(user_filter: str) -> List[Dict[str, Any]]:
    _require_supabase()
    url = f"{SUPABASE_URL}/rest/v1/{TABELA_CERTS}"
    params: Dict[str, str] = {
        "select": 'id,pem,key,empresa,codi,user,vencimento,"cnpj/cpf"',
        "user": f"eq.{user_filter}",
        "order": "id.desc",
    }
    r = requests.get(url, headers=supabase_headers(), params=params, timeout=30)
    if r.status_code >= 300:
        raise HTTPException(status_code=400, detail=f"Supabase REST falhou: {r.text}")
    return r.json() or []


def selecionar_cert_por_codi(certs: List[Dict[str, Any]], codi: str) -> Dict[str, Any]:
    codi = (codi or "").strip()
    if not codi:
        return certs[0]
    for c in certs:
        if str(c.get("codi") or "").strip() == codi:
            return c
    raise HTTPException(status_code=404, detail=f"Não encontrei certificado com CODI={codi} para este user.")


@app.get("/empresas")
def empresas(user: str = Query(...)):
    if not user or "@" not in user:
        raise HTTPException(status_code=400, detail="user inválido.")
    certs = carregar_certificados_validos(user)
    out = []
    for c in certs:
        out.append({
            "codi": str(c.get("codi") or "").strip(),
            "empresa": (c.get("empresa") or "").strip(),
            "cnpj": (c.get("cnpj/cpf") or "").strip(),
            "vencimento": (c.get("vencimento") or ""),
        })
    # remove duplicados por CODI (fica o mais recente)
    seen = set()
    uniq = []
    for it in out:
        if it["codi"] in seen:
            continue
        seen.add(it["codi"])
        uniq.append(it)
    return {"ok": True, "user": user, "total": len(uniq), "empresas": uniq}


# =========================================================
# CERT TEMP + SESSION
# =========================================================
def criar_arquivos_cert_temp(cert_row: Dict[str, Any]) -> Tuple[str, str]:
    pem_b64 = cert_row.get("pem") or ""
    key_b64 = cert_row.get("key") or ""
    if not pem_b64 or not key_b64:
        raise HTTPException(status_code=400, detail="Certificado inválido: pem/key vazios no Supabase.")

    pem_bytes = base64.b64decode(pem_b64)
    key_bytes = base64.b64decode(key_b64)

    cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    cert_file.write(pem_bytes)
    cert_file.close()
    key_file.write(key_bytes)
    key_file.close()
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
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    })
    return s


# =========================================================
# DET / PORTAL (login + home)
# =========================================================
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

    return ("/certificado/acessos" in (r_ent.url or ""))


def _extrair_form_logintoken(html: str) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if "portalcontribuinte.sefin.ro.gov.br" in action or "LoginToken" in action:
            if not action.startswith("http"):
                action = requests.compat.urljoin(URL_REDIRECT_PORTAL, action)
            data: Dict[str, str] = {}
            for inp in form.find_all("input"):
                name = inp.get("name")
                if not name:
                    continue
                data[name] = inp.get("value", "") or ""
            return action, data
    return None, None


def _extrair_redirect_do_logintoken(html: str) -> Optional[str]:
    m = re.search(r"location\s*=\s*['\"](https://portalcontribuinte\.sefin\.ro\.gov\.br[^'\"]+)['\"]", html)
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
            return (r_home.status_code == 200 and "portalcontribuinte.sefin.ro.gov.br" in (r_home.url or ""))

    r_portal = sess.get(URL_PORTAL_HOME_DEFAULT, timeout=30, allow_redirects=True)
    return (r_portal.status_code == 200 and "LoginToken" not in (r_portal.url or ""))


# =========================================================
# DÉBITOS (lista + urls)
# =========================================================
def _listar_inscricoes_estaduais(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    sel_ie = soup.find("select", {"name": "inscricaoEstadual"})
    if not sel_ie:
        return []
    vals = []
    for opt in sel_ie.find_all("option"):
        v = (opt.get("value") or "").strip()
        if v:
            vals.append(v)
    out, seen = [], set()
    for v in vals:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def obter_debitos_inscricao_estadual(html_deb: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_deb, "lxml")
    tabela_alvo = None
    for tab in soup.find_all("table"):
        ths = tab.find_all("th")
        if not ths:
            continue
        if "DÉBITOS NA INSCRIÇÃO ESTADUAL" in ths[0].get_text(" ", strip=True).upper():
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

        link_dare = tr.find("a", href=re.compile(r"dare\.sefin\.ro\.gov\.br/adm"))
        link_extrato = tr.find("a", href=re.compile(r"extrato\.jsp"))

        def norm_url(href: Optional[str]) -> str:
            if not href:
                return ""
            href = href.replace("%22", "").strip('"')
            if href.startswith("http"):
                return href
            return requests.compat.urljoin(URL_CONSULTA_DEBITOS_LISTA, href)

        debitos.append({
            "dare": txt(0),
            "extrato": txt(1),
            "nr_lancamento": txt(2),
            "parcela": txt(3),
            "referencia": txt(4),
            "complemento": txt(5),
            "receita": txt(6),
            "situacao": txt(7),
            "data_vencimento": txt(8),
            "valor_lancamento": txt(9),
            "valor_atualizado": txt(10),
            "url_dare": norm_url(link_dare.get("href") if link_dare else ""),
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
            d["ie"] = ie_val
        return debs, None

    return [], last_err or f"Falha ao consultar lista (ano {ano})"


@app.get("/debitos")
def debitos(
    user: str = Query(...),
    codi: str = Query(...),
    incluir_ano_anterior: int = Query(1, description="1=ano atual+anterior; 0=só ano atual"),
):
    if not user or "@" not in user:
        raise HTTPException(status_code=400, detail="user inválido.")
    if not codi:
        raise HTTPException(status_code=400, detail="codi obrigatório.")

    certs = carregar_certificados_validos(user)
    if not certs:
        raise HTTPException(status_code=404, detail="Nenhum certificado encontrado para este user.")
    cert = selecionar_cert_por_codi(certs, codi)

    empresa = (cert.get("empresa") or "").strip()
    codi_sel = str(cert.get("codi") or "").strip()

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        logger.info("DEBITOS_START | user=%s | codi=%s | empresa=%s", user, codi_sel, empresa)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(status_code=401, detail="Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(status_code=401, detail="Falha ao abrir Portal (LoginToken/home).")

        ano_atual = date.today().year
        anos = [ano_atual] + ([ano_atual - 1] if incluir_ano_anterior == 1 else [])

        all_debs: List[Dict[str, str]] = []
        erros: List[str] = []

        for a in anos:
            debs, err = consultar_debitos_ano(sess, a)
            if err:
                erros.append(f"{a}: {err}")
            if debs:
                all_debs.extend(debs)

        logger.info("DEBITOS_DONE | user=%s | codi=%s | debitos=%s | erros=%s", user, codi_sel, len(all_debs), len(erros))

        return {
            "ok": True,
            "user": user,
            "codi": codi_sel,
            "empresa": empresa,
            "anos": anos,
            "qtd": len(all_debs),
            "erros": erros,
            "debitos": all_debs,
        }

    except HTTPException:
        raise
    except Exception as e:
        _log_exc(f"DEBITOS_FAIL | user={user} codi={codi}", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for p in (cert_path, key_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# =========================================================
# ✅ EXTRATO: ITENS (com NCM) + CÁLCULO (impostos) + MERGE
# =========================================================
def _norm_txt(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _to_number_ptbr(s: str) -> Optional[float]:
    """
    Converte '5.633,65' -> 5633.65
    Retorna None se vazio.
    """
    s = _norm_txt(s)
    if not s:
        return None
    t = re.sub(r"[^\d,.\-]", "", s)
    if not t:
        return None
    t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except Exception:
        return None


def _find_table_by_title(soup: BeautifulSoup, title_text: str):
    """
    Procura um <h4 class="table-title">TÍTULO</h4> e pega a primeira <table> dentro do mesmo bloco.
    Seu HTML tem exatamente esse padrão.
    """
    title_text_n = _norm_txt(title_text).lower()
    h4 = None
    for x in soup.find_all(["h3", "h4"]):
        if _norm_txt(x.get_text()).lower() == title_text_n:
            h4 = x
            break
    if not h4:
        return None

    parent = h4.find_parent()
    if parent:
        tb = parent.find("table")
        if tb:
            return tb

    return h4.find_next("table")


def _table_headers(table) -> List[str]:
    thead = table.find("thead")
    if not thead:
        # fallback: usa a primeira row como header se tiver <th>
        ths = table.find_all("th")
        return [_norm_txt(th.get_text()) for th in ths if _norm_txt(th.get_text())]
    ths = thead.find_all("th")
    return [_norm_txt(th.get_text()) for th in ths if _norm_txt(th.get_text())]


def _table_rows(table) -> List[List[str]]:
    tbody = table.find("tbody")
    trs = tbody.find_all("tr") if tbody else table.find_all("tr")
    out = []
    for tr in trs:
        tds = tr.find_all("td")
        if not tds:
            continue
        cols = [_norm_txt(td.get_text(" ", strip=True)) for td in tds]
        if any(c for c in cols):
            out.append(cols)
    return out


def parse_itens_da_nota(html: str) -> List[Dict[str, Any]]:
    """
    ✅ Aqui é onde vem o NCM.
    Seu arquivo tem coluna NCM visível em 'Itens da nota'.
    """
    soup = BeautifulSoup(html, "lxml")
    table = _find_table_by_title(soup, "Itens da nota")
    if not table:
        return []

    headers = _table_headers(table)
    rows = _table_rows(table)

    # Mapeia índices pelo nome do header (mais robusto que posição fixa)
    def idx_of(name_contains: str) -> Optional[int]:
        for i, h in enumerate(headers):
            if name_contains.lower() in h.lower():
                return i
        return None

    i_item = idx_of("Item") or 0
    i_desc = idx_of("Descrição") or 1
    i_cfop = idx_of("CFOP")
    i_cest = idx_of("CEST")
    i_ncm = idx_of("NCM")

    itens: List[Dict[str, Any]] = []
    for cols in rows:
        # garante tamanho
        if len(cols) < len(headers):
            cols = cols + [""] * (len(headers) - len(cols))

        item = cols[i_item].strip() if i_item is not None and i_item < len(cols) else ""
        desc = cols[i_desc].strip() if i_desc is not None and i_desc < len(cols) else ""
        cfop = cols[i_cfop].strip() if i_cfop is not None and i_cfop < len(cols) else ""
        cest = cols[i_cest].strip() if i_cest is not None and i_cest < len(cols) else ""
        ncm = cols[i_ncm].strip() if i_ncm is not None and i_ncm < len(cols) else ""

        if not (item or desc or ncm):
            continue

        itens.append({
            "item": item,
            "descricao": desc,
            "cfop": cfop or None,
            "cest": cest or None,
            "ncm": ncm or None,  # ✅ GARANTIDO no retorno
            "campos_raw": {headers[i]: cols[i] for i in range(min(len(headers), len(cols)))},
        })

    return itens


def parse_calculo_itens(html: str) -> List[Dict[str, Any]]:
    """
    Tabela 'Cálculo Itens' (impostos).
    """
    soup = BeautifulSoup(html, "lxml")
    table = _find_table_by_title(soup, "Cálculo Itens")
    if not table:
        return []

    headers = _table_headers(table)
    rows = _table_rows(table)

    # Aqui usamos posição fixa porque seu cálculo tem muitas colunas e nomes podem variar,
    # mas ainda guardamos campos_raw.
    out: List[Dict[str, Any]] = []
    for cols in rows:
        if len(cols) < 2:
            continue
        # completa
        if len(cols) < len(headers):
            cols = cols + [""] * (len(headers) - len(cols))

        def txt(i: int) -> str:
            return cols[i].strip() if i < len(cols) else ""

        def num(i: int) -> Optional[float]:
            return _to_number_ptbr(txt(i))

        item = txt(0)
        if not item:
            continue

        out.append({
            "item": item,
            "produto_sefin": txt(1) or None,  # ex: 8231
            "val_mercadoria": num(2),
            "perc_reducao_bc": num(3),
            "bc_mercadoria": num(4),
            "frete_fob_cte": num(5),
            "bc_frete_fob": num(6),
            "bc_final": num(7),
            "aliq_interna": num(8),
            "aliq_orig_nfe": num(9),
            "aliq_orig_cte": num(10),
            "debito_mercadoria": num(11),
            "debito_frete_fob": num(12),
            "credito_nfe": num(13),
            "credito_cte": num(14),
            "credito_complementar": num(15),
            "valor_a_recolher": num(16),
            "campos_raw": {headers[i]: cols[i] for i in range(min(len(headers), len(cols)))},
        })

    return out


def merge_itens_e_calculo(itens: List[Dict[str, Any]], calculos: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    ✅ MERGE POR ITEM: itens da nota (com NCM) + cálculo itens (imposto)
    """
    calc_by_item: Dict[str, Dict[str, Any]] = {}
    for c in calculos:
        k = str(c.get("item") or "").strip()
        if not k:
            continue
        # se vier duplicado, mantém o primeiro (ou troque a lógica se precisar)
        if k not in calc_by_item:
            calc_by_item[k] = c

    merged: List[Dict[str, Any]] = []
    for it in itens:
        k = str(it.get("item") or "").strip()
        c = calc_by_item.get(k)
        merged.append({
            **it,
            "produto_sefin": c.get("produto_sefin") if c else None,
            "calculo": c or None,
        })

    total_recolher = 0.0
    for m in merged:
        v = (m.get("calculo") or {}).get("valor_a_recolher")
        if isinstance(v, (int, float)):
            total_recolher += float(v)

    return {
        "itens_mesclados": merged,
        "totais": {
            "qtd_itens": len(merged),
            "itens_sem_ncm": sum(1 for x in merged if not x.get("ncm")),
            "total_valor_a_recolher": round(total_recolher, 2),
        }
    }


def extrair_extrato_mesclado(html: str) -> Dict[str, Any]:
    itens = parse_itens_da_nota(html)
    calc = parse_calculo_itens(html)
    merged = merge_itens_e_calculo(itens, calc)

    return {
        "ok": True,
        "itens_da_nota": itens,
        "calculo_itens": calc,
        "merge": merged,
        "meta": {
            "qtd_itens": len(itens),
            "qtd_calculos": len(calc),
        }
    }


def baixar_extrato(sess: requests.Session, url_extrato: str) -> str:
    r = sess.get(url_extrato, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} ao abrir extrato")
    return r.text


# =========================================================
# /extrato
# =========================================================
@app.get("/extrato")
def route_extrato(
    user: str = Query(...),
    codi: str = Query(...),
    url_extrato: str = Query(...),
):
    if not user or "@" not in user:
        raise HTTPException(status_code=400, detail="user inválido.")
    if not codi:
        raise HTTPException(status_code=400, detail="codi obrigatório.")
    if not url_extrato.startswith("http"):
        raise HTTPException(status_code=400, detail="url_extrato deve começar com http/https.")

    certs = carregar_certificados_validos(user)
    if not certs:
        raise HTTPException(status_code=404, detail="Nenhuma empresa/certificado encontrado para este user.")
    cert = selecionar_cert_por_codi(certs, codi)

    empresa = (cert.get("empresa") or "").strip()
    codi_sel = str(cert.get("codi") or "").strip()

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        logger.info("EXTRATO_START | user=%s | codi=%s | empresa=%s", user, codi_sel, empresa)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(status_code=401, detail="Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(status_code=401, detail="Falha ao abrir Portal (LoginToken/home).")

        html = baixar_extrato(sess, url_extrato)

        # ✅ AQUI está a correção: extrai NCM de Itens da nota e faz merge com Cálculo
        data = extrair_extrato_mesclado(html)

        logger.info(
            "EXTRATO_DONE | user=%s | codi=%s | itens=%s | sem_ncm=%s",
            user, codi_sel,
            data.get("merge", {}).get("totais", {}).get("qtd_itens", 0),
            data.get("merge", {}).get("totais", {}).get("itens_sem_ncm", 0),
        )

        return {"ok": True, "user": user, "codi": codi_sel, "empresa": empresa, "result": data}

    except HTTPException:
        raise
    except Exception as e:
        _log_exc(f"EXTRATO_FAIL | user={user} codi={codi}", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for p in (cert_path, key_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


if __name__ == "__main__":
    uvicorn.run("extrato_api:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)
