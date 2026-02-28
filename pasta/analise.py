# -*- coding: utf-8 -*-
"""
extrato_api.py (FastAPI) — Extrato por Produto (NCM/CFOP/CEST) via Portal SEFIN RO

O que faz:
- Recebe user (email do Bubble/Supabase) e url_extrato (extrato.jsp?inscricaoEstadual=...&numeroGuia=...)
- Usa certificado (pem/key) do Supabase para autenticar no DET/Portal
- Abre o extrato.jsp e extrai TOKEN + USUARIO/CPF (igual ao JS abrirCapaInternamento)
- Monta a URL do Internamento: /capa_internamentos/<usuario>/<chave>?token=<token>
- Segue redirects até /processamentos/show
- Extrai a tabela "Itens da Nota" com NCM/CFOP/CEST e devolve JSON

Deploy Render:
- Start command: uvicorn extrato_api:app --host 0.0.0.0 --port $PORT
"""

import os
import re
import json
import base64
import tempfile
import traceback
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware


# =========================
# CONFIG
# =========================
SUPABASE_URL = "https://hysrxadnigzqadnlkynq.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA"
TABELA_CERTS = os.getenv("TABELA_CERTS", "certifica_dfe")

URL_DET_HOME = "https://detsec.sefin.ro.gov.br/certificados"
URL_ENTRAR = "https://detsec.sefin.ro.gov.br/entrar"
URL_REDIRECT_PORTAL = "https://detsec.sefin.ro.gov.br/contribuinte/notificacoes/redirect_portal"
URL_PORTAL_HOME_DEFAULT = "https://portalcontribuinte.sefin.ro.gov.br/app/home/?exibir_modal=true"

BASE_INTERNAMENTO = "https://internamentonotas.sefin.ro.gov.br"

DEBUG_ERRORS = os.getenv("DEBUG_ERRORS", "1") == "1"
LOG_FILE = os.getenv("LOG_FILE", "/tmp/extrato_api.log" if os.getenv("RENDER") else "extrato_api.log")

logger = logging.getLogger("extrato_api")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_exc(prefix: str, exc: Exception):
    try:
        logger.error("%s | %s\n%s", prefix, str(exc), traceback.format_exc())
    except Exception:
        pass


def supabase_headers() -> Dict[str, str]:
    if not SUPABASE_KEY:
        raise HTTPException(status_code=500, detail="SUPABASE_KEY não configurada no ENV.")
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def carregar_certificado_por_user_e_codi(user_email: str, codi: Optional[str]) -> Dict[str, Any]:
    """
    Busca 1 certificado (pem/key) no Supabase.
    Se codi vier vazio, pega o primeiro.
    """
    url = f"{SUPABASE_URL}/rest/v1/{TABELA_CERTS}"
    params: Dict[str, str] = {
        "select": 'id,pem,key,empresa,codi,user,vencimento,"cnpj/cpf"',
        "user": f"eq.{user_email}",
        "limit": "50",
    }
    r = requests.get(url, headers=supabase_headers(), params=params, timeout=30)
    r.raise_for_status()
    rows = r.json() or []
    if not rows:
        raise HTTPException(status_code=404, detail="Nenhum certificado encontrado para este user.")

    if codi:
        codi = str(codi).strip()
        for row in rows:
            if str(row.get("codi") or "").strip() == codi:
                return row

        raise HTTPException(status_code=404, detail=f"Nenhum certificado encontrado para o CODI={codi}.")

    return rows[0]


def criar_arquivos_cert_temp(cert_row: Dict[str, Any]) -> Tuple[str, str]:
    pem_bytes = base64.b64decode(cert_row.get("pem") or "")
    key_bytes = base64.b64decode(cert_row.get("key") or "")

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


# =========================
# LOGIN DET -> PORTAL
# =========================
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
    if r_ent.status_code != 200 or "/certificado/acessos" not in r_ent.url:
        return False

    return True


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


def ir_para_portal_e_carregar_home(sess: requests.Session) -> Optional[str]:
    r_red = sess.get(URL_REDIRECT_PORTAL, timeout=30, allow_redirects=True)
    if r_red.status_code != 200:
        return None

    action_form, data_form = _extrair_form_logintoken(r_red.text)
    if action_form:
        r_login = sess.post(action_form, data=data_form, timeout=30, allow_redirects=True)
        if r_login.status_code == 200 and "LoginToken" not in r_login.url:
            return r_login.text
        if r_login.status_code == 200 and "LoginToken" in r_login.url:
            next_url = _extrair_redirect_do_logintoken(r_login.text) or URL_PORTAL_HOME_DEFAULT
            r_home = sess.get(next_url, timeout=30, allow_redirects=True)
            if r_home.status_code == 200 and "portalcontribuinte.sefin.ro.gov.br" in r_home.url:
                return r_home.text

    r_portal = sess.get(URL_PORTAL_HOME_DEFAULT, timeout=30, allow_redirects=True)
    if r_portal.status_code == 200 and "LoginToken" not in r_portal.url:
        return r_portal.text

    return None


# =========================
# EXTRATO.JSP -> TOKEN/USUARIO/CHAVE
# =========================
def _parse_url_extrato_params(url_extrato: str) -> Dict[str, str]:
    """
    Ex: extrato.jsp?PrimeiraVez=S&inscricaoEstadual=00000006598307&numeroGuia=20261600155408
    """
    pr = urlparse(url_extrato)
    qs = parse_qs(pr.query)
    insc = (qs.get("inscricaoEstadual", [""])[0] or "").strip()
    guia = (qs.get("numeroGuia", [""])[0] or "").strip()
    return {"inscricaoEstadual": insc, "numeroGuia": guia}


def _extrair_token_e_usuario_do_html_extrato(html_extrato: str) -> Tuple[Optional[str], Optional[str]]:
    """
    No HTML do extrato.jsp, existe a função:
      var TOKEN = '...';
      var CPF_CLIENTE = ('06311645297' || '').replace(/\D/g, '');
      var USUARIO = CPF_CLIENTE;

    Isso é exatamente o que precisamos (mesma lógica do "abrir em outra aba"). :contentReference[oaicite:4]{index=4}
    """
    # TOKEN
    m1 = re.search(r"var\s+TOKEN\s*=\s*'([^']+)'", html_extrato)
    token = m1.group(1).strip() if m1 else None

    # CPF/USUARIO (usa "Olá <strong>06311645297 - ...</strong>")
    m2 = re.search(r"Ol[áa]\s*<strong>\s*([0-9]{11})\s*-", html_extrato, flags=re.I)
    usuario = m2.group(1).strip() if m2 else None

    return token, usuario


def _extrair_chaves_do_extrato(html_extrato: str) -> List[str]:
    """
    A tabela do extrato.jsp tem linhas com a chave NFe no 1º TD (150 width no HTML salvo). :contentReference[oaicite:5]{index=5}
    Vamos coletar todas as chaves (44 dígitos).
    """
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


# =========================
# INTERNAMENTO -> ITENS COM NCM
# =========================
def _score_headers(headers: List[str]) -> int:
    h = " | ".join([x.strip().lower() for x in headers if x]).lower()
    score = 0
    for kw, pts in [
        ("itens da nota", 50),
        ("ncm", 40),
        ("cfop", 25),
        ("cest", 20),
        ("produto sefin", 20),
        ("produto", 8),
        ("descr", 8),
        ("valor", 5),
    ]:
        if kw in h:
            score += pts
    return score


def _extract_table_headers(table) -> List[str]:
    # tenta th primeiro
    ths = table.find_all("th")
    if ths:
        return [th.get_text(" ", strip=True) for th in ths]
    # fallback: primeira linha tds
    tr = table.find("tr")
    if tr:
        tds = tr.find_all("td")
        return [td.get_text(" ", strip=True) for td in tds]
    return []


def _pick_best_items_table(html_internamento: str) -> Tuple[Optional[Any], Dict[str, Any]]:
    soup = BeautifulSoup(html_internamento, "lxml")
    tables = soup.find_all("table")
    best = None
    best_score = -1
    diag = {"tables_found": len(tables), "scores": []}

    for i, t in enumerate(tables):
        headers = _extract_table_headers(t)
        sc = _score_headers(headers)
        diag["scores"].append({"i": i, "score": sc, "headers_sample": headers[:30]})
        if sc > best_score:
            best_score = sc
            best = t

    diag["best_score"] = best_score
    return best, diag


def _parse_decimal_br(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip()
    # remove tudo que não dígito, ponto, vírgula, menos
    s = re.sub(r"[^0-9,\.-]+", "", s)
    if not s:
        return None
    # padrão BR: 5.633,65
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def _parse_items_from_table(table) -> List[Dict[str, Any]]:
    """
    Lê linhas (tr) e tenta mapear colunas do jeito mais robusto possível.
    Se a tabela tiver header "NCM/CFOP/CEST", vamos localizar índices por nome.
    """
    # header “de verdade” geralmente fica num thead ou nas primeiras linhas
    # vamos montar um header linear e procurar índices
    all_tr = table.find_all("tr")
    if not all_tr:
        return []

    # coleta uma linha de header “boa”
    header_cells = []
    header_tr = None
    for tr in all_tr[:8]:
        ths = tr.find_all("th")
        if ths and len(ths) >= 6:
            header_cells = [th.get_text(" ", strip=True) for th in ths]
            header_tr = tr
            break

    # se não achou, tenta primeiro tr com tds
    if not header_cells:
        tds = all_tr[0].find_all(["td", "th"])
        header_cells = [x.get_text(" ", strip=True) for x in tds]
        header_tr = all_tr[0]

    # normaliza header
    hnorm = [re.sub(r"\s+", " ", (h or "").strip()).lower() for h in header_cells]

    def find_idx(*names: str) -> Optional[int]:
        for n in names:
            n = n.lower()
            for i, h in enumerate(hnorm):
                if n in h:
                    return i
        return None

    idx_item = find_idx("item")
    idx_desc = find_idx("descr", "descrição")
    idx_cfop = find_idx("cfop")
    idx_cest = find_idx("cest")
    idx_ncm = find_idx("ncm")
    idx_prod_sefin = find_idx("produto sefin", "produto  sefin", "produto sefin sugerido", "produto sefin sugerida")

    # se o layout tiver colunas repetidas de "valor", a gente guarda um snapshot de todas as colunas também
    items: List[Dict[str, Any]] = []
    for tr in all_tr:
        if tr == header_tr:
            continue
        tds = tr.find_all("td")
        if not tds:
            continue

        cols = [td.get_text(" ", strip=True) for td in tds]
        # ignora linhas muito curtas
        if len(cols) < 6:
            continue

        def get(i: Optional[int]) -> Optional[str]:
            if i is None:
                return None
            if i < 0 or i >= len(cols):
                return None
            v = (cols[i] or "").strip()
            return v or None

        item = {
            "item": get(idx_item),
            "descricao": get(idx_desc),
            "cfop": get(idx_cfop),
            "cest": get(idx_cest),
            "ncm": get(idx_ncm),
            "produto_sefin": get(idx_prod_sefin),
            "cols_raw": cols,  # para diagnóstico / auditoria
        }

        # tenta extrair alguns valores numéricos “comuns”
        # (não depende de posição fixa; serve para você tratar depois)
        nums = []
        for c in cols:
            f = _parse_decimal_br(c)
            if f is not None:
                nums.append(f)
        item["nums_detectados"] = nums[:30]

        # se não parece item (sem descrição e sem ncm), pula
        if not (item["descricao"] or item["ncm"] or item["produto_sefin"]):
            continue

        items.append(item)

    return items


# =========================
# API
# =========================
app = FastAPI(title="Extrato por Produto (NCM) — SEFIN RO")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "now": _now_iso(), "log_file": LOG_FILE}


@app.get("/extrato-produto")
def extrato_produto(
    user: str = Query(..., description="email do user (igual no Supabase)"),
    codi: str = Query("", description="CODI opcional para selecionar a empresa"),
    url_extrato: str = Query(..., description="URL do extrato.jsp (a do botão/linha do débito)"),
    chave: str = Query("", description="chave NFe (opcional) — se vazio usa a primeira detectada"),
):
    """
    Retorna os itens do internamento com NCM/CFOP/CEST (por produto).
    """
    job = datetime.now().strftime("%Y%m%d%H%M%S")
    logger.info("START | job=%s | user=%s | codi=%s", job, user, codi or "-")

    cert = carregar_certificado_por_user_e_codi(user, codi or None)

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        # login DET/Portal
        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(status_code=400, detail="Falha ao entrar no Acesso Digital (DET).")
        if not ir_para_portal_e_carregar_home(sess):
            raise HTTPException(status_code=400, detail="Falha ao abrir Portal (home).")

        # abre extrato.jsp
        # (se vier url com aspas ou # no final, limpamos)
        url_extrato_clean = (url_extrato or "").strip().replace("%22", "").split("#", 1)[0]
        r = sess.get(url_extrato_clean, timeout=30, allow_redirects=True)
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Erro ao abrir extrato.jsp: HTTP {r.status_code}")

        html_extrato = r.text

        token, usuario = _extrair_token_e_usuario_do_html_extrato(html_extrato)
        if not usuario:
            raise HTTPException(status_code=400, detail="Não consegui extrair USUARIO/CPF do extrato.jsp.")
        # token às vezes pode ser vazio; tentamos mesmo assim
        if not token:
            logger.warning("WARN | job=%s | token não encontrado no extrato.jsp", job)

        # chaves do extrato (tabela principal)
        chaves = _extrair_chaves_do_extrato(html_extrato)
        if not chaves:
            raise HTTPException(status_code=400, detail="Nenhuma chave NFe (44 dígitos) encontrada no extrato.jsp.")

        chave_alvo = (chave or "").strip()
        if chave_alvo:
            if chave_alvo not in chaves:
                logger.warning("WARN | job=%s | chave informada não está na lista detectada; seguindo mesmo assim", job)
        else:
            chave_alvo = chaves[0]

        # monta URL do internamento igual ao JS abrirCapaInternamento :contentReference[oaicite:6]{index=6}
        url_capa = _montar_url_capa_internamento(usuario=usuario, chave=chave_alvo, token=token)
        logger.info("CAPA | job=%s | url=%s", job, url_capa)

        # abre internamento (segue redirects até processamentos/show) :contentReference[oaicite:7]{index=7}
        r2 = sess.get(url_capa, timeout=45, allow_redirects=True)
        if r2.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Falha ao abrir internamento: HTTP {r2.status_code}")

        html_intern = r2.text
        final_url = r2.url

        # acha tabela “Itens da Nota” (onde tem NCM/CFOP/CEST)
        table, diag = _pick_best_items_table(html_intern)
        if not table or (diag.get("best_score", 0) < 40):
            # devolve diagnóstico para ajustar rápido
            return {
                "ok": True,
                "user": user,
                "codi": str(cert.get("codi") or ""),
                "empresa": cert.get("empresa") or "",
                "result": {
                    "ok": False,
                    "message": "Não consegui localizar a tabela de Itens da Nota (NCM/CFOP/CEST) no internamento.",
                    "final_url": final_url,
                    "diag": diag,
                },
            }

        itens = _parse_items_from_table(table)

        # totais básicos
        itens_sem_ncm = sum(1 for x in itens if not x.get("ncm"))
        totals = {
            "qtd_itens": len(itens),
            "itens_sem_ncm": itens_sem_ncm,
        }

        out = {
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
                "itens": itens,
                "totais": totals,
                "diagnostico": diag,
            },
        }

        logger.info("DONE | job=%s | itens=%s | sem_ncm=%s", job, len(itens), itens_sem_ncm)
        return out

    except HTTPException:
        raise
    except Exception as e:
        _log_exc(f"FAIL | job={job}", e)
        if DEBUG_ERRORS:
            raise HTTPException(status_code=500, detail={"error": str(e), "traceback": traceback.format_exc(), "log_file": LOG_FILE})
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for p in (cert_path, key_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# Render/local
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("extrato_api:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
