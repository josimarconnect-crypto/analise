# -*- coding: utf-8 -*-
"""
analise.py — FastAPI (Render)
✅ Rotas:
- /health
- /empresas?user=...
- /debitos?user=...&codi=...&incluir_ano_anterior=1
- /extrato-produto?user=...&codi=...&url_extrato=...&chave=...

✅ Login: DET -> Portal usando mTLS (cert pem/key do Supabase)
✅ /extrato-produto:
  abre extrato.jsp, extrai TOKEN + USUARIO e CHAVE (44 dígitos) (se existir)
  abre internamento (capa/processamentos/show)
  extrai e mescla por ITEM (1ª coluna) usando 2 tabelas analíticas:
    1) "Itens da Nota"  -> Item, Descrição, CST, CFOP, CEST, NCM
    2) "Cálculo Itens"  -> Item, Produto, Val. Merc., %Red, BC Merc., Frete, BC Frete, BC Final,
                           %Int, %NFE, %CTE, Déb Merc, Déb Frete, Cred NFE, Cred CTE, Cred Compl, A Recolher

✅ Correção principal:
- A tabela "Cálculo Itens" é encontrada por seletor/estrutura:
  - #calculo-itens-container table
  - OU presença de tr.itens-calculos-row
  Assim não depende de "chave no último quadro" e não falha por score.
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
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://hysrxadnigzqadnlkynq.supabase.co").strip()
SUPABASE_KEY = os.getenv(
    "SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA",
).strip()

TABELA_CERTS = os.getenv("TABELA_CERTS", "certifica_dfe").strip()

DEBUG_ERRORS = os.getenv("DEBUG_ERRORS", "1") == "1"
LOG_FILE = os.getenv("LOG_FILE", "/tmp/analise.log" if os.getenv("RENDER") else "analise.log")

# URLs DET / PORTAL / INTERNAMENTO
URL_DET_HOME = "https://detsec.sefin.ro.gov.br/certificados"
URL_ENTRAR = "https://detsec.sefin.ro.gov.br/entrar"
URL_REDIRECT_PORTAL = "https://detsec.sefin.ro.gov.br/contribuinte/notificacoes/redirect_portal"
URL_PORTAL_HOME_DEFAULT = "https://portalcontribuinte.sefin.ro.gov.br/app/home/?exibir_modal=true"

URL_CONSULTA_DEBITOS = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/"
URL_CONSULTA_DEBITOS_LISTA = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/lista.jsp"

BASE_INTERNAMENTO = "https://internamentonotas.sefin.ro.gov.br"


# =========================================================
# LOG
# =========================================================
logger = logging.getLogger("analise")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=500, detail="Configure SUPABASE_URL e SUPABASE_KEY no ENV do Render.")


def supabase_headers() -> Dict[str, str]:
    _require_supabase()
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


# =========================================================
# APP
# =========================================================
app = FastAPI(title="API Débitos + Internamento Itens (Consolidado) — SEFIN RO")

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
        "service": "analise",
        "date_utc": _now_iso(),
        "routes": ["/health", "/empresas", "/debitos", "/extrato-produto"],
        "log_file": LOG_FILE,
    }


@app.get("/health")
def health():
    return {"ok": True, "date_utc": _now_iso()}


# =========================================================
# SUPABASE CERTS
# =========================================================
def carregar_certificados(user_filter: str) -> List[Dict[str, Any]]:
    url = f"{SUPABASE_URL}/rest/v1/{TABELA_CERTS}"
    params: Dict[str, str] = {
        "select": 'id,pem,key,empresa,codi,user,vencimento,"cnpj/cpf"',
        "user": f"eq.{user_filter}",
        "order": "id.desc",
        "limit": "100",
    }
    r = requests.get(url, headers=supabase_headers(), params=params, timeout=30)
    if r.status_code >= 300:
        raise HTTPException(status_code=400, detail=f"Supabase REST falhou: {r.text}")
    return r.json() or []


def selecionar_cert_por_codi(certs: List[Dict[str, Any]], codi: str) -> Dict[str, Any]:
    codi = (codi or "").strip()
    if not certs:
        raise HTTPException(status_code=404, detail="Nenhum certificado encontrado para este user.")
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
    certs = carregar_certificados(user)
    out = []
    seen = set()
    for c in certs:
        codi = str(c.get("codi") or "").strip()
        if not codi or codi in seen:
            continue
        seen.add(codi)
        out.append(
            {
                "codi": codi,
                "empresa": (c.get("empresa") or "").strip(),
                "cnpj": (c.get("cnpj/cpf") or "").strip(),
                "vencimento": (c.get("vencimento") or ""),
            }
        )
    return {"ok": True, "user": user, "total": len(out), "empresas": out}


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
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return s


# =========================================================
# DET / PORTAL
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
                if name:
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
# DÉBITOS
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
def route_debitos(
    user: str = Query(...),
    codi: str = Query(...),
    incluir_ano_anterior: int = Query(1),
):
    if not user or "@" not in user:
        raise HTTPException(status_code=400, detail="user inválido.")
    if not codi:
        raise HTTPException(status_code=400, detail="codi obrigatório.")

    certs = carregar_certificados(user)
    cert = selecionar_cert_por_codi(certs, codi)

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(status_code=401, detail="Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(status_code=401, detail="Falha ao abrir Portal.")

        ano_atual = date.today().year
        anos = [ano_atual] + ([ano_atual - 1] if incluir_ano_anterior == 1 else [])

        all_debs: List[Dict[str, str]] = []
        for a in anos:
            debs, _ = consultar_debitos_ano(sess, a)
            all_debs.extend(debs or [])

        return {
            "ok": True,
            "user": user,
            "codi": str(cert.get("codi") or ""),
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


# =========================================================
# EXTRATO -> TOKEN/USUARIO/CHAVE
# =========================================================
def _extrair_token_e_usuario_do_html_extrato(html_extrato: str) -> Tuple[Optional[str], Optional[str]]:
    m1 = re.search(r"var\s+TOKEN\s*=\s*'([^']+)'", html_extrato)
    token = m1.group(1).strip() if m1 else None

    m2 = re.search(r"Ol[áa]\s*<strong>\s*([0-9]{11})\s*-", html_extrato, flags=re.I)
    usuario = m2.group(1).strip() if m2 else None

    return token, usuario


def _extrair_chaves_do_extrato(html_extrato: str) -> List[str]:
    chaves = re.findall(r"\b\d{44}\b", html_extrato)
    out, seen = [], set()
    for c in chaves:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def _montar_url_capa_internamento(usuario: str, chave: str, token: Optional[str]) -> str:
    base = f"{BASE_INTERNAMENTO}/capa_internamentos/{usuario}/{chave}"
    if token:
        return base + "?token=" + requests.utils.quote(token, safe="")
    return base


# =========================================================
# INTERNAMENTO -> TABELAS (ACHAR + PARSE)
# =========================================================
def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("á", "a").replace("à", "a").replace("â", "a").replace("ã", "a")
    s = s.replace("é", "e").replace("ê", "e")
    s = s.replace("í", "i")
    s = s.replace("ó", "o").replace("ô", "o").replace("õ", "o")
    s = s.replace("ú", "u")
    s = s.replace("ç", "c")
    s = re.sub(r"\s+", " ", s)
    return s


def _extract_table_headers(table) -> List[str]:
    ths = table.find_all("th")
    if ths:
        return [th.get_text(" ", strip=True) for th in ths]
    tr = table.find("tr")
    if tr:
        cells = tr.find_all(["td", "th"])
        return [c.get_text(" ", strip=True) for c in cells]
    return []


def _score_headers_itens(headers: List[str]) -> int:
    h = _norm(" | ".join([x for x in headers if x]))
    score = 0
    for kw, pts in [
        ("itens da nota", 70),
        ("o/cst", 40),
        ("csosn", 30),
        ("cst", 30),
        ("ncm", 50),
        ("cfop", 30),
        ("cest", 20),
        ("produto sefin", 20),
        ("descricao", 10),
        ("item", 10),
    ]:
        if kw in h:
            score += pts
    return score


def _pick_best_table_by_score(html: str, scorer) -> Tuple[Optional[Any], Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    best = None
    best_score = -1
    diag = {"tables_found": len(tables), "scores": []}

    for i, t in enumerate(tables):
        headers = _extract_table_headers(t)
        sc = scorer(headers)
        diag["scores"].append({"i": i, "score": sc, "headers_sample": headers[:30]})
        if sc > best_score:
            best_score = sc
            best = t

    diag["best_score"] = best_score
    return best, diag


def _find_calculo_table(html: str) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Encontra a tabela 'Cálculo Itens' por estrutura real do HTML:
    - #calculo-itens-container table
    - OU tabela que contenha tr.itens-calculos-row
    """
    soup = BeautifulSoup(html, "lxml")

    tab = soup.select_one("#calculo-itens-container table")
    if tab:
        return tab, {"method": "css:#calculo-itens-container table", "found": True}

    row = soup.select_one("tr.itens-calculos-row")
    if row:
        tab2 = row.find_parent("table")
        if tab2:
            return tab2, {"method": "row:tr.itens-calculos-row -> parent table", "found": True}

    # fallback: por header keywords (caso mudem id/classes)
    tab3, diag = _pick_best_table_by_score(html, _score_headers_calculo_fallback)
    diag2 = {"method": "fallback:score_headers_calculo", "found": bool(tab3), "fallback_diag": diag}
    return tab3, diag2


def _score_headers_calculo_fallback(headers: List[str]) -> int:
    h = _norm(" | ".join([x for x in headers if x]))
    score = 0
    for kw, pts in [
        ("calculo itens", 80),
        ("val. mercadoria", 60),
        ("base calc. mercadoria", 50),
        ("frete fob", 40),
        ("val. debito mercadoria", 40),
        ("val. credito nfe", 30),
        ("val. a recolher", 80),
        ("item", 15),
        ("produto", 15),
    ]:
        if kw in h:
            score += pts
    return score


def _parse_itens_da_nota(table) -> List[Dict[str, Any]]:
    """
    Tabela: Item | Descrição | O/CST ou O/CSOSN | CFOP | CEST | NCM | ...
    """
    all_tr = table.find_all("tr")
    if not all_tr:
        return []

    header_tr = None
    header_cells: List[str] = []
    for tr in all_tr[:15]:
        ths = tr.find_all("th")
        if ths and len(ths) >= 6:
            header_cells = [th.get_text(" ", strip=True) for th in ths]
            header_tr = tr
            break

    if not header_cells:
        cells = all_tr[0].find_all(["td", "th"])
        header_cells = [c.get_text(" ", strip=True) for c in cells]
        header_tr = all_tr[0]

    hnorm = [_norm(h) for h in header_cells]

    def find_idx(*parts: str) -> Optional[int]:
        for i, h in enumerate(hnorm):
            for p in parts:
                if _norm(p) in h:
                    return i
        return None

    idx_item = find_idx("item")
    idx_desc = find_idx("descricao", "descr", "descrição")
    idx_cst = find_idx("cst", "csosn", "o/cst", "o/csosn", "o/cst ou o/csosn")
    idx_cfop = find_idx("cfop")
    idx_cest = find_idx("cest")
    idx_ncm = find_idx("ncm")

    out: List[Dict[str, Any]] = []
    for tr in all_tr:
        if tr == header_tr:
            continue
        tds = tr.find_all("td")
        if not tds:
            continue
        cols = [td.get_text(" ", strip=True) for td in tds]
        if len(cols) < 6:
            continue

        def get(i: Optional[int]) -> Optional[str]:
            if i is None or i >= len(cols):
                return None
            v = (cols[i] or "").strip()
            return v or None

        item = get(idx_item)
        if not (item and re.fullmatch(r"\d+", item.strip())):
            continue

        row = {
            "item": item.strip(),
            "descricao": get(idx_desc),
            "cst": get(idx_cst),
            "cfop": get(idx_cfop),
            "cest": get(idx_cest),
            "ncm": get(idx_ncm),
        }

        if not (row["descricao"] or row["ncm"]):
            continue

        out.append(row)

    return out


def _parse_calculo_itens(table) -> List[Dict[str, Any]]:
    """
    Tabela "Cálculo Itens" (analítica), com 17 colunas:
    Item, Produto, Val. Mercadoria, % Redução, BC Merc, Frete, BC Frete, BC Final,
    %Int, %NFE, %CTE, Déb Merc, Déb Frete, Cred NFE, Cred CTE, Cred Compl, A Recolher
    """

    all_tr = table.find_all("tr")
    if not all_tr:
        return []

    # acha header "melhor"
    header_tr = None
    header_cells: List[str] = []
    best = (-1, -1)  # (qtd_th, score_kw)
    for tr in all_tr[:30]:
        ths = tr.find_all("th")
        if not ths:
            continue
        cells = [th.get_text(" ", strip=True) for th in ths]
        hjoin = _norm(" | ".join(cells))
        score = 0
        for kw in ["item", "produto", "val. mercadoria", "base calc", "frete", "credito", "debito", "recolher"]:
            if kw in hjoin:
                score += 1
        key = (len(ths), score)
        if key > best:
            best = key
            header_tr = tr
            header_cells = cells

    if not header_tr or len(header_cells) < 8:
        return []

    hnorm = [_norm(h) for h in header_cells]

    def find_idx_regex(*patterns: str) -> Optional[int]:
        for i, h in enumerate(hnorm):
            for pat in patterns:
                if re.search(pat, h):
                    return i
        return None

    idx_item = find_idx_regex(r"\bitem\b")
    idx_produto = find_idx_regex(r"\bproduto\b")
    idx_val_merc = find_idx_regex(r"val\.?\s*mercadoria", r"\bmercadoria\b")
    idx_red = find_idx_regex(r"reduc", r"%\s*reduc")
    idx_bc_merc = find_idx_regex(r"base\s*calc.*merc", r"base\s*calc\.?\s*mercadoria")
    idx_frete = find_idx_regex(r"frete")
    idx_bc_frete = find_idx_regex(r"base\s*calc.*frete", r"base\s*calc.*fob")
    idx_bc_final = find_idx_regex(r"base\s*calc\.?\s*final")
    idx_perc_int = find_idx_regex(r"aliq.*interna", r"\binterna\b")
    idx_perc_nfe = find_idx_regex(r"orig.*nfe")
    idx_perc_cte = find_idx_regex(r"orig.*cte")
    idx_deb_merc = find_idx_regex(r"debito.*merc", r"debito\s*mercadoria")
    idx_deb_frete = find_idx_regex(r"debito.*frete")
    idx_cred_nfe = find_idx_regex(r"credito.*nfe")
    idx_cred_cte = find_idx_regex(r"credito.*cte")
    idx_cred_compl = find_idx_regex(r"cred.*compl", r"credito.*compl", r"comple")
    idx_recolher = find_idx_regex(r"recolher", r"a\s*recolher")

    def safe_get(cols: List[str], i: Optional[int]) -> Optional[str]:
        if i is None or i < 0 or i >= len(cols):
            return None
        v = (cols[i] or "").strip()
        return v or None

    # preferir as linhas reais do cálculo
    trs = table.select("tr.itens-calculos-row") or [tr for tr in all_tr if tr != header_tr]

    out: List[Dict[str, Any]] = []
    for tr in trs:
        tds = tr.find_all("td")
        if not tds:
            continue
        cols = [td.get_text(" ", strip=True) for td in tds]

        item = safe_get(cols, idx_item)
        if not (item and re.fullmatch(r"\d+", item.strip())):
            continue

        out.append(
            {
                "item": item.strip(),
                "produto": safe_get(cols, idx_produto),
                "val_merc": safe_get(cols, idx_val_merc),
                "perc_red": safe_get(cols, idx_red),
                "bc_merc": safe_get(cols, idx_bc_merc),
                "frete": safe_get(cols, idx_frete),
                "bc_frete": safe_get(cols, idx_bc_frete),
                "bc_final": safe_get(cols, idx_bc_final),
                "perc_int": safe_get(cols, idx_perc_int),
                "perc_nfe": safe_get(cols, idx_perc_nfe),
                "perc_cte": safe_get(cols, idx_perc_cte),
                "deb_merc": safe_get(cols, idx_deb_merc),
                "deb_frete": safe_get(cols, idx_deb_frete),
                "cred_nfe": safe_get(cols, idx_cred_nfe),
                "cred_cte": safe_get(cols, idx_cred_cte),
                "cred_compl": safe_get(cols, idx_cred_compl),
                "a_recolher": safe_get(cols, idx_recolher),
            }
        )

    return out


def _merge_consolidado(
    itens_nota: List[Dict[str, Any]],
    calc: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    calc_by_item: Dict[str, Dict[str, Any]] = {}
    for c in calc or []:
        it = str(c.get("item") or "").strip()
        if it and it not in calc_by_item:
            calc_by_item[it] = c

    merged: List[Dict[str, Any]] = []
    with_calc = 0

    for it in itens_nota or []:
        item_key = str(it.get("item") or "").strip()
        c = calc_by_item.get(item_key)

        row = {
            "item": item_key or None,
            "descricao": it.get("descricao"),
            "cst": it.get("cst"),
            "cfop": it.get("cfop"),
            "cest": it.get("cest"),
            "ncm": it.get("ncm"),

            "produto": None,
            "val_merc": None,
            "perc_red": None,
            "bc_merc": None,
            "frete": None,
            "bc_frete": None,
            "bc_final": None,
            "perc_int": None,
            "perc_nfe": None,
            "perc_cte": None,
            "deb_merc": None,
            "deb_frete": None,
            "cred_nfe": None,
            "cred_cte": None,
            "cred_compl": None,
            "a_recolher": None,
        }

        if c:
            with_calc += 1
            for k in [
                "produto",
                "val_merc",
                "perc_red",
                "bc_merc",
                "frete",
                "bc_frete",
                "bc_final",
                "perc_int",
                "perc_nfe",
                "perc_cte",
                "deb_merc",
                "deb_frete",
                "cred_nfe",
                "cred_cte",
                "cred_compl",
                "a_recolher",
            ]:
                row[k] = c.get(k)

        merged.append(row)

    diag = {
        "itens_nota": len(itens_nota or []),
        "linhas_calculo": len(calc or []),
        "itens_com_calculo": with_calc,
        "itens_sem_calculo": (len(itens_nota or []) - with_calc),
    }
    return merged, diag


# =========================================================
# ROUTE: EXTRATO PRODUTO (JSON CONSOLIDADO)
# =========================================================
@app.get("/extrato-produto")
def extrato_produto(
    user: str = Query(...),
    codi: str = Query(...),
    url_extrato: str = Query(...),
    chave: str = Query(""),
):
    if not user or "@" not in user:
        raise HTTPException(status_code=400, detail="user inválido.")
    if not codi:
        raise HTTPException(status_code=400, detail="codi obrigatório.")
    if not url_extrato.startswith("http"):
        raise HTTPException(status_code=400, detail="url_extrato deve começar com http/https.")

    certs = carregar_certificados(user)
    cert = selecionar_cert_por_codi(certs, codi)

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(status_code=401, detail="Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(status_code=401, detail="Falha ao abrir Portal (LoginToken/home).")

        url_extrato_clean = (url_extrato or "").strip().replace("%22", "").split("#", 1)[0]
        r = sess.get(url_extrato_clean, timeout=45, allow_redirects=True)
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Erro ao abrir extrato.jsp: HTTP {r.status_code}")

        html_extrato = r.text
        token, usuario = _extrair_token_e_usuario_do_html_extrato(html_extrato)
        if not usuario:
            raise HTTPException(status_code=400, detail="Não consegui extrair USUARIO/CPF do extrato.jsp.")

        chaves = _extrair_chaves_do_extrato(html_extrato)
        chave_alvo = (chave or "").strip() or (chaves[0] if chaves else "")

        # tenta abrir internamento (preferencialmente via capa)
        final_url = None
        html_intern = None

        if chave_alvo:
            url_capa = _montar_url_capa_internamento(usuario=usuario, chave=chave_alvo, token=token)
            r2 = sess.get(url_capa, timeout=60, allow_redirects=True)
            if r2.status_code == 200:
                html_intern = r2.text
                final_url = r2.url

        # fallback (se não abriu via capa por algum motivo): tenta pegar um link de processamentos/show do próprio extrato
        if not html_intern:
            mshow = re.search(r"https://internamentonotas\.sefin\.ro\.gov\.br/processamentos/show\?[^\"'\s<>]+", html_extrato)
            if mshow:
                r3 = sess.get(mshow.group(0), timeout=60, allow_redirects=True)
                if r3.status_code == 200:
                    html_intern = r3.text
                    final_url = r3.url

        if not html_intern:
            raise HTTPException(status_code=400, detail="Falha ao abrir internamento (capa/processamentos/show).")

        # 1) Itens da Nota (score)
        tab_itens, diag_itens = _pick_best_table_by_score(html_intern, _score_headers_itens)
        itens_nota: List[Dict[str, Any]] = []
        if tab_itens and (diag_itens.get("best_score", 0) >= 80):
            itens_nota = _parse_itens_da_nota(tab_itens)

        # 2) Cálculo Itens (por estrutura real)
        tab_calc, diag_calc = _find_calculo_table(html_intern)
        calc_itens: List[Dict[str, Any]] = []
        if tab_calc:
            calc_itens = _parse_calculo_itens(tab_calc)

        # 3) Merge consolidado
        itens_consolidados, diag_merge = _merge_consolidado(itens_nota, calc_itens)

        return {
            "ok": True,
            "user": user,
            "codi": str(cert.get("codi") or ""),
            "empresa": cert.get("empresa") or "",
            "result": {
                "ok": True,
                "final_url": final_url,
                "token_found": bool(token),
                "usuario": usuario,
                "chave": chave_alvo or None,
                "itens_consolidados": itens_consolidados,
                "totais": {
                    "qtd_itens": len(itens_consolidados),
                    "linhas_calculo": len(calc_itens),
                    "itens_com_calculo": diag_merge.get("itens_com_calculo", 0),
                    "itens_sem_calculo": diag_merge.get("itens_sem_calculo", 0),
                    "itens_sem_ncm": sum(1 for x in itens_consolidados if not x.get("ncm")),
                },
                "diagnostico": {
                    "itens_da_nota": diag_itens,
                    "calculo_itens": diag_calc,
                    "merge": diag_merge,
                },
            },
        }

    finally:
        for p in (cert_path, key_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


if __name__ == "__main__":
    uvicorn.run("analise:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)
