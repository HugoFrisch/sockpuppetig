#!/usr/bin/env python3
"""
sockpuppetRun.py - Auditoria da timeline do Instagram (sockpuppets).

Cada ciclo abre o Chrome UMA vez e coleta:
  1) feed geral        -> feed_geral/
  2) aba "Seguindo"    -> feed_seguindo/
Em cada pasta: timeline_*.json (formato do professor, 1 por coleta) e a subpasta raw/
(dados brutos + metadados).

Uso:
  python sockpuppetRun.py                      (interativo)
  python sockpuppetRun.py --nome ana
  python sockpuppetRun.py --nome ana --intervalo 3600 --jitter 300
  python sockpuppetRun.py --nome ana --uma-vez (uma coleta e sai, para testes)

Curtidas (opcional): se o nome estiver em sockpuppets.txt (neutro, esquerda, direita, balanceado,
total esquerda, total direita), o robo curte no feed geral os posts das contas do orientacao.txt
conforme a orientacao. Registro em sock_<nome>/interacoes/curtidas.jsonl.
Desligar: --curtidas-por-ciclo 0.

Para parar: Ctrl+C, ou crie um arquivo chamado STOP dentro da pasta sock_<nome>.
"""
import argparse
import json
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
TARGET_POSTS = 30
MAX_SCROLLS = 60
DEFAULT_INTERVAL = 3600      # segundos (1 hora)
DEFAULT_JITTER = 300         # segundos de atraso aleatório máximo por coleta
MAX_FALHAS_SEGUIDAS = 5
HOME_URL = "https://www.instagram.com/"
FOLLOWING_URL = "https://www.instagram.com/?variant=following"
BRT = timezone(timedelta(hours=-3))   # Brasil sem horário de verão desde 2019
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

# Dicionário de tipos (valores observados; novos valores viram "unknown_<n>")
MEDIA_TYPE = {1: "image", 2: "video", 8: "carousel"}

JS_ARTICLES = """() => {
  const out = [];
  document.querySelectorAll('article').forEach(art => {
    let code = null;
    for (const a of art.querySelectorAll('a[href]')) {
      const m = a.getAttribute('href').match(/\\/(?:p|reel)\\/([A-Za-z0-9_-]+)/);
      if (m) { code = m[1]; break; }
    }
    out.push(code);
  });
  return out;
}"""

JS_SCRIPTS = """() => [...document.querySelectorAll('script[type="application/json"]')]
                       .map(s => s.textContent)"""


JS_GRID = r"""() => [...document.querySelectorAll('main a[href*="/p/"], main a[href*="/reel/"]')]
  .map(a => { const m = (a.getAttribute('href') || '').match(/\/(?:p|reel)\/([A-Za-z0-9_-]+)/); return m ? m[1] : null; })
  .filter(Boolean)"""


# ----------------------------------------------------------------- utilidades
class Paths:
    def __init__(self, nome):
        self.dir = BASE_DIR / f"sock_{nome}"
        self.profile = self.dir / f"navegador_dados_{nome}"
        self.session = self.dir / f"instagram_session_{nome}.json"
        # uma pasta por tipo de coleta (cada uma com sua subpasta raw/)
        self.pastas = {"feed": self.dir / "feed_geral",
                       "following": self.dir / "feed_seguindo"}
        self.log = self.dir / "coleta.log"
        self.stop = self.dir / "STOP"
        self.curtidas = self.dir / "interacoes" / "curtidas.jsonl"

    def pasta(self, prefixo):
        """Pasta de saida de um tipo de coleta (e a sua subpasta raw)."""
        d = self.pastas[prefixo]
        (d / "raw").mkdir(parents=True, exist_ok=True)
        return d


def now_str():
    return datetime.now(BRT).strftime("%Y-%m-%d %H:%M:%S")


def now_iso():
    return datetime.now(BRT).replace(tzinfo=None).isoformat()


def log(paths, msg):
    linha = f"[{now_str()}] {msg}"
    print(linha, flush=True)
    try:
        with open(paths.log, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass


def find_chrome():
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    sysname = platform.system()
    if sysname == "Windows":
        cands = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sysname == "Darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        cands = [shutil.which("google-chrome"), shutil.which("google-chrome-stable"),
                 "/usr/bin/google-chrome"]
    for c in cands:
        if c and Path(c).exists():
            return c
    raise RuntimeError("Google Chrome não encontrado. Defina a variável CHROME_PATH.")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_cdp(port, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(
        "Não consegui conectar ao Chrome. Feche qualquer janela do Chrome que use "
        "o perfil deste sockpuppet e tente de novo.")


def kill_chrome(proc):
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        else:
            proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def launch_chrome(profile, port, url="https://www.instagram.com/"):
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([
        find_chrome(),
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--window-size=1280,900", "--lang=pt-BR",
        "--no-first-run", "--no-default-browser-check",
        url,
    ])
    try:
        wait_cdp(port)
    except Exception:
        kill_chrome(proc)
        raise
    return proc


def graceful_close(browser, proc):
    """Fecha o Chrome de forma limpa (grava cookies) e, se falhar, força."""
    try:
        browser.new_browser_cdp_session().send("Browser.close")
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
    except Exception:
        kill_chrome(proc)


# ------------------------------------------------------------------ extração
def extract_posts(payload):
    """Só posts reais do feed. Ignora slides internos de carrossel."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            if (o.get("code") and o.get("pk")
                    and isinstance(o.get("user"), dict)
                    and o.get("product_type") != "carousel_item"
                    and not o.get("carousel_parent_id")):
                found.append(o)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(payload)
    return found


def media_type_name(post):
    mt = post.get("media_type")
    return MEDIA_TYPE.get(mt, f"unknown_{mt}")


def rel_path(post):
    kind = "reel" if post.get("product_type") == "clips" else "p"
    return f"/{kind}/{post.get('code')}/"


def is_repost(post):
    return any(f.get("floating_context_item_type") == "REPOSTED_BY"
               for f in (post.get("floating_context_items") or []))


def is_ad(post):
    # Heurística NÃO validada: depende de o Instagram expor esses campos.
    return any(bool(post.get(k)) for k in ("ad_id", "is_ad", "ad_action"))


def play_count(post):
    for k in ("play_count", "view_count", "ig_play_count"):
        if post.get(k) is not None:
            return post.get(k)
    return None


def hashtags(caption):
    vistos, out = set(), []
    for h in re.findall(r"#\w+", caption or ""):
        if h not in vistos:
            vistos.add(h)
            out.append(h)
    return out


def create_time(post):
    ta = post.get("taken_at")
    if not ta:
        return None
    return datetime.fromtimestamp(ta, BRT).replace(tzinfo=None).isoformat(timespec="seconds")


def to_timeline_item(post, vis_idx, reason=None):
    """Modelo do professor."""
    u = post.get("user") or {}
    fs = u.get("friendship_status") or {}
    cap = (post.get("caption") or {}).get("text")
    return {
        "id": post.get("pk"),
        "mediaType": media_type_name(post),
        "relative_path": rel_path(post),
        "desc": cap,
        "createTime": create_time(post),
        "captured_at": post.get("_recebido_em"),
        "api_arrival_index": post.get("_recv_pos"),
        "visual_feed_index": vis_idx,
        "author": {
            "uniqueId": u.get("username"),
            "id": u.get("pk"),
            "verified": u.get("is_verified"),
            "isFollowed": fs.get("following"),
        },
        "isAd": is_ad(post),
        "isPaidContent": post.get("is_paid_partnership"),
        "isAIGCGenerated": post.get("ai_label_info") is not None,
        "itemRecommendReason": post.get("inventory_source") or reason,
        "contents": [{"type": "hashtag", "value": h} for h in hashtags(cap)],
        "stats": {
            "playCount": play_count(post),
            "diggCount": post.get("like_count"),
            "commentCount": post.get("comment_count"),
            "shareCount": post.get("media_repost_count"),
            "collectCount": None,
        },
    }


# -------------------------------------------------------------------- coleta
def nao_seguido(post):
    fs = (post.get("user") or {}).get("friendship_status") or {}
    return fs.get("following") is False      # anuncio / sugerido


def parse_payloads(text):
    """Le uma resposta que pode ter um JSON so ou varios (um por linha)."""
    text = (text or "").strip()
    if text.startswith("for (;;);"):
        text = text[9:]
    try:
        yield json.loads(text)
        return
    except Exception:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("for (;;);"):
            line = line[9:]
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _adapt_old(o):
    edges = (o.get("edge_media_to_caption") or {}).get("edges") or []
    text = edges[0]["node"]["text"] if edges and "node" in edges[0] else None
    return {"pk": o.get("id"), "code": o.get("shortcode"),
            "taken_at": o.get("taken_at_timestamp"),
            "like_count": (o.get("edge_media_preview_like") or {}).get("count"),
            "comment_count": (o.get("edge_media_to_comment") or {}).get("count"),
            "media_type": 2 if o.get("is_video") else 1,
            "product_type": o.get("product_type"),
            "caption": {"text": text} if text else None,
            "user": o.get("owner") or {}}


def extract_profile_posts(payload):
    """Posts de uma pagina de perfil (o campo 'user' pode faltar)."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            if (o.get("code") and o.get("pk")
                    and ("taken_at" in o or "media_type" in o)
                    and o.get("product_type") != "carousel_item"
                    and not o.get("carousel_parent_id")):
                found.append(o)
                return
            if o.get("shortcode") and o.get("id") and o.get("taken_at_timestamp"):
                found.append(_adapt_old(o))
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(payload)
    return found


class Captura:
    """Guarda os posts recebidos (rede/HTML) e a ordem em que aparecem na tela."""

    def __init__(self, so_seguidos=False, dono=None):
        self.by_code = {}
        self.dom_order = []
        self.ign = set()            # codigos descartados de proposito (anuncios/sugeridos)
        self.contador = 0
        self.sem_link = 0
        self.so_seguidos = so_seguidos
        self.dono = dono            # perfil visitado (modo perfil)
        self.rate_limited = False
        self.urls = []

    def ingest(self, data, origem):
        posts = extract_profile_posts(data) if self.dono else extract_posts(data)
        for post in posts:
            code = post.get("code")
            if not code or code in self.by_code or code in self.ign:
                continue
            if self.dono:
                u = post.get("user")
                if not isinstance(u, dict):
                    u = post["user"] = {}
                if u.get("username") and str(u["username"]).lower() != self.dono:
                    continue
                if not u.get("username"):
                    u["username"] = self.dono
            if self.so_seguidos and nao_seguido(post):
                self.ign.add(code)
                continue
            self.contador += 1
            post["_recv_pos"] = self.contador
            post["_recebido_em"] = now_iso()
            post["_origem"] = origem
            self.by_code[code] = post

    def on_response(self, resp):
        url = resp.url
        if "instagram.com" not in url:
            return
        try:
            if resp.status == 429:
                self.rate_limited = True
                return
        except Exception:
            pass
        if self.dono:
            ok = "graphql" in url or "/api/v1/" in url
        else:
            ok = "graphql" in url or "/api/v1/feed" in url
        if not ok:
            return
        if len(self.urls) < 12:
            self.urls.append(url[:150])
        try:
            text = resp.text()
        except Exception:
            return
        for data in parse_payloads(text):
            self.ingest(data, "rede")

    def ingest_html(self, page):
        try:
            scripts = page.evaluate(JS_SCRIPTS)
        except Exception:
            return
        for txt in scripts:
            if not txt or '"code"' not in txt:
                continue
            try:
                self.ingest(json.loads(txt), "html")
            except Exception:
                continue

    def read_dom(self, page, grid=False):
        try:
            arts = page.evaluate(JS_GRID if grid else JS_ARTICLES)
        except Exception:
            return
        if not grid:
            self.sem_link = max(self.sem_link, sum(1 for c in arts if c is None))
        for c in arts:
            if c and c not in self.dom_order:
                self.dom_order.append(c)

    def n_ready(self):
        return sum(1 for c in self.dom_order if c in self.by_code)


def stop_requested(paths):
    return paths.stop.exists()


def salvar_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------ feeds (geral / seguindo)
def rolar_feed(page, cap):
    scrolls = 0
    while cap.n_ready() < TARGET_POSTS and scrolls < MAX_SCROLLS:
        cap.read_dom(page)
        page.mouse.wheel(0, random.randint(400, 800))
        time.sleep(random.uniform(1.5, 5.0))
        if random.random() < 0.15:
            time.sleep(random.uniform(5, 12))
        scrolls += 1
    cap.read_dom(page)


def salvar_feed(nome, paths, cap, status, chrome_version, prefixo, rotulo):
    agora = datetime.now(BRT)
    ts = agora.strftime("%Y%m%dT%H%M%S")
    coletado_em = agora.strftime("%Y-%m-%d %H:%M:%S")
    agora_epoch = time.time()
    by_code, dom_order = cap.by_code, cap.dom_order

    # ordem da TELA; so entram posts que tem dados
    final = [(dom_order.index(c) + 1, by_code[c])
             for c in dom_order if c in by_code][:TARGET_POSTS]
    dom_sem_json = [c for c in dom_order if c not in by_code and c not in cap.ign]
    so_rede = [c for c in by_code if c not in dom_order]

    if status == "ok" and len(final) < TARGET_POSTS:
        status = f"incompleto_{len(final)}_posts"

    origens = {}
    for _, p_ in final:
        origens[p_.get("_origem")] = origens.get(p_.get("_origem"), 0) + 1

    pasta = paths.pasta(prefixo)

    completo = {
        "sockpuppet": nome,
        "feed": rotulo,
        "coletado_em_brasilia": coletado_em,
        "chrome_version": chrome_version,
        "status": status,
        "n_posts": len(final),
        "n_ignorados_nao_seguidos": len(cap.ign),
        "diagnostico_ordem": {
            "ordem": "tela",
            "n_recebidos_rede_ou_html": len(by_code),
            "n_desenhados_na_tela": len(dom_order),
            "na_tela_sem_dados": dom_sem_json,
            "recebidos_nao_exibidos": so_rede,
            "blocos_sem_link_de_post": cap.sem_link,
            "origem_dos_posts": origens,
        },
    }
    timeline = [to_timeline_item(p_, i) for i, p_ in final]
    nome_tl = "timeline" if prefixo == "feed" else f"timeline_{prefixo}"

    (pasta / f"{nome_tl}_{ts}.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    (pasta / "raw" / f"{prefixo}_{ts}_raw.json").write_text(
        json.dumps({"meta": {k: v for k, v in completo.items() if k != "posts"},
                    "dom_order": dom_order, "posts": list(by_code.values())},
                   ensure_ascii=False), encoding="utf-8")

    log(paths, f"[{rotulo}] {status} | {len(final)} posts | recebidos={len(by_code)} "
               f"tela={len(dom_order)} sem_dados={len(dom_sem_json)} "
               f"ignorados={len(cap.ign)} sem_link={cap.sem_link} origem={origens}")
    return status


def coletar_feed(page, nome, paths, url, prefixo, rotulo, so_seguidos, chrome_version,
                 cfg_curtidas=None):
    cap = Captura(so_seguidos=so_seguidos)
    status = "ok"
    handler = cap.on_response
    page.on("response", handler)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(random.uniform(4, 8))
        if "accounts/login" in page.url or "challenge" in page.url:
            status = "sessao_caida_ou_checkpoint"
        else:
            cap.ingest_html(page)
            rolar_feed(page, cap)
            time.sleep(3)
            cap.ingest_html(page)
            cap.read_dom(page)
    finally:
        page.remove_listener("response", handler)
    status_final = salvar_feed(nome, paths, cap, status, chrome_version, prefixo, rotulo)
    if (cfg_curtidas and rotulo in cfg_curtidas["onde"]
            and not str(status_final).startswith("sessao_caida")):
        try:
            interagir(page, nome, paths, cap, rotulo, cfg_curtidas)
        except Exception as e:
            log(paths, f"[curtidas] ERRO em {rotulo}: {e}")
    return status_final


# ------------------------------------------------------------ interacoes (curtidas)
ORIENTACOES = ("neutro", "esquerda", "direita", "balanceado", "total_esquerda", "total_direita")
# neutro          : nao curte nada
# esquerda        : curte so posts de contas do PT (todas as contas do PT no orientacao.txt)
# direita         : curte so posts de contas do PL (todas as contas do PL no orientacao.txt)
# balanceado      : curte os dois lados (todas as contas do arquivo)
# total_esquerda  : curte somente a esquerda (todas as contas de esquerda do arquivo)
# total_direita   : curte somente a direita (todas as contas de direita do arquivo)
PARTIDO_CURTIR = {"esquerda": "PT", "direita": "PL"}
# balanceado: curte TODOS os posts rotulados de esquerda E de direita.
# True = curte o mesmo numero de cada lado em cada feed/ciclo (o lado menor limita o outro)
BALANCEADO_IGUALAR = False
LADOS = ("esquerda", "direita")
SOCKPUPPETS_FILE = BASE_DIR / "sockpuppets.txt"
ORIENTACAO_FILE = BASE_DIR / "orientacao.txt"
DEFAULT_CURTIDAS_POR_CICLO = 100000  # maximo por FEED em cada ciclo: na pratica, sem limite (0 desliga)
DEFAULT_CURTIDAS_POR_DIA = 100000    # maximo por dia, somando os feeds: na pratica, sem limite
PAUSA_ENTRE_CURTIDAS = (20, 70)    # segundos entre uma curtida e a proxima
BUSCA_MAX_SEG = 180                # tempo maximo procurando UM post na tela
TEMPO_MAX_CURTIDAS_MIN = 0         # teto de minutos da fase de curtidas em cada feed (0 = sem teto)

SOCKPUPPETS_TEMPLATE = (
    "# Orientacao de cada sockpuppet:  nome  orientacao\n"
    "# neutro         = nao curte nada (so coleta)\n"
    "# esquerda       = curte so contas do PT\n"
    "# direita        = curte so contas do PL\n"
    "# balanceado     = curte os dois lados\n"
    "# total esquerda = curte apenas a esquerda (todos os partidos de esquerda)\n"
    "# total direita  = curte apenas a direita (todos os partidos de direita)\n"
    "joao1 neutro\n"
    "maria esquerda\n"
    "jonas direita\n"
    "carla balanceado\n"
    "# ana total esquerda\n"
    "# pedro total direita\n")

ORIENTACAO_TEMPLATE = (
    "# Como o robo decide se um post e de esquerda ou de direita.\n"
    "# Os rotulos sao decisao da pesquisa: preencha com as suas contas e termos.\n"
    "#\n"
    "#   @perfil,PARTIDO,esquerda|direita            uma conta por linha\n"
    "#   @perfil,esquerda|direita                    formato antigo, sem partido\n"
    "#   conta <perfil ou link> <esquerda|direita>   todo post desse perfil recebe o rotulo\n"
    "#   termo <palavra ou #hashtag> <esquerda|direita>   posts cuja legenda contem o termo\n"
    "#\n"
    "# A regra por conta vale primeiro. Se a legenda casar com termos de lados opostos,\n"
    "# o post fica 'indefinido' e nenhum sockpuppet esquerda/direita curte.\n"
    "#\n"
    "# conta perfil_exemplo_a esquerda\n"
    "# conta perfil_exemplo_b direita\n"
    "# termo #hashtag_exemplo_a esquerda\n"
    "# termo #hashtag_exemplo_b direita\n")


def garantir_modelos():
    if not SOCKPUPPETS_FILE.exists():
        SOCKPUPPETS_FILE.write_text(SOCKPUPPETS_TEMPLATE, encoding="utf-8")
    if not ORIENTACAO_FILE.exists():
        ORIENTACAO_FILE.write_text(ORIENTACAO_TEMPLATE, encoding="utf-8")


def orientacao_do_sockpuppet(nome):
    """Le sockpuppets.txt. Retorna (orientacao, erro). Aceita 'total esquerda' ou 'total_esquerda'."""
    if not SOCKPUPPETS_FILE.exists():
        return None, None
    for linha in SOCKPUPPETS_FILE.read_text(encoding="utf-8-sig").splitlines():
        partes = linha.split("#")[0].split()
        if len(partes) >= 2 and partes[0].lower() == nome.lower():
            ori = "_".join(x.lower() for x in partes[1:]).replace("-", "_")
            if ori in ORIENTACOES:
                return ori, None
            return None, (f"orientacao invalida '{' '.join(partes[1:])}' para '{nome}' "
                          f"(use: neutro, esquerda, direita, balanceado, total esquerda, total direita)")
    return None, None


def carregar_regras():
    """Le orientacao.txt. Retorna ({perfil: {lado, partido, idx}}, [(termo, lado)], [linhas_ignoradas]).
    idx = posicao da conta dentro do seu partido, na ordem do arquivo (informativo).
    Aceita:  @perfil,PARTIDO,lado | @perfil,lado | conta <perfil|link> <lado>
             termo <palavra ou #hashtag> <lado> | tipo,valor,lado | <perfil|link> <lado>"""
    contas, termos, ignoradas = {}, [], []
    por_partido = {}
    if not ORIENTACAO_FILE.exists():
        return contas, termos, ignoradas
    for linha in ORIENTACAO_FILE.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):      # comentario so no inicio da linha (hashtags usam #)
            continue
        partido = None
        if "," in linha:
            p = [x.strip() for x in linha.split(",") if x.strip()]
            if len(p) == 2 and p[1].lower() in LADOS:
                tipo, valor, lado = "conta", p[0], p[1].lower()
            elif len(p) == 3 and p[2].lower() in LADOS and p[0].lower() in ("conta", "termo"):
                tipo, valor, lado = p[0].lower(), p[1], p[2].lower()
            elif len(p) == 3 and p[2].lower() in LADOS:      # @perfil,PARTIDO,lado
                tipo, valor, lado, partido = "conta", p[0], p[2].lower(), p[1].upper()
            else:
                ignoradas.append(linha)
                continue
        else:
            p = linha.split()
            if len(p) == 2 and p[1].lower() in LADOS:   # "<perfil ou link> <lado>" = conta
                tipo, valor, lado = "conta", p[0], p[1].lower()
            elif len(p) >= 3 and p[-1].lower() in LADOS:
                tipo, valor, lado = p[0].lower(), " ".join(p[1:-1]), p[-1].lower()
            else:
                ignoradas.append(linha)
                continue
        if tipo == "conta":
            m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", valor)
            nome_conta = (m.group(1) if m else valor).lstrip("@").strip("/").lower()
            if nome_conta in contas:
                if contas[nome_conta]["lado"] != lado:
                    ignoradas.append(f"{linha}  <- conflito: '{nome_conta}' ja esta como "
                                     f"{contas[nome_conta]['lado']}; mantido o primeiro")
                continue
            idx = None
            if partido:
                por_partido[partido] = por_partido.get(partido, 0) + 1
                idx = por_partido[partido]
            contas[nome_conta] = {"lado": lado, "partido": partido, "idx": idx}
        elif tipo == "termo":
            termos.append((valor.lower(), lado))
        else:
            ignoradas.append(linha)
    return contas, termos, ignoradas


def classificar_post(post, contas, termos):
    """Retorna (rotulo, regra): esquerda | direita | indefinido | sem_rotulo."""
    user = str((post.get("user") or {}).get("username") or "").lower()
    if user in contas:
        return contas[user]["lado"], "conta"
    texto = str((post.get("caption") or {}).get("text") or "").lower()
    lados = {lado for termo, lado in termos if termo in texto}
    if len(lados) == 1:
        return lados.pop(), "termo"
    if len(lados) > 1:
        return "indefinido", "termos_conflitantes"
    return "sem_rotulo", None


def elegivel(orientacao, rotulo, info=None):
    if orientacao == "neutro":
        return False                    # neutro nao curte nada
    if orientacao == "balanceado":
        return rotulo in LADOS          # balanceado curte esquerda e direita
    if orientacao.startswith("total_"):
        return rotulo == orientacao[6:]  # total esquerda / total direita: todo o lado
    # esquerda / direita: so o partido definido (PT ou PL), em qualquer posicao do arquivo
    if rotulo != orientacao or not info:
        return False
    return info.get("partido") == PARTIDO_CURTIR[orientacao]


def candidato_anuncio(post):
    """Mesma regra do campo candidato_anuncio: conteudo nao seguido vindo do estoque 'media_or_ad'."""
    fs = (post.get("user") or {}).get("friendship_status") or {}
    return (post.get("inventory_source") == "media_or_ad"
            and fs.get("following") is False and not is_repost(post))


def avaliar_post(post, cfg, ja_tentados):
    """Retorna (rotulo, regra, motivo_para_pular). motivo None = pode curtir."""
    rot, regra = classificar_post(post, cfg["contas"], cfg["termos"])
    if post.get("has_liked"):
        return rot, regra, "ja_curtido_na_conta"
    if post.get("code") in ja_tentados:
        return rot, regra, "ja_curtido_pelo_robo"
    if candidato_anuncio(post):
        return rot, regra, "candidato_a_anuncio"
    user = str((post.get("user") or {}).get("username") or "").lower()
    if not elegivel(cfg["orientacao"], rot, cfg["contas"].get(user)):
        if rot == cfg["orientacao"]:
            return rot, regra, f"fora_do_{PARTIDO_CURTIR[rot]}"
        return rot, regra, "rotulo_nao_serve"
    return rot, regra, None


def posts_finais(cap):
    return [(cap.dom_order.index(c) + 1, cap.by_code[c])
            for c in cap.dom_order if c in cap.by_code][:TARGET_POSTS]


def ler_curtidas(paths):
    regs = []
    if paths.curtidas.exists():
        for linha in paths.curtidas.read_text(encoding="utf-8").splitlines():
            try:
                regs.append(json.loads(linha))
            except Exception:
                pass
    return regs


def registrar_curtida(paths, reg):
    paths.curtidas.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.curtidas, "a", encoding="utf-8") as f:
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")


def ir_ao_topo(page):
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    for _ in range(6):                      # a roda do mouse cobre o caso de rolagem interna
        page.mouse.wheel(0, -3000)
        time.sleep(0.3)
    time.sleep(random.uniform(1.5, 2.5))


def achar_artigo(page, code, prazo=None, max_passos=60):
    """Acha o post na tela. O Instagram descarta da pagina os posts que ficaram longe da
    tela; entao sobe aos poucos ate achar e, se nao achar, volta ao topo e desce."""
    sel = f'a[href*="/p/{code}/"], a[href*="/reel/{code}/"]'

    def tenta():
        art = page.locator("article").filter(has=page.locator(sel)).first
        return art if art.count() > 0 else None

    art = tenta()
    if art:
        return art, 0
    passos = 0
    for direcao in (-1, +1):                # 1) subir a partir da posicao atual; 2) topo e descer
        if direcao == +1:
            ir_ao_topo(page)
            art = tenta()
            if art:
                return art, passos
        for _ in range(max_passos):
            if prazo and time.time() > prazo:
                return None, passos
            page.mouse.wheel(0, direcao * random.randint(500, 800))
            time.sleep(random.uniform(1.0, 2.0))
            passos += 1
            art = tenta()
            if art:
                return art, passos
    return None, passos


def curtir_post(page, code, prazo=None):
    """Clica em Curtir no post `code` que esta no feed. Retorna o status."""
    art, passos = achar_artigo(page, code, prazo)
    if art is None:
        return f"nao_encontrado_na_tela(buscou_{passos}_passos)"
    art.scroll_into_view_if_needed(timeout=5000)
    time.sleep(random.uniform(1.5, 3.5))
    like = art.locator('svg[aria-label="Curtir"], svg[aria-label="Like"]').first
    if like.count() == 0:
        if art.locator('svg[aria-label="Descurtir"], svg[aria-label="Unlike"]').count() > 0:
            return "ja_curtido"
        return "botao_nao_encontrado"
    like.click(timeout=5000)
    time.sleep(random.uniform(1.0, 2.5))
    if art.locator('svg[aria-label="Descurtir"], svg[aria-label="Unlike"]').count() > 0:
        return "curtido"
    return "nao_confirmado"


def montar_cfg_curtidas(nome, opts):
    """Retorna (cfg, motivo_desligado). cfg=None quando nao ha curtidas neste ciclo."""
    if not opts or opts.get("por_ciclo", 0) <= 0:
        return None, None
    ori, erro = orientacao_do_sockpuppet(nome)
    if erro:
        return None, erro
    if ori is None:
        return None, f"'{nome}' nao esta em sockpuppets.txt"
    if ori == "neutro":
        return None, f"'{nome}' e neutro: nao curte nada (so coleta)"
    contas, termos, ignoradas = carregar_regras()
    return {"orientacao": ori, "contas": contas, "termos": termos, "ignoradas": ignoradas,
            "por_ciclo": opts["por_ciclo"], "por_dia": opts["por_dia"],
            "onde": opts["onde"], "feitas": {}}, None


def interagir(page, nome, paths, cap, rotulo, cfg):
    """Depois de coletar o feed, curte posts conforme a orientacao do sockpuppet."""
    ori = cfg["orientacao"]
    regs = ler_curtidas(paths)
    hoje = datetime.now(BRT).strftime("%Y-%m-%d")
    feitas_hoje = sum(1 for r in regs if r.get("status") == "curtido"
                      and str(r.get("quando_brasilia", "")).startswith(hoje))
    feitas_feed = cfg["feitas"].get(rotulo, 0)      # o limite por ciclo vale para cada feed
    restantes = min(cfg["por_ciclo"] - feitas_feed, cfg["por_dia"] - feitas_hoje)
    if restantes <= 0:
        log(paths, f"[curtidas] {rotulo} | limite atingido (neste feed={feitas_feed}/{cfg['por_ciclo']}, "
                   f"hoje={feitas_hoje}/{cfg['por_dia']})")
        return
    ja_tentados = {r.get("code") for r in regs if r.get("status") in ("curtido", "ja_curtido")}
    candidatos, total, rotulos, pulados = [], 0, {}, {}
    for pos, post in posts_finais(cap):
        rot, regra, motivo = avaliar_post(post, cfg, ja_tentados)
        total += 1
        rotulos[rot] = rotulos.get(rot, 0) + 1
        if motivo is None:
            candidatos.append((pos, post, rot, regra))
        else:
            pulados[motivo] = pulados.get(motivo, 0) + 1
            if rot in LADOS:            # post rotulado que nao sera curtido: diz o motivo
                log(paths, f"[curtidas] {rotulo} | pulado #{pos} "
                           f"@{(post.get('user') or {}).get('username')} ({rot}/{regra}): {motivo}")
    random.shuffle(candidatos)
    balanco = ""
    if ori == "balanceado":
        esq = [c for c in candidatos if c[2] == "esquerda"]
        dire = [c for c in candidatos if c[2] == "direita"]
        if BALANCEADO_IGUALAR:
            k = min(len(esq), len(dire), restantes // 2)
            escolhidos = esq[:k] + dire[:k]
            balanco = f" balanceado(esquerda={len(esq)} direita={len(dire)} -> {k} de cada)"
        else:
            escolhidos = candidatos[:restantes]
            balanco = f" balanceado(esquerda={len(esq)} direita={len(dire)} -> todos)"
    else:
        escolhidos = candidatos[:restantes]
    escolhidos.sort(key=lambda x: x[0], reverse=True)   # de baixo para cima: o feed acabou de ser rolado
    inicio_fase = time.time()
    log(paths, f"[curtidas] {rotulo} | {ori} | posts={total} elegiveis={len(candidatos)} "
               f"a_curtir={len(escolhidos)} rotulos={rotulos}{balanco}")
    if pulados:
        log(paths, f"[curtidas] {rotulo} | pulados={pulados}")
    for n_esc, (pos, post, rot, regra) in enumerate(escolhidos):
        if stop_requested(paths):
            break
        if TEMPO_MAX_CURTIDAS_MIN and time.time() - inicio_fase > TEMPO_MAX_CURTIDAS_MIN * 60:
            log(paths, f"[curtidas] {rotulo} | parou: tempo maximo da fase de curtidas "
                       f"({TEMPO_MAX_CURTIDAS_MIN} min); o restante fica para o proximo ciclo")
            break
        try:
            status = curtir_post(page, post["code"], time.time() + BUSCA_MAX_SEG)
        except Exception as e:
            status = f"erro: {str(e)[:80]}"
        reg = {"quando_brasilia": datetime.now(BRT).strftime("%Y-%m-%d %H:%M:%S"),
               "sockpuppet": nome, "orientacao_sockpuppet": ori, "feed": rotulo,
               "position": pos, "code": post.get("code"),
               "url": "https://www.instagram.com" + rel_path(post),
               "username": (post.get("user") or {}).get("username"),
               "orientacao_post": rot, "regra": regra, "status": status}
        registrar_curtida(paths, reg)
        if status == "curtido":
            cfg["feitas"][rotulo] = cfg["feitas"].get(rotulo, 0) + 1
        log(paths, f"[curtidas] {rotulo} | #{pos} @{reg['username']} ({rot}/{regra}) -> {status}")
        if n_esc < len(escolhidos) - 1:          # sem pausa depois da ultima curtida
            time.sleep(random.uniform(*PAUSA_ENTRE_CURTIDAS))


def testar_curtidas(nome, paths):
    """Diagnostico sem abrir o Chrome: aplica as regras aos dados ja coletados."""
    print(f"Pasta do projeto: {BASE_DIR}")
    print(f"sockpuppets.txt : {SOCKPUPPETS_FILE} ({'encontrado' if SOCKPUPPETS_FILE.exists() else 'NAO ENCONTRADO'})")
    ori, erro = orientacao_do_sockpuppet(nome)
    print(f"  '{nome}' -> {ori or ('ERRO: ' + erro if erro else 'NAO ESTA NA LISTA')}")
    print(f"orientacao.txt  : {ORIENTACAO_FILE} ({'encontrado' if ORIENTACAO_FILE.exists() else 'NAO ENCONTRADO'})")
    contas, termos, ignoradas = carregar_regras()
    por_lado = {}
    for i in contas.values():
        por_lado[(i["lado"], i["partido"])] = por_lado.get((i["lado"], i["partido"]), 0) + 1
    print(f"  contas rotuladas: {len(contas)} -> {dict(sorted(por_lado.items(), key=lambda x: str(x)))}")
    print(f"  termos          : {termos or 'nenhum'}")
    for lin in ignoradas:
        print(f"  LINHA IGNORADA: {lin}")
    print(f"Registro de curtidas: {paths.curtidas} ({'existe' if paths.curtidas.exists() else 'ainda nao existe: so e criado na 1a tentativa de curtir'})")
    if not ori:
        print("\nSem orientacao valida, o robo nao curte nada.")
        return
    if ori == "neutro":
        print("\nNeutro: o robo coleta mas nao curte nada.")
        return
    if ori in PARTIDO_CURTIR:
        n = sum(1 for i in contas.values() if i["lado"] == ori and i["partido"] == PARTIDO_CURTIR[ori])
        print(f"\nCurtir: so o {PARTIDO_CURTIR[ori]} ({n} contas elegiveis no orientacao.txt)")
    cfg = {"orientacao": ori, "contas": contas, "termos": termos}
    ja = {r.get("code") for r in ler_curtidas(paths) if r.get("status") in ("curtido", "ja_curtido")}
    for prefixo, rotulo in (("feed", "geral"), ("following", "seguindo")):
        arqs = sorted((paths.pastas[prefixo] / "raw").glob(f"{prefixo}_*_raw.json"))
        if not arqs:
            print(f"\n[{rotulo}] nenhum arquivo raw encontrado em {paths.pastas[prefixo] / 'raw'}")
            continue
        raw = json.loads(arqs[-1].read_text(encoding="utf-8"))
        by_code = {p.get("code"): p for p in raw.get("posts", [])}
        ordem = [c for c in raw.get("dom_order", []) if c in by_code][:TARGET_POSTS]
        print(f"\n[{rotulo}] {arqs[-1].name}: {len(ordem)} posts")
        n_ok, lados = 0, {"esquerda": 0, "direita": 0}
        for i, c in enumerate(ordem, 1):
            post = by_code[c]
            rot, regra, motivo = avaliar_post(post, cfg, ja)
            user = (post.get("user") or {}).get("username")
            fs = ((post.get("user") or {}).get("friendship_status") or {}).get("following")
            if motivo is None:
                n_ok += 1
                lados[rot] = lados.get(rot, 0) + 1
            if motivo is None or rot != "sem_rotulo":
                print(f"  #{i:<2} @{user} | rotulo={rot}/{regra} | segue={fs} | origem={post.get('inventory_source')}"
                      f" -> {'CURTIRIA' if motivo is None else 'pulado: ' + motivo}")
        print(f"  elegiveis: {n_ok} de {len(ordem)} (os demais sao 'sem_rotulo' e nao aparecem acima)")
        if ori == "balanceado":
            tot = (min(lados['esquerda'], lados['direita']) * 2 if BALANCEADO_IGUALAR else n_ok)
            print(f"  balanceado: esquerda={lados['esquerda']} direita={lados['direita']} -> curtiria {tot}")


# ------------------------------------------------- trava entre sockpuppet e stalker
def pid_vivo(pid):
    """True se existe um processo com esse PID (sem matar nada)."""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


class TravaExecucao:
    """Trava unica por sockpuppet (arquivo EXECUCAO.lock em sock_<nome>), compartilhada pelo
    sockpuppetRun.py e pelo stalker.py: so um dos dois usa o Chrome por vez. Quem chega depois
    espera. Se o dono morreu (PID inexistente), a trava e considerada abandonada e e retomada."""

    def __init__(self, caminho, dono, log_fn, stop_fn=lambda: False):
        self.caminho, self.dono, self.log_fn, self.stop_fn = Path(caminho), dono, log_fn, stop_fn
        self.tenho = False

    def _ler(self):
        try:
            return json.loads(self.caminho.read_text(encoding="utf-8"))
        except Exception:
            return None

    def adquirir(self, espera_max_s=None):
        t0, avisou = time.time(), False
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(self.caminho), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, json.dumps({"dono": self.dono, "pid": os.getpid(),
                                             "desde": datetime.now(BRT).strftime("%Y-%m-%d %H:%M:%S")}
                                            ).encode("utf-8"))
                finally:
                    os.close(fd)
                self.tenho = True
                if avisou:
                    self.log_fn(f"[trava] o outro processo terminou; {self.dono} vai comecar agora")
                return True
            except FileExistsError:
                info = self._ler()
                try:
                    idade = time.time() - self.caminho.stat().st_mtime
                except OSError:
                    continue                                # sumiu agora: tenta de novo
                abandonada = (info is None and idade > 15) or (info is not None and not pid_vivo(info.get("pid")))
                if abandonada:
                    try:
                        self.caminho.unlink()
                    except OSError:
                        pass
                    continue
                if not avisou:
                    quem = (info or {}).get("dono", "outro processo")
                    self.log_fn(f"[trava] {quem} esta usando o Chrome deste perfil desde "
                                f"{(info or {}).get('desde', '?')}; {self.dono} aguarda ele terminar...")
                    avisou = True
                if self.stop_fn():
                    return False
                if espera_max_s is not None and time.time() - t0 > espera_max_s:
                    return False
                time.sleep(10)

    def liberar(self):
        if self.tenho:
            self.tenho = False
            try:
                self.caminho.unlink()
            except OSError:
                pass



# ------------------------------------------------------------------------ ciclo
def collect_cycle(nome, paths, cfg_curtidas=None):
    """Uma abertura do Chrome: feed geral e aba Seguindo."""
    # o stalker (pasta separada) usa o mesmo perfil do Chrome: um de cada vez
    trava = TravaExecucao(paths.dir / "EXECUCAO.lock", "o sockpuppet", lambda m: log(paths, m),
                          lambda: stop_requested(paths))
    if not trava.adquirir():
        return {}
    port = free_port()
    resultados = {}
    proc = None
    try:
        proc = launch_chrome(paths.profile, port, "about:blank")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            versao = browser.version
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            resultados["feed"] = coletar_feed(page, nome, paths, HOME_URL, "feed", "geral",
                                              False, versao, cfg_curtidas)
            if not str(resultados["feed"]).startswith("sessao_caida"):
                if not stop_requested(paths):
                    time.sleep(random.uniform(3, 8))
                    try:
                        resultados["seguindo"] = coletar_feed(
                            page, nome, paths, FOLLOWING_URL, "following", "seguindo", True, versao,
                            cfg_curtidas)
                    except Exception as e:
                        resultados["seguindo"] = f"erro: {e}"
                        log(paths, f"[seguindo] ERRO: {e}")
            graceful_close(browser, proc)
    finally:
        if proc is not None and proc.poll() is None:
            kill_chrome(proc)
        trava.liberar()
    return resultados


# --------------------------------------------------------------- configuração
def setup_sockpuppet(nome, paths, intervalo):
    paths.dir.mkdir(parents=True, exist_ok=True)
    print("\n=== CONFIGURAÇÃO INICIAL ===")
    print("O Chrome vai abrir. Faça login na conta do experimento (senha, 2FA, "
          "verificações) e configure o perfil conforme o protocolo.\n")
    port = free_port()
    proc = launch_chrome(paths.profile, port)
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            versao = browser.version
            while True:
                input("Quando terminar a configuração, pressione ENTER aqui... ")
                cookies = browser.contexts[0].cookies("https://www.instagram.com")
                if any(c["name"] == "sessionid" for c in cookies):
                    break
                resp = input("Login NÃO detectado. ENTER para verificar de novo "
                             "ou digite 'sair' para cancelar: ").strip().lower()
                if resp == "sair":
                    graceful_close(browser, proc)
                    raise SystemExit("Configuração cancelada.")
            graceful_close(browser, proc)
    finally:
        if proc.poll() is None:
            kill_chrome(proc)

    paths.session.write_text(json.dumps({
        "sockpuppet": nome,
        "plataforma": "instagram",
        "configurado_em_brasilia": now_str(),
        "chrome_version": versao,
        "sistema": platform.platform(),
        "python": platform.python_version(),
        "intervalo_segundos": intervalo,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Sessão salva. Iniciando a primeira coleta...\n")


def escolher_nome(args):
    if args.nome:
        nome = args.nome
    else:
        existentes = sorted(p.name[5:] for p in BASE_DIR.glob("sock_*") if p.is_dir())
        if existentes:
            print("Sockpuppets existentes:", ", ".join(existentes))
        nome = input("Nome do sockpuppet (existente para retomar, ou novo): ").strip()
    if not NAME_RE.match(nome):
        raise SystemExit("Nome inválido: comece com letra e use só letras e números.")
    return nome


def esperar(paths, segundos):
    """Espera `segundos`, acordando a cada 5 s para ver se o arquivo STOP apareceu."""
    alvo = time.time() + segundos
    while time.time() < alvo:
        if stop_requested(paths):
            return
        time.sleep(min(5, max(0, alvo - time.time())))


def run_loop(nome, paths, intervalo, jitter, uma_vez, curtidas=None):
    falhas = 0
    base = time.time()
    while not stop_requested(paths):
        try:
            cfg_curtidas, motivo = montar_cfg_curtidas(nome, curtidas)
            if motivo:
                log(paths, f"[curtidas] desligado: {motivo}")
            if cfg_curtidas:
                for lin in cfg_curtidas["ignoradas"][:5]:
                    log(paths, f"[curtidas] AVISO orientacao.txt: linha ignorada: {lin}")
                if cfg_curtidas["orientacao"] != "neutro" and not (cfg_curtidas["contas"] or cfg_curtidas["termos"]):
                    log(paths, "[curtidas] AVISO: nenhuma regra valida em orientacao.txt; "
                               f"'{nome}' ({cfg_curtidas['orientacao']}) nao tem o que curtir")
            res = collect_cycle(nome, paths, cfg_curtidas)
            if any(str(v).startswith("sessao_caida") for v in res.values()):
                log(paths, "SESSÃO CAÍDA OU CHECKPOINT. Coleta interrompida. "
                           "Resolva manualmente no Chrome do sockpuppet e rode de novo.")
                return
            falhas = 0
        except KeyboardInterrupt:
            raise
        except Exception as e:
            falhas += 1
            log(paths, f"ERRO ({falhas}/{MAX_FALHAS_SEGUIDAS}): {e}")
            if falhas >= MAX_FALHAS_SEGUIDAS:
                log(paths, "Muitas falhas seguidas. Encerrando.")
                return
        if uma_vez or stop_requested(paths):
            break
        base += intervalo
        espera = base + random.uniform(0, jitter) - time.time()
        if espera < 0:
            base = time.time()
            espera = 0
        log(paths, f"Próxima coleta em {int(espera // 60)} min")
        esperar(paths, espera)
    if stop_requested(paths):
        log(paths, "Arquivo STOP detectado. Coleta encerrada.")
        try:
            paths.stop.unlink()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nome")
    ap.add_argument("--intervalo", type=int, default=DEFAULT_INTERVAL,
                    help="segundos entre coletas (padrão 3600)")
    ap.add_argument("--jitter", type=int, default=DEFAULT_JITTER,
                    help="atraso aleatório máximo em segundos (padrão 300)")
    ap.add_argument("--curtidas-por-ciclo", type=int, default=DEFAULT_CURTIDAS_POR_CICLO,
                    help="maximo de curtidas por feed em cada ciclo (0 desliga; padrão 100000 = sem limite)")
    ap.add_argument("--curtidas-por-dia", type=int, default=DEFAULT_CURTIDAS_POR_DIA,
                    help="maximo de curtidas por dia, somando os feeds (padrão 100000 = sem limite)")
    ap.add_argument("--testar-curtidas", action="store_true",
                    help="diagnostico das regras de curtida com os dados ja coletados (nao abre o Chrome)")
    ap.add_argument("--uma-vez", action="store_true", help="faz uma coleta e sai")
    args = ap.parse_args()

    nome = escolher_nome(args)
    paths = Paths(nome)
    if args.testar_curtidas:
        testar_curtidas(nome, paths)
        return
    paths.dir.mkdir(parents=True, exist_ok=True)
    garantir_modelos()
    if paths.stop.exists():
        paths.stop.unlink()          # STOP antigo nao impede o inicio
    if not paths.session.exists():
        setup_sockpuppet(nome, paths, args.intervalo)
    else:
        print(f"Sessão de '{nome}' encontrada. Retomando coleta sem novo login.")
    print(f"Para parar: Ctrl+C, ou crie o arquivo: {paths.stop}")
    try:
        onde = ("geral",)               # curte somente no feed geral (nunca na aba "Seguindo")
        curtidas = {"por_ciclo": args.curtidas_por_ciclo, "por_dia": args.curtidas_por_dia,
                    "onde": onde}
        ori, erro = orientacao_do_sockpuppet(nome)
        if args.curtidas_por_ciclo > 0:
            print(f"Orientacao: {'ERRO - ' + erro if erro else ('neutro (nao curte nada)' if ori == 'neutro' else (ori or 'desligada (nome nao esta em sockpuppets.txt)'))}")
        run_loop(nome, paths, args.intervalo, args.jitter, args.uma_vez, curtidas)
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuário.")


if __name__ == "__main__":
    main()
