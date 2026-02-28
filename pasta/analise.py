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
  abre extrato.jsp, extrai TOKEN + USUARIO e CHAVE (se houver)
  abre internamento (capa_internamentos)
  extrai 2 tabelas:
    - "Itens da Nota" (Item, Descrição, CST/CSOSN, CFOP, CEST, NCM)
    - "Cálculo Itens" (Tabela analítica) — 3 estratégias para encontrar
  mescla por ITEM (1..n) e retorna JSON consolidado
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
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA"

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
    if not SUPABASE_URL or not SUPABASE_KEY or SUPABASE_KEY == "CHANGE_ME":
        raise HTTPException(status_code=500, detail="Configure SUPABASE_URL e SUPABASE_KEY no ENV do Render.")


def supabase_headers() -> Dict[str, str]:
    _require_supabase()
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


# =========================================================
# APP
# =========================================================
app = FastAPI(title="analise — API Débitos + Extrato por Produto (Itens + Cálculo consolidado)")

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
# INTERNAMENTO -> TABELAS (VERSÃO ULTRA ROBUSTA)
# =========================================================
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
    h = " | ".join([x.strip().lower() for x in headers if x]).lower()
    score = 0
    for kw, pts in [
        ("itens da nota", 60),
        ("ncm", 50),
        ("cfop", 30),
        ("cest", 20),
        ("cst", 20),
        ("csosn", 20),
        ("descricao", 10),
        ("descr", 10),
        ("item", 10),
    ]:
        if kw in h:
            score += pts
    return score


def _pick_best_table(html_internamento: str, scorer) -> Tuple[Optional[Any], Dict[str, Any]]:
    soup = BeautifulSoup(html_internamento, "lxml")
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


# -------------------------
# Itens da Nota (com CST)
# -------------------------
def _parse_items_da_nota(table) -> List[Dict[str, Any]]:
    all_tr = table.find_all("tr")
    if not all_tr:
        return []

    header_cells: List[str] = []
    header_tr = None
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

    def norm(h: str) -> str:
        h = re.sub(r"\s+", " ", (h or "").strip().lower())
        h = h.replace("á", "a").replace("à", "a").replace("â", "a").replace("ã", "a")
        h = h.replace("é", "e").replace("ê", "e")
        h = h.replace("í", "i")
        h = h.replace("ó", "o").replace("ô", "o").replace("õ", "o")
        h = h.replace("ú", "u")
        h = h.replace("ç", "c")
        return h

    hnorm = [norm(h) for h in header_cells]

    def find_idx_contains(*needles: str) -> Optional[int]:
        needles = [norm(n) for n in needles]
        for i, h in enumerate(hnorm):
            for n in needles:
                if n in h:
                    return i
        return None

    idx_item = find_idx_contains("item")
    idx_desc = find_idx_contains("descricao", "descrição", "descr")
    idx_cst = find_idx_contains("cst", "csosn", "o/cst", "o/csosn")
    idx_cfop = find_idx_contains("cfop")
    idx_cest = find_idx_contains("cest")
    idx_ncm = find_idx_contains("ncm")

    items: List[Dict[str, Any]] = []

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
            if i is None or i >= len(cols) or i < 0:
                return None
            v = (cols[i] or "").strip()
            return v or None

        item_str = get(idx_item)
        if not (item_str and re.fullmatch(r"\d+", item_str.strip())):
            continue

        row = {
            "item": item_str.strip(),
            "descricao": get(idx_desc),
            "cst": get(idx_cst),
            "cfop": get(idx_cfop),
            "cest": get(idx_cest),
            "ncm": get(idx_ncm),
            "cols_raw": cols,
        }

        if not (row["descricao"] or row["ncm"] or row["cfop"] or row["cst"]):
            continue

        items.append(row)

    return items


# =========================================================
# Cálculo Itens: VERSÃO ULTRA ROBUSTA - 3 ESTRATÉGIAS
# =========================================================
def _pick_calculo_table_ultra_robusto(html_internamento: str) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Três estratégias para encontrar a tabela de cálculo:
    1. Buscar por id="calculo-itens-container"
    2. Buscar por heading "Cálculo Itens"
    3. Buscar por tabela com muitas colunas e dados numéricos
    """
    soup = BeautifulSoup(html_internamento, "lxml")
    diag = {
        "tables_found": len(soup.find_all("table")),
        "estrategias": {}
    }
    
    # ESTRATÉGIA 1: Procurar pelo container específico
    container = soup.find("div", {"id": "calculo-itens-container"})
    if container:
        table = container.find("table")
        if table:
            # Verificar se realmente é a tabela de cálculo
            rows = table.find_all("tr")
            for tr in rows:
                tds = tr.find_all("td")
                if len(tds) >= 15:
                    diag["estrategias"]["container_id"] = "ENCONTRADA"
                    return table, diag
    
    diag["estrategias"]["container_id"] = "NAO_ENCONTRADA"
    
    # ESTRATÉGIA 2: Procurar pelo heading "Cálculo Itens"
    for heading in soup.find_all(["h4", "h3", "h2", "h1"]):
        if "Cálculo Itens" in heading.get_text():
            # Encontrou o heading, pegar a próxima tabela
            parent = heading.find_parent("div", class_="flex-container")
            if parent:
                table = parent.find("table")
                if table:
                    diag["estrategias"]["heading"] = "ENCONTRADA"
                    return table, diag
            # Se não achou pelo parent, procurar qualquer tabela próxima
            next_table = heading.find_next("table")
            if next_table:
                diag["estrategias"]["heading"] = "ENCONTRADA (next)"
                return next_table, diag
    
    diag["estrategias"]["heading"] = "NAO_ENCONTRADA"
    
    # ESTRATÉGIA 3: Busca avançada por tabela com muitas colunas
    tables = soup.find_all("table")
    best_table = None
    best_score = -1
    analysis = []
    
    for i, table in enumerate(tables):
        score = 0
        rows_with_many_cols = 0
        numeric_rows = 0
        has_calculo_text = False
        
        # Verificar se tem texto "Cálculo" próximo
        prev_text = table.find_previous(string=True)
        if prev_text and "cálculo" in str(prev_text).lower():
            has_calculo_text = True
            score += 20
        
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            
            # Verificar se tem muitas colunas
            if len(tds) >= 15:
                rows_with_many_cols += 1
                
                # Verificar primeira coluna numérica
                if tds and tds[0].get_text(strip=True).isdigit():
                    numeric_rows += 1
                    
                    # Verificar se tem valores monetários (com vírgula)
                    if len(tds) > 2 and "," in tds[2].get_text():
                        numeric_rows += 2  # Bônus extra
        
        # Pontuação
        score += (rows_with_many_cols * 5) + (numeric_rows * 10)
        
        analysis.append({
            "i": i,
            "rows_with_many_cols": rows_with_many_cols,
            "numeric_rows": numeric_rows,
            "has_calculo_text": has_calculo_text,
            "score": score
        })
        
        if score > best_score:
            best_score = score
            best_table = table
    
    diag["estrategias"]["busca_avancada"] = {
        "best_score": best_score,
        "analysis": analysis
    }
    
    if best_table and best_score > 30:
        return best_table, diag
    
    return None, diag


def _parse_calculo_itens_ultra_robusto(table) -> List[Dict[str, Any]]:
    """
    Parse super robusto que tenta diferentes abordagens:
    1. Tenta 17 colunas exatas
    2. Tenta colspan detection
    3. Fallback para qualquer tabela com dados parecidos
    """
    all_tr = table.find_all("tr")
    if not all_tr:
        return []

    out = []
    
    for tr in all_tr:
        tds = tr.find_all("td")
        if len(tds) < 10:  # Mínimo de colunas
            continue
            
        cols = [td.get_text(" ", strip=True) for td in tds]
        
        # Verificar se a primeira coluna é número (item)
        first_col = cols[0].strip()
        if not first_col or not re.fullmatch(r"\d+", first_col):
            continue
        
        # ESTRATÉGIA A: 17 colunas exatas
        if len(cols) == 17:
            row = {
                "item": cols[0],
                "produto": cols[1],
                "val_mercadoria": cols[2],
                "perc_reducao_base_calc": cols[3],
                "base_calc_mercadoria": cols[4],
                "frete_fob_cte": cols[5],
                "base_calc_frete_fob": cols[6],
                "base_calc_final": cols[7],
                "aliq_interna": cols[8],
                "aliq_orig_nfe": cols[9],
                "aliq_orig_cte": cols[10],
                "val_debito_mercadoria": cols[11],
                "val_debito_frete_fob": cols[12],
                "val_credito_nfe": cols[13],
                "val_credito_cte": cols[14],
                "val_cred_complementar": cols[15],
                "valor_a_recolher": cols[16],
            }
            out.append(row)
            continue
        
        # ESTRATÉGIA B: Tentar detectar por padrão de valores (15+ colunas)
        if len(cols) >= 15:
            # Tentar adivinhar as colunas baseado no conteúdo
            row = {
                "item": cols[0],
                "produto": cols[1] if len(cols) > 1 else None,
                "val_mercadoria": cols[2] if len(cols) > 2 else None,
                "perc_reducao_base_calc": cols[3] if len(cols) > 3 else None,
                "base_calc_mercadoria": cols[4] if len(cols) > 4 else None,
                "frete_fob_cte": cols[5] if len(cols) > 5 else None,
                "base_calc_frete_fob": cols[6] if len(cols) > 6 else None,
                "base_calc_final": cols[7] if len(cols) > 7 else None,
                "aliq_interna": cols[8] if len(cols) > 8 else None,
                "aliq_orig_nfe": cols[9] if len(cols) > 9 else None,
                "aliq_orig_cte": cols[10] if len(cols) > 10 else None,
                "val_debito_mercadoria": cols[11] if len(cols) > 11 else None,
                "val_debito_frete_fob": cols[12] if len(cols) > 12 else None,
                "val_credito_nfe": cols[13] if len(cols) > 13 else None,
                "val_credito_cte": cols[14] if len(cols) > 14 else None,
                "val_cred_complementar": cols[15] if len(cols) > 15 else None,
                "valor_a_recolher": cols[16] if len(cols) > 16 else None,
            }
            
            # Verificar se parece uma linha de cálculo (tem valores com vírgula)
            if any(val and "," in val for val in [row["val_mercadoria"], row["valor_a_recolher"]]):
                out.append(row)
    
    return out


# =========================================================
# MERGE CONSOLIDADO (um JSON só)
# =========================================================
def _merge_itens_com_calculo(
    itens_nota: List[Dict[str, Any]],
    calculo: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    calc_by_item: Dict[str, Dict[str, Any]] = {}
    for c in calculo or []:
        it = str(c.get("item") or "").strip()
        if it and it not in calc_by_item:
            calc_by_item[it] = c

    merged: List[Dict[str, Any]] = []
    with_calc = 0

    for it in itens_nota or []:
        k = str(it.get("item") or "").strip()
        c = calc_by_item.get(k)

        out = {
            "item": k or None,
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
            out.update(
                {
                    "produto": c.get("produto"),
                    "val_merc": c.get("val_mercadoria"),
                    "perc_red": c.get("perc_reducao_base_calc"),
                    "bc_merc": c.get("base_calc_mercadoria"),
                    "frete": c.get("frete_fob_cte"),
                    "bc_frete": c.get("base_calc_frete_fob"),
                    "bc_final": c.get("base_calc_final"),
                    "perc_int": c.get("aliq_interna"),
                    "perc_nfe": c.get("aliq_orig_nfe"),
                    "perc_cte": c.get("aliq_orig_cte"),
                    "deb_merc": c.get("val_debito_mercadoria"),
                    "deb_frete": c.get("val_debito_frete_fob"),
                    "cred_nfe": c.get("val_credito_nfe"),
                    "cred_cte": c.get("val_credito_cte"),
                    "cred_compl": c.get("val_cred_complementar"),
                    "a_recolher": c.get("valor_a_recolher"),
                }
            )

        merged.append(out)

    diag = {
        "itens_nota": len(itens_nota or []),
        "linhas_calculo": len(calculo or []),
        "itens_com_calculo": with_calc,
        "itens_sem_calculo": (len(itens_nota or []) - with_calc),
    }
    return merged, diag


# =========================================================
# ROUTE: EXTRATO PRODUTO (JSON CONSOLIDADO) - VERSÃO FINAL
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

        if not chave_alvo:
            raise HTTPException(status_code=400, detail="Não encontrei chave NFe (44 dígitos) no extrato.jsp e não foi informada em 'chave'.")

        url_capa = _montar_url_capa_internamento(usuario=usuario, chave=chave_alvo, token=token)

        r2 = sess.get(url_capa, timeout=60, allow_redirects=True)
        if r2.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Falha ao abrir internamento: HTTP {r2.status_code}")

        html_intern = r2.text
        final_url = r2.url

        # 1) Itens da Nota
        tab_itens, diag_itens = _pick_best_table(html_intern, _score_headers_itens)
        itens_nota: List[Dict[str, Any]] = []
        if tab_itens and (diag_itens.get("best_score", 0) >= 60):
            itens_nota = _parse_items_da_nota(tab_itens)

        # 2) Cálculo Itens - VERSÃO ULTRA ROBUSTA
        tab_calc, diag_calc = _pick_calculo_table_ultra_robusto(html_intern)
        calc_itens: List[Dict[str, Any]] = []
        if tab_calc:
            calc_itens = _parse_calculo_itens_ultra_robusto(tab_calc)

        # 3) Merge consolidado
        itens_consolidados, diag_merge = _merge_itens_com_calculo(itens_nota, calc_itens)

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
                "chave": chave_alvo,

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
