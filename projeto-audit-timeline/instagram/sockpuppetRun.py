#!/usr/bin/env python3
"""
sockpuppetRun.py - Auditoria da timeline do Instagram (sockpuppets).

Cada ciclo abre o Chrome UMA vez e coleta:
  1) feed geral        -> feed_geral/
  2) aba "Seguindo"    -> feed_seguindo/
  3) perfis (profiles.txt, 1x por hora por padrao; 1a vez = base de 30 posts, depois so os novos)
                       -> perfis/
Em cada pasta: timeline_*.json (formato do professor, 1 por coleta) e a subpasta raw/
(dados brutos + metadados).

Uso:
  python sockpuppetRun.py                      (interativo)
  python sockpuppetRun.py --nome ana
  python sockpuppetRun.py --nome ana --intervalo 3600 --jitter 300 --perfis-horas 1
  python sockpuppetRun.py --nome ana --uma-vez (uma coleta e sai, para testes)
  python sockpuppetRun.py --nome ana --uma-vez --forcar-perfis (testa tambem os perfis)

Curtidas (opcional): se o nome estiver em sockpuppets.txt (neutro/esquerda/direita/balanceado), o robo curte
ate N posts por ciclo conforme a orientacao e as regras de orientacao.txt. Registro em
sock_<nome>/interacoes/curtidas.jsonl. Desligar: --curtidas-por-ciclo 0.

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
DEFAULT_PERFIS_HORAS = 1     # de quantas em quantas horas visitar a lista profiles.txt (0 = desligado)
BASELINE_POSTS = 30          # 1a visita de cada perfil: quantos posts guardar
KNOWN_STREAK = 3             # visitas seguintes: para ao ver 3 posts ja coletados em sequencia
MAX_NEW_PER_PROFILE = 60     # trava de seguranca por perfil
MAX_SCROLLS_PROFILE = 12     # trava de seguranca de rolagens por perfil
STAGNANT_LIMIT = 3           # 3 rolagens sem nada novo = fim da grade
PAUSE_BETWEEN_PROFILES = (10, 25)   # segundos entre um perfil e o proximo
MAX_PERFIS_MIN = 150         # a rodada de perfis para sozinha depois disso (continua na proxima)
MAX_PERFIS_FALHAS = 5        # aborta a rodada apos 5 perfis seguidos com problema
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
                       "following": self.dir / "feed_seguindo",
                       "profiles": self.dir / "perfis"}
        self.log = self.dir / "coleta.log"
        self.profiles_txt = self.dir / "profiles.txt"
        self.state_profiles = self.dir / "state_profiles.json"
        self.stop = self.dir / "STOP"
        self.curtidas = self.dir / "interacoes" / "curtidas.jsonl"
        self.seguidos = self.dir / "interacoes" / "seguidos.jsonl"

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
# neutro          : nao segue nem curte ninguem
# esquerda/direita: SEGUE todas as contas do orientacao.txt (os dois lados) e curte so o seu lado,
#                   e dentro do lado so o partido de PARTIDO_CURTIR, apenas as primeiras
#                   LIMITE_CURTIR_POR_PARTIDO contas desse partido (ordem do arquivo)
# balanceado      : segue e curte os dois lados (todas as contas do arquivo)
# total_esquerda  : segue e curte somente a esquerda (todas as contas de esquerda do arquivo)
# total_direita   : segue e curte somente a direita (todas as contas de direita do arquivo)
PARTIDO_CURTIR = {"esquerda": "PT", "direita": "PL"}
LIMITE_CURTIR_POR_PARTIDO = 50
# balanceado: curte TODOS os posts rotulados de esquerda E de direita.
# True = curte o mesmo numero de cada lado em cada feed/ciclo (o lado menor limita o outro)
BALANCEADO_IGUALAR = False
LADOS = ("esquerda", "direita")
SOCKPUPPETS_FILE = BASE_DIR / "sockpuppets.txt"
ORIENTACAO_FILE = BASE_DIR / "orientacao.txt"
DEFAULT_CURTIDAS_POR_CICLO = 100000  # maximo por FEED em cada ciclo: na pratica, sem limite (0 desliga)
DEFAULT_CURTIDAS_POR_DIA = 100000    # maximo por dia, somando os feeds: na pratica, sem limite
DEFAULT_SEGUIR_POR_CICLO = 20      # contas novas seguidas por ciclo (0 desliga). Seguir em massa e o que
DEFAULT_SEGUIR_POR_DIA = 100       # mais faz o Instagram restringir a conta: suba com cuidado
PAUSA_ENTRE_SEGUIDAS = (30, 90)    # segundos entre um "seguir" e o proximo
SEGUIR_TEMPO_MAX_MIN = 15          # teto de minutos da fase de seguir em cada ciclo
SEGUIR_BLOQUEIO_HORAS = 12         # se o Instagram bloquear a acao, para de seguir por tantas horas
SEGUIR_MAX_TENTATIVAS = 3          # tentativas por conta quando nao da para confirmar o "seguir"
PAUSA_ENTRE_CURTIDAS = (20, 70)    # segundos entre uma curtida e a proxima
BUSCA_MAX_SEG = 180                # tempo maximo procurando UM post na tela
TEMPO_MAX_CURTIDAS_MIN = 0         # teto de minutos da fase de curtidas em cada feed (0 = sem teto)

SOCKPUPPETS_TEMPLATE = (
    "# Orientacao de cada sockpuppet:  nome  orientacao\n"
    "# neutro         = nao segue nem curte ninguem (so coleta)\n"
    "# esquerda       = segue todas as contas do orientacao.txt; curte so o PT (primeiras 50 contas do PT)\n"
    "# direita        = segue todas as contas do orientacao.txt; curte so o PL (primeiras 50 contas do PL)\n"
    "# balanceado     = segue e curte os dois lados\n"
    "# total esquerda = segue e curte apenas a esquerda\n"
    "# total direita  = segue e curte apenas a direita\n"
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
    "#   @perfil,PARTIDO,esquerda|direita            uma conta por linha (a ordem do arquivo vale)\n"
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
    idx = posicao da conta dentro do seu partido, na ordem do arquivo (1 = primeira).
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


def contas_para_seguir(ori, contas):
    """Contas que o sockpuppet deve seguir, na ordem do arquivo."""
    if ori in ("esquerda", "direita", "balanceado"):
        return list(contas)
    if ori in ("total_esquerda", "total_direita"):
        lado = ori[6:]
        return [c for c, i in contas.items() if i["lado"] == lado]
    return []


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
    # esquerda / direita: so o partido definido e so as primeiras N contas dele
    if rotulo != orientacao or not info:
        return False
    return (info.get("partido") == PARTIDO_CURTIR[orientacao]
            and info.get("idx") is not None and info["idx"] <= LIMITE_CURTIR_POR_PARTIDO)


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
            return rot, regra, f"fora_do_{PARTIDO_CURTIR[rot]}_top{LIMITE_CURTIR_POR_PARTIDO}"
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
    """Retorna (cfg, motivo_desligado). cfg=None quando nao ha curtidas nem follows neste ciclo."""
    if not opts:
        return None, None
    curtir = opts.get("por_ciclo", 0) > 0
    seguir = opts.get("seguir_por_ciclo", 0) > 0
    if not curtir and not seguir:
        return None, None
    ori, erro = orientacao_do_sockpuppet(nome)
    if erro:
        return None, erro
    if ori is None:
        return None, f"'{nome}' nao esta em sockpuppets.txt"
    if ori == "neutro":
        return None, f"'{nome}' e neutro: nao segue nem curte ninguem (so coleta)"
    contas, termos, ignoradas = carregar_regras()
    return {"orientacao": ori, "contas": contas, "termos": termos, "ignoradas": ignoradas,
            "por_ciclo": opts.get("por_ciclo", 0), "por_dia": opts.get("por_dia", 0),
            "onde": opts["onde"], "feitas": {}, "curtir": curtir,
            "seguir": seguir, "seguir_todas": bool(opts.get("seguir_todas")), "seguir_por_ciclo": opts.get("seguir_por_ciclo", 0),
            "seguir_por_dia": opts.get("seguir_por_dia", 0)}, None


def interagir(page, nome, paths, cap, rotulo, cfg):
    """Depois de coletar o feed, curte posts conforme a orientacao do sockpuppet."""
    ori = cfg["orientacao"]
    regs = ler_curtidas(paths)
    hoje = datetime.now(BRT).strftime("%Y-%m-%d")
    feitas_hoje = sum(1 for r in regs if r.get("status") == "curtido"
                      and str(r.get("quando_brasilia", "")).startswith(hoje))
    feitas_feed = cfg["feitas"].get(rotulo, 0)      # o limite por ciclo vale para cada feed
    restantes = min(cfg["por_ciclo"] - feitas_feed, cfg["por_dia"] - feitas_hoje) if cfg.get("curtir") else 0
    # autores dos posts identificados para curtir (conta e rotulo servem), mesmo que o post ja esteja curtido
    autores, ja_seguindo = [], set()
    for pos, post in posts_finais(cap):
        u = str((post.get("user") or {}).get("username") or "").lower()
        rot, _ = classificar_post(post, cfg["contas"], cfg["termos"])
        if u and u not in autores and elegivel(ori, rot, cfg["contas"].get(u)):
            autores.append(u)
            if ((post.get("user") or {}).get("friendship_status") or {}).get("following"):
                ja_seguindo.add(u)
    if restantes <= 0 and cfg.get("curtir"):
        log(paths, f"[curtidas] {rotulo} | limite atingido (neste feed={feitas_feed}/{cfg['por_ciclo']}, "
                   f"hoje={feitas_hoje}/{cfg['por_dia']})")
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
        if n_esc < len(escolhidos) - 1:          # sem pausa depois da ultima: segue direto para o "seguir"
            time.sleep(random.uniform(*PAUSA_ENTRE_CURTIDAS))
    if not stop_requested(paths):
        try:
            seguir_autores(page, nome, paths, cfg, autores, ja_seguindo)
        except Exception as e:
            log(paths, f"[seguir] ERRO: {e}")


# ---------------------------------------------------------- seguir contas
RX_SEGUIR = re.compile(r"^(Seguir|Follow|Seguir de volta|Follow back)$", re.I)
RX_SEGUINDO = re.compile(r"^(Seguindo|Following)", re.I)
RX_SOLICITADO = re.compile(r"^(Solicitado|Requested)", re.I)
TEXTOS_BLOQUEIO = ("ação bloqueada", "acao bloqueada", "action blocked", "tente novamente mais tarde",
                   "try again later", "we restrict certain activity", "restringimos determinadas atividades")
TEXTOS_NAO_EXISTE = ("esta página não está disponível", "this page isn't available",
                     "sorry, this page isn't available", "página não disponível")
SEGUIR_FEITOS = ("seguido", "ja_seguia", "solicitado", "ja_solicitado", "nao_encontrado")


def ler_seguidos(paths):
    regs = []
    if paths.seguidos.exists():
        for linha in paths.seguidos.read_text(encoding="utf-8").splitlines():
            try:
                regs.append(json.loads(linha))
            except Exception:
                pass
    return regs


def registrar_seguido(paths, reg):
    paths.seguidos.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.seguidos, "a", encoding="utf-8") as f:
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")


DIAGNOSTICO_BOTOES = []


def seguir_um(page, usuario):
    """Abre o perfil e segue. Retorna: seguido | solicitado | ja_seguia | ja_solicitado |
    nao_encontrado | bloqueado | sessao_caida | sem_confirmacao | sem_botao."""
    page.goto(f"https://www.instagram.com/{usuario}/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(random.uniform(3, 6))
    if "accounts/login" in page.url or "challenge" in page.url:
        return "sessao_caida"
    corpo = ""
    try:
        corpo = page.inner_text("body", timeout=5000).lower()
    except Exception:
        pass
    hdr = page.locator("header")
    if hdr.count() == 0:
        return "nao_encontrado" if any(t in corpo for t in TEXTOS_NAO_EXISTE) else "sem_botao"

    def botao(rx):
        loc = hdr.get_by_role("button", name=rx)
        return loc.first if loc.count() else None

    if botao(RX_SEGUINDO):
        return "ja_seguia"
    if botao(RX_SOLICITADO):
        return "ja_solicitado"
    b = botao(RX_SEGUIR)
    if not b:
        try:
            DIAGNOSTICO_BOTOES[:] = [x.strip()[:25] for x in hdr.locator("button, [role=button]").all_inner_texts()
                                     if x.strip()][:10]
        except Exception:
            DIAGNOSTICO_BOTOES[:] = []
        return "sem_botao"
    b.scroll_into_view_if_needed(timeout=5000)
    time.sleep(random.uniform(0.8, 2.0))
    b.click(timeout=8000)
    time.sleep(random.uniform(2.5, 4.5))
    try:
        corpo = page.inner_text("body", timeout=5000).lower()
    except Exception:
        corpo = ""
    if any(t in corpo for t in TEXTOS_BLOQUEIO):
        return "bloqueado"
    if botao(RX_SEGUINDO):
        return "seguido"
    if botao(RX_SOLICITADO):
        return "solicitado"
    return "sem_confirmacao"


PERFIS_NOVOS = []      # perfis acrescentados ao profiles.txt durante o ciclo atual


def adicionar_perfil(paths, usuario):
    """Acrescenta o perfil ao profiles.txt (se ainda nao estiver la). Retorna True se adicionou."""
    usuario = str(usuario or "").lstrip("@").strip().lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", usuario) or usuario in carregar_perfis(paths):
        return False
    garantir_profiles_txt(paths)
    txt = paths.profiles_txt.read_text(encoding="utf-8-sig")
    with open(paths.profiles_txt, "a", encoding="utf-8") as f:
        f.write(("" if txt.endswith("\n") or not txt else "\n") + usuario + "\n")
    PERFIS_NOVOS.append(usuario)
    return True


def _bloqueio_recente(paths, regs):
    agora = datetime.now(BRT)
    for r in reversed(regs):
        if r.get("status") == "bloqueado":
            try:
                t = datetime.strptime(r["quando_brasilia"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BRT)
            except Exception:
                return False
            if agora - t < timedelta(hours=SEGUIR_BLOQUEIO_HORAS):
                log(paths, f"[seguir] em pausa: o Instagram bloqueou a acao em {r['quando_brasilia']}; "
                           f"so tenta de novo apos {SEGUIR_BLOQUEIO_HORAS} h")
                return True
            return False
    return False


def _seguir_lista(page, nome, paths, cfg, alvo, origem, ja_seguindo=()):
    """Segue as contas de 'alvo' (lista de usernames) respeitando limites, pausas e tentativas.
    ja_seguindo = contas que o feed ja mostra como seguidas (nao precisa abrir o perfil)."""
    ori = cfg["orientacao"]
    regs = ler_seguidos(paths)
    if _bloqueio_recente(paths, regs):
        return
    hoje = datetime.now(BRT).strftime("%Y-%m-%d")
    feitos = {r.get("username") for r in regs if r.get("status") in SEGUIR_FEITOS}
    tentativas = {}
    for r in regs:
        if r.get("status") not in SEGUIR_FEITOS:
            tentativas[r.get("username")] = tentativas.get(r.get("username"), 0) + 1
    novos_hoje = sum(1 for r in regs if r.get("status") in ("seguido", "solicitado")
                     and str(r.get("quando_brasilia", "")).startswith(hoje))
    pendentes = [u for u in alvo if u not in feitos and tentativas.get(u, 0) < SEGUIR_MAX_TENTATIVAS]
    log(paths, f"[seguir] {origem} | {ori} | alvo={len(alvo)} ja_resolvidas={len([u for u in alvo if u in feitos])} "
               f"pendentes={len(pendentes)} | hoje={novos_hoje}/{cfg['seguir_por_dia']} "
               f"(maximo {cfg['seguir_por_ciclo']} novas por ciclo)")
    novas, inicio = 0, time.time()

    def reg(u, status):
        info = cfg["contas"].get(u) or {}
        registrar_seguido(paths, {"quando_brasilia": datetime.now(BRT).strftime("%Y-%m-%d %H:%M:%S"),
                                  "sockpuppet": nome, "orientacao_sockpuppet": ori, "origem": origem,
                                  "username": u, "url": f"https://www.instagram.com/{u}/",
                                  "lado": info.get("lado"), "partido": info.get("partido"), "status": status})
        log(paths, f"[seguir] @{u} ({info.get('partido')}/{info.get('lado')}) -> {status}")

    for u in pendentes:
        if stop_requested(paths):
            break
        if u in ja_seguindo:                       # o proprio feed informa que ja seguimos
            reg(u, "ja_seguia")
            continue
        if novas >= cfg["seguir_por_ciclo"] or novos_hoje + novas >= cfg["seguir_por_dia"]:
            log(paths, f"[seguir] limite atingido (ciclo={novas}/{cfg['seguir_por_ciclo']}, "
                       f"hoje={novos_hoje + novas}/{cfg['seguir_por_dia']}); o resto fica para os proximos ciclos")
            break
        if time.time() - inicio > SEGUIR_TEMPO_MAX_MIN * 60:
            log(paths, f"[seguir] parou: tempo maximo da fase ({SEGUIR_TEMPO_MAX_MIN} min)")
            break
        try:
            status = seguir_um(page, u)
        except Exception as e:
            status = f"erro: {str(e)[:80]}"
        reg(u, status)
        if status in ("seguido", "solicitado"):
            novas += 1
        if status == "sem_botao" and DIAGNOSTICO_BOTOES:
            log(paths, f"[seguir] diagnostico: botoes vistos no cabecalho do perfil: {DIAGNOSTICO_BOTOES}")
        if status == "bloqueado":
            log(paths, "[seguir] BLOQUEIO do Instagram detectado. Parando de seguir por "
                       f"{SEGUIR_BLOQUEIO_HORAS} h. Considere baixar --seguir-por-ciclo.")
            break
        if status == "sessao_caida":
            log(paths, "[seguir] sessao caida; parando")
            break
        time.sleep(random.uniform(*PAUSA_ENTRE_SEGUIDAS) if status in ("seguido", "solicitado")
                   else random.uniform(4, 9))


def seguir_autores(page, nome, paths, cfg, autores, ja_seguindo):
    """Segue os autores dos posts identificados para curtir (e os poe no profiles.txt)."""
    if not autores:
        return
    novos = [u for u in autores if adicionar_perfil(paths, u)]
    if novos:
        log(paths, f"[perfis] adicionados ao profiles.txt: {', '.join('@' + u for u in novos)}")
    if cfg.get("seguir"):
        _seguir_lista(page, nome, paths, cfg, autores, "post_para_curtir", ja_seguindo)


def seguir_contas(page, nome, paths, cfg):
    """(Opcional, --seguir-todas) segue em ordem de arquivo todas as contas que a orientacao manda seguir."""
    _seguir_lista(page, nome, paths, cfg, contas_para_seguir(cfg["orientacao"], cfg["contas"]), "lista_completa")


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
        print("\nNeutro: o robo coleta mas nao segue nem curte nada.")
        return
    alvo = contas_para_seguir(ori, contas)
    seg_ok = {r.get("username") for r in ler_seguidos(paths) if r.get("status") in SEGUIR_FEITOS}
    print(f"\nSeguir: {len(alvo)} contas no total; {len([u for u in alvo if u in seg_ok])} ja resolvidas; "
          f"registro: {paths.seguidos} ({'existe' if paths.seguidos.exists() else 'ainda nao existe'})")
    if ori in PARTIDO_CURTIR:
        n = sum(1 for i in contas.values() if i["lado"] == ori and i["partido"] == PARTIDO_CURTIR[ori]
                and i["idx"] is not None and i["idx"] <= LIMITE_CURTIR_POR_PARTIDO)
        print(f"Curtir: so {PARTIDO_CURTIR[ori]} e so as primeiras {LIMITE_CURTIR_POR_PARTIDO} contas dele "
              f"({n} contas elegiveis para curtida)")
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


# ---------------------------------------------------------- perfis (profiles.txt)
PROFILES_TEMPLATE = (
    "# Perfis a monitorar. Um por linha, sem o @ (pode colar o link tambem).\n"
    "# Linhas que comecam com # sao ignoradas. Apague os exemplos e coloque os seus.\n"
    "#\n# perfil_exemplo_1\n# perfil_exemplo_2\n# https://www.instagram.com/perfil_exemplo_3/\n")


def garantir_profiles_txt(paths):
    if not paths.profiles_txt.exists():
        paths.profiles_txt.write_text(PROFILES_TEMPLATE, encoding="utf-8")


def carregar_perfis(paths):
    if not paths.profiles_txt.exists():
        return []
    nomes, vistos = [], set()
    for linha in paths.profiles_txt.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.split("#")[0].strip()
        if not linha:
            continue
        m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", linha)
        n = (m.group(1) if m else linha).lstrip("@").strip("/ ").lower()
        if re.fullmatch(r"[a-z0-9._]{1,30}", n) and n not in vistos:
            vistos.add(n)
            nomes.append(n)
    return nomes


def carregar_estado(paths):
    if paths.state_profiles.exists():
        try:
            st = json.loads(paths.state_profiles.read_text(encoding="utf-8"))
            st.setdefault("profiles", {})
            st.setdefault("ultima_rodada_epoch", 0)
            return st
        except Exception:
            paths.state_profiles.replace(paths.state_profiles.with_suffix(".bak"))
    return {"profiles": {}, "ultima_rodada_epoch": 0}


def ordenar_perfis(nomes, estado):
    """Quem nunca foi visitado primeiro; depois quem foi visitado ha mais tempo."""
    profs = estado["profiles"]
    nunca = [n for n in nomes if n not in profs]
    random.shuffle(nunca)
    visitados = sorted((n for n in nomes if n in profs),
                       key=lambda n: profs[n].get("last_visit", ""))
    return nunca + visitados


def perfis_devidos(paths, horas, forcar=False):
    if not carregar_perfis(paths):
        return False
    if forcar:
        return True
    if horas <= 0:
        return False
    ult = carregar_estado(paths).get("ultima_rodada_epoch", 0)
    return time.time() - ult >= horas * 3600 - 600


def classificar_vazio(page):
    try:
        texto = page.locator("body").inner_text(timeout=5000).lower()
    except Exception:
        return "sem_posts_capturados"
    if "conta é privada" in texto or "account is private" in texto:
        return "perfil_privado"
    if ("não está disponível" in texto or "isn't available" in texto
            or "isn\u2019t available" in texto):
        return "perfil_nao_encontrado"
    return "sem_posts_capturados"


def rolar_perfil(page, cap, seen, baseline):
    """Retorna o motivo da parada: ok_baseline, known, end ou cap."""
    scrolls = stagnant = 0
    last = len(cap.by_code)
    cap.read_dom(page, grid=True)
    while True:
        codes = list(cap.by_code)
        if baseline:
            if len(codes) >= BASELINE_POSTS:
                return "ok_baseline"
        else:
            if sum(1 for c in codes if c not in seen) >= MAX_NEW_PER_PROFILE:
                return "cap"
            tail = codes[-KNOWN_STREAK:]
            if len(tail) >= KNOWN_STREAK and all(c in seen for c in tail):
                return "known"
        if stagnant >= STAGNANT_LIMIT:
            return "end"
        if scrolls >= MAX_SCROLLS_PROFILE:
            return "cap"
        page.mouse.wheel(0, random.randint(600, 1200))
        time.sleep(random.uniform(1.5, 4.0))
        if random.random() < 0.15:
            time.sleep(random.uniform(4, 10))
        scrolls += 1
        cap.read_dom(page, grid=True)
        if len(cap.by_code) == last:
            stagnant += 1
        else:
            stagnant, last = 0, len(cap.by_code)


def visitar_perfil(page, perfil, entry, nome):
    """Visita um perfil. Retorna (resultado, itens_timeline, erro_fatal, entrada_estado, posts_brutos)."""
    seen = set(entry.get("seen", []))
    baseline = not entry.get("baseline_done")
    cap = Captura(dono=perfil)
    url = f"https://www.instagram.com/{perfil}/"
    agora = datetime.now(BRT)
    coletado_em = agora.strftime("%Y-%m-%d %H:%M:%S")
    agora_epoch = time.time()
    res = {"profile": perfil, "url": url, "visita_brasilia": coletado_em,
           "modo": "baseline" if baseline else "incremental",
           "status": "ok", "n_novos": 0}
    tl, brutos, fatal, motivo = [], [], None, None

    handler = cap.on_response
    page.on("response", handler)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(random.uniform(3, 6))
        if "accounts/login" in page.url or "challenge" in page.url:
            res["status"] = fatal = "sessao_caida_ou_checkpoint"
        else:
            cap.ingest_html(page)
            motivo = rolar_perfil(page, cap, seen, baseline)
            time.sleep(2)
            cap.ingest_html(page)
            cap.read_dom(page, grid=True)
    except Exception as e:
        res["status"] = "erro_navegacao"
        res["erro"] = str(e)[:200]
    finally:
        page.remove_listener("response", handler)

    if cap.rate_limited and not fatal:
        res["status"] = fatal = "bloqueio_429"
    if res["status"] == "ok" and not cap.by_code:
        res["status"] = classificar_vazio(page)
        if res["status"] == "sem_posts_capturados":
            res["urls_vistas"] = cap.urls

    if res["status"] == "ok":
        novos = [c for c in cap.by_code if c not in seen]
        ordenados = ([c for c in cap.dom_order if c in novos]
                     + [c for c in novos if c not in cap.dom_order])
        ordenados = ordenados[:BASELINE_POSTS if baseline else MAX_NEW_PER_PROFILE]
        brutos = [cap.by_code[c] for c in ordenados]
        tl = [to_timeline_item(p, i + 1, "perfil_monitorado") for i, p in enumerate(brutos)]
        res["n_novos"] = len(brutos)
        if motivo == "cap" and not baseline:
            res["aviso"] = "limite_atingido_pode_haver_posts_nao_coletados"
        entry = {"seen": sorted(seen | set(ordenados)), "baseline_done": True,
                 "last_visit": agora.isoformat(timespec="seconds")}
    elif res["status"] in ("perfil_privado", "perfil_nao_encontrado", "sem_posts_capturados"):
        entry = dict(entry, last_visit=agora.isoformat(timespec="seconds"))
    return res, tl, fatal, entry, brutos


def coletar_perfis(page, nome, paths, limite_min=MAX_PERFIS_MIN):
    perfis = carregar_perfis(paths)
    if not perfis:
        log(paths, "[perfis] profiles.txt vazio: nada a visitar.")
        return "sem_perfis"
    estado = carregar_estado(paths)
    ordem = ordenar_perfis(perfis, estado)
    inicio = datetime.now(BRT)
    ts = inicio.strftime("%Y%m%dT%H%M%S")
    estado["ultima_rodada_epoch"] = time.time()
    salvar_json(paths.state_profiles, estado)
    pasta = paths.pasta("profiles")
    out_tl = pasta / f"timeline_profiles_{ts}.json"
    rnd = {"sockpuppet": nome, "inicio_brasilia": inicio.strftime("%Y-%m-%d %H:%M:%S"),
           "status": "em_andamento", "n_perfis_lista": len(perfis), "perfis": []}
    tl_all, raw_all = [], []
    abort, ruins, t0 = None, 0, time.time()
    try:
        for i, perfil in enumerate(ordem):
            if stop_requested(paths):
                abort = "interrompido_STOP"
                break
            if time.time() - t0 > limite_min * 60:
                abort = "interrompido_tempo_maximo"
                break
            res, tl, fatal, entry, brutos = visitar_perfil(
                page, perfil, estado["profiles"].get(perfil, {}), nome)
            log(paths, f"[perfis] {perfil} | {res['modo']} | {res['status']} | {res['n_novos']} novos")
            if res["status"] != "erro_navegacao" and not fatal:
                estado["profiles"][perfil] = entry
            rnd["perfis"].append(res)
            tl_all.extend(tl)
            raw_all.extend(brutos)
            salvar_json(out_tl, tl_all)
            salvar_json(paths.state_profiles, estado)
            if fatal:
                abort = fatal
                break
            ruins = ruins + 1 if res["status"] in ("sem_posts_capturados", "erro_navegacao") else 0
            if ruins >= MAX_PERFIS_FALHAS:
                abort = "interrompido_5_falhas_seguidas"
                break
            if i < len(ordem) - 1:
                time.sleep(random.uniform(*PAUSE_BETWEEN_PROFILES))
                if random.random() < 0.10:
                    time.sleep(random.uniform(20, 45))
    except Exception:
        abort = abort or "erro_inesperado"
        raise
    finally:
        fim = datetime.now(BRT)
        rnd["status"] = abort or "completo"
        rnd["fim_brasilia"] = fim.strftime("%Y-%m-%d %H:%M:%S")
        rnd["n_perfis_visitados"] = len(rnd["perfis"])
        rnd["n_posts_novos"] = sum(r["n_novos"] for r in rnd["perfis"])
        salvar_json(out_tl, tl_all)
        salvar_json(paths.state_profiles, estado)
        (pasta / "raw" / f"profiles_{ts}_raw.json").write_text(
            json.dumps({"meta": rnd, "posts": raw_all}, ensure_ascii=False), encoding="utf-8")
    log(paths, f"[perfis] {rnd['status']} | {rnd['n_perfis_visitados']}/{len(ordem)} perfis "
               f"| {rnd['n_posts_novos']} posts novos")
    return rnd["status"]


# ------------------------------------------------------------------------ ciclo
def collect_cycle(nome, paths, fazer_perfis, limite_perfis_min=MAX_PERFIS_MIN, cfg_curtidas=None):
    """Uma abertura do Chrome: feed geral, aba Seguindo e (se devido) a lista de perfis."""
    port = free_port()
    proc = launch_chrome(paths.profile, port, "about:blank")
    resultados = {}
    try:
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
                # perfis acrescentados durante este ciclo (por curtir+seguir) entram na rodada de agora
                fazer_perfis = fazer_perfis or bool(PERFIS_NOVOS)
                if fazer_perfis and PERFIS_NOVOS:
                    log(paths, "[perfis] novos neste ciclo: " + ", ".join("@" + u for u in PERFIS_NOVOS))
                if fazer_perfis and not stop_requested(paths):
                    time.sleep(random.uniform(3, 8))
                    try:
                        resultados["perfis"] = coletar_perfis(page, nome, paths, limite_perfis_min)
                    except Exception as e:
                        resultados["perfis"] = f"erro: {e}"
                        log(paths, f"[perfis] ERRO: {e}")
                PERFIS_NOVOS.clear()
                if cfg_curtidas and cfg_curtidas.get("seguir") and cfg_curtidas.get("seguir_todas") \
                        and not stop_requested(paths):
                    time.sleep(random.uniform(3, 8))
                    try:
                        seguir_contas(page, nome, paths, cfg_curtidas)
                    except Exception as e:
                        log(paths, f"[seguir] ERRO: {e}")
            graceful_close(browser, proc)
    finally:
        if proc.poll() is None:
            kill_chrome(proc)
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


def run_loop(nome, paths, intervalo, jitter, uma_vez, perfis_horas, forcar_perfis, curtidas=None):
    falhas = 0
    base = time.time()
    while not stop_requested(paths):
        try:
            a_cada_ciclo = perfis_horas > 0 and perfis_horas * 3600 <= intervalo
            if forcar_perfis or a_cada_ciclo:
                fazer = bool(carregar_perfis(paths))
            else:
                fazer = perfis_devidos(paths, perfis_horas)
            # se os perfis rodam todo ciclo, a rodada para cedo para o ciclo caber na hora
            limite = max(10, intervalo // 60 - 20) if a_cada_ciclo else MAX_PERFIS_MIN
            forcar_perfis = False
            if not fazer and perfis_horas > 0 and carregar_perfis(paths):
                desde = time.time() - carregar_estado(paths).get("ultima_rodada_epoch", 0)
                falta = max(0, perfis_horas * 3600 - desde)
                log(paths, f"[perfis] fora de hora neste ciclo: última rodada há {int(desde // 60)} min; "
                           f"próxima em ~{int(falta // 60)} min (a cada {perfis_horas} h)")
            cfg_curtidas, motivo = montar_cfg_curtidas(nome, curtidas)
            if motivo:
                log(paths, f"[curtidas] desligado: {motivo}")
            if cfg_curtidas:
                for lin in cfg_curtidas["ignoradas"][:5]:
                    log(paths, f"[curtidas] AVISO orientacao.txt: linha ignorada: {lin}")
                if cfg_curtidas["orientacao"] != "neutro" and not (cfg_curtidas["contas"] or cfg_curtidas["termos"]):
                    log(paths, "[curtidas] AVISO: nenhuma regra valida em orientacao.txt; "
                               f"'{nome}' ({cfg_curtidas['orientacao']}) nao tem o que seguir nem curtir")
            res = collect_cycle(nome, paths, fazer, limite, cfg_curtidas)
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
    ap.add_argument("--perfis-horas", type=int, default=DEFAULT_PERFIS_HORAS,
                    help="de quantas em quantas horas visitar profiles.txt (0 desliga; padrão 1)")
    ap.add_argument("--forcar-perfis", action="store_true",
                    help="visita os perfis já na primeira coleta desta execução")
    ap.add_argument("--curtidas-por-ciclo", type=int, default=DEFAULT_CURTIDAS_POR_CICLO,
                    help="maximo de curtidas por feed em cada ciclo (0 desliga; padrão 100000 = sem limite)")
    ap.add_argument("--curtidas-por-dia", type=int, default=DEFAULT_CURTIDAS_POR_DIA,
                    help="maximo de curtidas por dia, somando os feeds (padrão 100000 = sem limite)")
    ap.add_argument("--seguir-por-ciclo", type=int, default=DEFAULT_SEGUIR_POR_CICLO,
                    help="contas novas seguidas por ciclo (0 desliga; padrão 20)")
    ap.add_argument("--seguir-por-dia", type=int, default=DEFAULT_SEGUIR_POR_DIA,
                    help="maximo de contas novas seguidas por dia (padrão 100)")
    ap.add_argument("--seguir-todas", action="store_true",
                    help="alem de seguir os autores dos posts curtidos, segue a lista inteira do orientacao.txt")
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
    garantir_profiles_txt(paths)
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
                    "onde": onde, "seguir_por_ciclo": args.seguir_por_ciclo,
                    "seguir_por_dia": args.seguir_por_dia, "seguir_todas": args.seguir_todas}
        ori, erro = orientacao_do_sockpuppet(nome)
        if args.curtidas_por_ciclo > 0 or args.seguir_por_ciclo > 0:
            print(f"Orientacao: {'ERRO - ' + erro if erro else ('neutro (nao segue nem curte)' if ori == 'neutro' else (ori or 'desligada (nome nao esta em sockpuppets.txt)'))}")
        run_loop(nome, paths, args.intervalo, args.jitter, args.uma_vez,
                 args.perfis_horas, args.forcar_perfis, curtidas)
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuário.")


if __name__ == "__main__":
    main()
