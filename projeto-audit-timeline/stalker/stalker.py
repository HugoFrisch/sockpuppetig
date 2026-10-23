#!/usr/bin/env python3
"""
stalker.py - Monitor de perfis (separado do sockpuppet).

O que faz, a cada rodada (padrao: de 12 em 12 horas):
  1) le a lista de contas do orientacao.txt do projeto do sockpuppet;
  2) descobre QUAIS dessas contas a conta do sockpuppet segue e quais nao segue
     (relatorio em saida_<sock>/seguimento/);
  3) entra so nos perfis que a conta segue:
       - 1a visita: guarda os 3 primeiros posts (padrao; --posts-iniciais);
       - visitas seguintes: guarda apenas os posts novos.
     Saida no formato do professor em saida_<sock>/perfis/.
NAO curte, NAO segue e NAO comenta: so le.

Usa o MESMO login (perfil do Chrome) do sockpuppet, por isso nao roda junto com ele:
os dois usam a trava sock_<nome>/EXECUCAO.lock. Quem chega depois espera o outro terminar
(vale tanto para o stalker esperar o sockpuppet quanto o contrario).

Uso (dentro da pasta stalker, com o venv ativo):
  python stalker.py --sock joao1                  (loop de 12 em 12 horas)
  python stalker.py --sock todos                  (todos os sockpuppets existentes, um de cada vez)
  python stalker.py --sock joao1 --uma-vez        (uma rodada e sai)
  python stalker.py --sock joao1 --so-seguimento  (so verifica quem segue, sem coletar posts)

Para parar: Ctrl+C, ou crie um arquivo chamado STOP dentro de saida_<sock>.
"""
import argparse
import csv
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
DEFAULT_INSTAGRAM_DIR = BASE_DIR.parent / "instagram"   # onde estao sock_<nome>/ e orientacao.txt
DEFAULT_INTERVAL = 12 * 3600       # 12 horas
DEFAULT_JITTER = 600               # ate 10 min de atraso aleatorio
DEFAULT_POSTS_INICIAIS = 3         # 1a visita: quantos posts guardar (2 a 3)
MAX_NOVOS_POR_PERFIL = 12          # trava de seguranca: novos posts guardados por visita
DEFAULT_MAX_MIN = 0                # 0 = sem limite de tempo na rodada de perfis
DEFAULT_ESPERAR_CHROME_MIN = 120   # quanto esperar o Chrome do sockpuppet liberar
PAUSA_ENTRE_PERFIS = (8, 20)       # segundos entre um perfil e o proximo
MAX_FALHAS_PERFIS = 5              # aborta a rodada apos 5 perfis seguidos com problema
MAX_FALHAS_SEGUIDAS = 3
LOCK_VELHO_HORAS = 4               # trava esquecida ha mais tempo que isso e ignorada
BRT = timezone(timedelta(hours=-3))
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
MEDIA_TYPE = {1: "image", 2: "video", 8: "carousel"}
HOME_URL = "https://www.instagram.com/"

RX_SEGUIR = re.compile(r"^(Seguir|Follow|Seguir de volta|Follow back)$", re.I)
RX_SEGUINDO = re.compile(r"^(Seguindo|Following)", re.I)
RX_SOLICITADO = re.compile(r"^(Solicitado|Requested)", re.I)

JS_SCRIPTS = """() => [...document.querySelectorAll('script[type="application/json"]')]
                       .map(s => s.textContent)"""
JS_GRID = r"""() => [...document.querySelectorAll('main a[href*="/p/"], main a[href*="/reel/"]')]
  .map(a => { const m = (a.getAttribute('href') || '').match(/\/(?:p|reel)\/([A-Za-z0-9_-]+)/); return m ? m[1] : null; })
  .filter(Boolean)"""
JS_SEGUINDO = """async (uid) => {
  const nomes = []; let max = null;
  for (let i = 0; i < 40; i++) {
    const url = `/api/v1/friendships/${uid}/following/?count=200` + (max ? `&max_id=${encodeURIComponent(max)}` : "");
    const r = await fetch(url, {credentials: "include",
      headers: {"X-IG-App-ID": "936619743392459", "X-Requested-With": "XMLHttpRequest"}});
    if (!r.ok) return {erro: "http_" + r.status, nomes};
    const j = await r.json();
    for (const u of (j.users || [])) nomes.push(String(u.username).toLowerCase());
    if (!j.next_max_id) return {nomes, completo: true};
    max = j.next_max_id;
    await new Promise(res => setTimeout(res, 1200 + Math.random() * 1500));
  }
  return {nomes, completo: false};
}"""


# ----------------------------------------------------------------- utilidades
class Paths:
    def __init__(self, sock, instagram_dir):
        self.instagram_dir = Path(instagram_dir)
        self.sock_dir = self.instagram_dir / f"sock_{sock}"
        self.chrome_profile = self.sock_dir / f"navegador_dados_{sock}"
        self.session = self.sock_dir / f"instagram_session_{sock}.json"
        self.lock = self.sock_dir / "EXECUCAO.lock"
        self.orientacao = self.instagram_dir / "orientacao.txt"
        self.out = BASE_DIR / f"saida_{sock}"
        self.seguimento = self.out / "seguimento"
        self.perfis = self.out / "perfis"
        self.log = self.out / "stalker.log"
        self.state = self.out / "state.json"
        self.stop = self.out / "STOP"


def now_str():
    return datetime.now(BRT).strftime("%Y-%m-%d %H:%M:%S")


def now_iso():
    return datetime.now(BRT).replace(tzinfo=None).isoformat()


def log(paths, msg):
    linha = f"[{now_str()}] {msg}"
    print(linha, flush=True)
    try:
        paths.out.mkdir(parents=True, exist_ok=True)
        with open(paths.log, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass


def stop_requested(paths):
    return paths.stop.exists()


def salvar_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def find_chrome():
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    sysname = platform.system()
    if sysname == "Windows":
        cands = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")]
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
    raise RuntimeError("Não consegui conectar ao Chrome. Feche qualquer janela do Chrome que "
                       "use o perfil do sockpuppet e tente de novo.")


def kill_chrome(proc):
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def launch_chrome(profile, port, url="about:blank"):
    proc = subprocess.Popen([find_chrome(), f"--user-data-dir={profile}",
                             f"--remote-debugging-port={port}", "--window-size=1280,900",
                             "--lang=pt-BR", "--no-first-run", "--no-default-browser-check", url])
    try:
        wait_cdp(port)
    except Exception:
        kill_chrome(proc)
        raise
    return proc


def graceful_close(browser, proc):
    try:
        browser.new_browser_cdp_session().send("Browser.close")
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
    except Exception:
        kill_chrome(proc)


def perfil_em_uso(profile):
    """True se o Chrome ja esta aberto com este perfil (ex.: o sockpuppet coletando)."""
    try:
        if platform.system() == "Windows":
            lock = Path(profile) / "lockfile"
            if not lock.exists():
                return False
            try:
                with open(lock, "rb"):
                    return False
            except PermissionError:
                return True
            except OSError:
                return True
            return False
        return (Path(profile) / "SingletonLock").is_symlink() or (Path(profile) / "SingletonLock").exists()
    except Exception:
        return False


def esperar(paths, segundos):
    alvo = time.time() + segundos
    while time.time() < alvo:
        if stop_requested(paths):
            return
        time.sleep(min(5, max(0, alvo - time.time())))


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



# ----------------------------------------------------------------- lista de contas
def carregar_lista(paths):
    """Contas do orientacao.txt, na ordem do arquivo: [{perfil, partido, lado}]."""
    if not paths.orientacao.exists():
        return []
    out, vistos = [], set()
    for linha in paths.orientacao.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        partido = lado = None
        if "," in linha:
            p = [x.strip() for x in linha.split(",") if x.strip()]
            if p and p[0].lower() == "termo":
                continue
            if len(p) == 3 and p[0].lower() == "conta":
                valor, lado = p[1], p[2].lower()
            elif len(p) == 3:
                valor, partido, lado = p[0], p[1].upper(), p[2].lower()
            elif len(p) == 2:
                valor, lado = p[0], p[1].lower()
            else:
                continue
        else:
            p = linha.split()
            if p and p[0].lower() == "termo":
                continue
            if len(p) == 3 and p[0].lower() == "conta":
                valor, lado = p[1], p[2].lower()
            elif len(p) == 2:
                valor, lado = p[0], p[1].lower()
            else:
                continue
        m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", valor)
        nome = (m.group(1) if m else valor).lstrip("@").strip("/ ").lower()
        if lado in ("esquerda", "direita") and re.fullmatch(r"[a-z0-9._]{1,30}", nome) and nome not in vistos:
            vistos.add(nome)
            out.append({"perfil": nome, "partido": partido, "lado": lado})
    return out


# ------------------------------------------------------------------ extração
def extract_profile_posts(payload):
    found = []

    def adapt_old(o):
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

    def walk(o):
        if isinstance(o, dict):
            if (o.get("code") and o.get("pk") and ("taken_at" in o or "media_type" in o)
                    and o.get("product_type") != "carousel_item"
                    and not o.get("carousel_parent_id")):
                found.append(o)
                return
            if o.get("shortcode") and o.get("id") and o.get("taken_at_timestamp"):
                found.append(adapt_old(o))
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(payload)
    return found


def parse_payloads(text):
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


def media_type_name(post):
    mt = post.get("media_type")
    return MEDIA_TYPE.get(mt, f"unknown_{mt}")


def rel_path(post):
    kind = "reel" if post.get("product_type") == "clips" else "p"
    return f"/{kind}/{post.get('code')}/"


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


def play_count(post):
    for k in ("play_count", "view_count", "ig_play_count"):
        if post.get(k) is not None:
            return post.get(k)
    return None


def is_ad(post):
    return any(bool(post.get(k)) for k in ("ad_id", "is_ad", "ad_action"))


def to_timeline_item(post, vis_idx, reason=None):
    """Mesmo modelo do professor usado no sockpuppet."""
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
        "author": {"uniqueId": u.get("username"), "id": u.get("pk"),
                   "verified": u.get("is_verified"), "isFollowed": fs.get("following")},
        "isAd": is_ad(post),
        "isPaidContent": post.get("is_paid_partnership"),
        "isAIGCGenerated": post.get("ai_label_info") is not None,
        "itemRecommendReason": post.get("inventory_source") or reason,
        "contents": [{"type": "hashtag", "value": h} for h in hashtags(cap)],
        "stats": {"playCount": play_count(post), "diggCount": post.get("like_count"),
                  "commentCount": post.get("comment_count"),
                  "shareCount": post.get("media_repost_count"), "collectCount": None},
    }


class Captura:
    """Posts de UM perfil (rede + HTML) e a ordem em que a grade os mostra."""

    def __init__(self, dono):
        self.dono = dono
        self.by_code = {}
        self.dom_order = []
        self.contador = 0
        self.rate_limited = False
        self.urls = []

    def ingest(self, data, origem):
        for post in extract_profile_posts(data):
            code = post.get("code")
            if not code or code in self.by_code:
                continue
            u = post.get("user")
            if not isinstance(u, dict):
                u = post["user"] = {}
            if u.get("username") and str(u["username"]).lower() != self.dono:
                continue
            if not u.get("username"):
                u["username"] = self.dono
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
        if not ("graphql" in url or "/api/v1/" in url):
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

    def read_dom(self, page):
        try:
            codes = page.evaluate(JS_GRID)
        except Exception:
            return
        for c in codes:
            if c and c not in self.dom_order:
                self.dom_order.append(c)


# ------------------------------------------------------------ seguimento
def obter_seguindo(page, ctx):
    """Lista de quem a conta segue (pela API do proprio site, na sessao logada).
    Retorna (conjunto_de_usernames | None, detalhe)."""
    uid = None
    try:
        for c in ctx.cookies("https://www.instagram.com"):
            if c["name"] == "ds_user_id":
                uid = c["value"]
    except Exception:
        pass
    if not uid:
        return None, "sem_ds_user_id"
    try:
        r = page.evaluate(JS_SEGUINDO, uid)
    except Exception as e:
        return None, f"erro_js: {str(e)[:100]}"
    if not isinstance(r, dict) or r.get("erro") or not r.get("completo"):
        return None, (r or {}).get("erro") or "lista_incompleta"
    return set(r.get("nomes") or []), "ok"


def estado_botao(page):
    """Le o botao do cabecalho do perfil: segue | solicitado | nao_segue | None."""
    try:
        hdr = page.locator("header")
        if hdr.count() == 0:
            return None
        for rx, st in ((RX_SEGUINDO, "segue"), (RX_SOLICITADO, "solicitado"), (RX_SEGUIR, "nao_segue")):
            if hdr.get_by_role("button", name=rx).count():
                return st
    except Exception:
        pass
    return None


def salvar_seguimento(paths, sock, ts, metodo, lista, segue_map):
    """segue_map: {perfil: True|False|'solicitado'|None(desconhecido)}"""
    contas = []
    for it in lista:
        v = segue_map.get(it["perfil"])
        contas.append({"perfil": it["perfil"], "partido": it["partido"], "lado": it["lado"],
                       "segue": {True: "sim", False: "nao", "solicitado": "solicitado"}.get(v, "desconhecido")})
    resumo = {k: sum(1 for c in contas if c["segue"] == k) for k in ("sim", "nao", "solicitado", "desconhecido")}
    rel = {"sockpuppet": sock, "verificado_em_brasilia": now_str(), "metodo": metodo,
           "total_na_lista": len(lista), "resumo": resumo, "contas": contas}
    salvar_json(paths.seguimento / f"seguimento_{ts}.json", rel)
    for nome in (f"seguimento_{ts}.csv", "seguimento_atual.csv"):
        with open(paths.seguimento / nome, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["perfil", "partido", "lado", "segue"])
            for c in contas:
                w.writerow([c["perfil"], c["partido"] or "", c["lado"], c["segue"]])
    return resumo


# ------------------------------------------------------------ perfis
def carregar_estado(paths):
    if paths.state.exists():
        try:
            st = json.loads(paths.state.read_text(encoding="utf-8"))
            st.setdefault("profiles", {})
            st.setdefault("seguimento", {})
            return st
        except Exception:
            paths.state.replace(paths.state.with_suffix(".bak"))
    return {"profiles": {}, "seguimento": {}}


def ordenar_perfis(nomes, estado):
    """Quem nunca foi visitado primeiro; depois quem foi visitado ha mais tempo."""
    profs = estado["profiles"]
    nunca = [n for n in nomes if n not in profs]
    random.shuffle(nunca)
    vis = sorted((n for n in nomes if n in profs), key=lambda n: profs[n].get("last_visit", ""))
    return nunca + vis


def classificar_vazio(page):
    try:
        texto = page.locator("body").inner_text(timeout=5000).lower()
    except Exception:
        return "sem_posts_capturados"
    if "conta é privada" in texto or "account is private" in texto:
        return "perfil_privado"
    if "não está disponível" in texto or "isn't available" in texto or "isn\u2019t available" in texto:
        return "perfil_nao_encontrado"
    return "sem_posts_capturados"


def visitar_perfil(page, perfil, entry, n_iniciais, verificar_botao):
    """Visita um perfil. Retorna (resultado, itens_timeline, erro_fatal, entrada_estado, posts_brutos)."""
    seen = set(entry.get("seen", []))
    baseline = not entry.get("baseline_done")
    cap = Captura(dono=perfil)
    url = f"https://www.instagram.com/{perfil}/"
    agora = datetime.now(BRT)
    res = {"profile": perfil, "url": url, "visita_brasilia": agora.strftime("%Y-%m-%d %H:%M:%S"),
           "modo": "baseline" if baseline else "incremental", "status": "ok", "n_novos": 0,
           "segue": None}
    tl, brutos, fatal = [], [], None
    handler = cap.on_response
    page.on("response", handler)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(random.uniform(3, 6))
        if "accounts/login" in page.url or "challenge" in page.url:
            res["status"] = fatal = "sessao_caida_ou_checkpoint"
        else:
            if verificar_botao:
                res["segue"] = estado_botao(page)
            if verificar_botao and res["segue"] != "segue":
                # nao segue (ou nao deu para confirmar): nao coleta
                res["status"] = "nao_segue" if res["segue"] in ("nao_segue", "solicitado") else "segue_indeterminado"
            else:
                cap.ingest_html(page)
                cap.read_dom(page)
                if not cap.dom_order:                     # um empurrao so, para a grade carregar
                    page.mouse.wheel(0, random.randint(400, 800))
                    time.sleep(random.uniform(1.5, 3))
                    cap.ingest_html(page)
                    cap.read_dom(page)
                time.sleep(1.5)
                cap.ingest_html(page)
                cap.read_dom(page)
    except Exception as e:
        res["status"] = "erro_navegacao"
        res["erro"] = str(e)[:200]
    finally:
        page.remove_listener("response", handler)

    if cap.rate_limited and not fatal:
        res["status"] = fatal = "bloqueio_429"
    if res["status"] == "ok" and not cap.by_code and not cap.dom_order:
        res["status"] = classificar_vazio(page)
        if res["status"] == "sem_posts_capturados":
            res["urls_vistas"] = cap.urls

    if res["status"] == "ok":
        grade = list(cap.dom_order) + [c for c in cap.by_code if c not in cap.dom_order]
        if baseline:
            # guarda os N primeiros com dados; marca TODA a grade visivel como "ja vista"
            escolhidos = [c for c in grade if c in cap.by_code][:n_iniciais]
            vistos_novos = set(grade)
        else:
            escolhidos = [c for c in grade if c in cap.by_code and c not in seen][:MAX_NOVOS_POR_PERFIL]
            vistos_novos = set(escolhidos)
        brutos = [cap.by_code[c] for c in escolhidos]
        tl = [to_timeline_item(p, i + 1, "perfil_monitorado") for i, p in enumerate(brutos)]
        res["n_novos"] = len(brutos)
        entry = {"seen": sorted(seen | vistos_novos), "baseline_done": True,
                 "last_visit": agora.isoformat(timespec="seconds")}
    elif res["status"] in ("perfil_privado", "perfil_nao_encontrado", "sem_posts_capturados",
                          "nao_segue", "segue_indeterminado"):
        entry = dict(entry, last_visit=agora.isoformat(timespec="seconds"))
    return res, tl, fatal, entry, brutos


# ------------------------------------------------------------------ rodada
def rodada(sock, paths, args):
    trava = TravaExecucao(paths.lock, "o stalker", lambda m: log(paths, m), lambda: stop_requested(paths))
    try:
        return _rodada(sock, paths, args, trava)
    finally:
        trava.liberar()


def _rodada(sock, paths, args, trava):
    lista = carregar_lista(paths)
    if not lista:
        log(paths, f"[stalker] lista vazia: confira {paths.orientacao}")
        return "sem_lista"
    if not paths.session.exists():
        log(paths, f"[stalker] sockpuppet '{sock}' ainda sem login ({paths.session.name}). "
                   "Rode o sockpuppetRun.py uma vez para fazer o login.")
        return "sem_sessao"

    # 1) trava compartilhada com o sockpuppet: um de cada vez
    if not trava.adquirir():
        return "interrompido_STOP"
    # (alguem pode ter o Chrome deste perfil aberto na mao)
    t0, avisou = time.time(), False
    while perfil_em_uso(paths.chrome_profile):
        if stop_requested(paths):
            trava.liberar()
            return "interrompido_STOP"
        if time.time() - t0 > args.esperar_chrome * 60:
            log(paths, f"[stalker] o Chrome deste perfil continuou aberto por {args.esperar_chrome} min; "
                       "rodada adiada para o proximo horario")
            trava.liberar()
            return "perfil_em_uso"
        if not avisou:
            log(paths, "[stalker] ha um Chrome aberto com o perfil do sockpuppet; aguardando fechar...")
            avisou = True
        time.sleep(30)

    estado = carregar_estado(paths)
    ts = datetime.now(BRT).strftime("%Y%m%dT%H%M%S")
    port = free_port()
    proc = None
    status_final = "ok"
    segue_map = {}
    metodo = None
    try:
        proc = launch_chrome(paths.chrome_profile, port)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(random.uniform(4, 7))
            if "accounts/login" in page.url or "challenge" in page.url:
                log(paths, "[stalker] SESSÃO CAÍDA OU CHECKPOINT. Resolva no Chrome do sockpuppet e rode de novo.")
                graceful_close(browser, proc)
                return "sessao_caida_ou_checkpoint"

            # 2) quem a conta segue
            seguindo, detalhe = obter_seguindo(page, ctx)
            if seguindo is not None:
                metodo = "lista_de_seguindo_da_conta"
                for it in lista:
                    segue_map[it["perfil"]] = it["perfil"] in seguindo
                log(paths, f"[seguimento] lista obtida: a conta segue {len(seguindo)} perfis no total")
            else:
                metodo = "botao_no_perfil"
                log(paths, f"[seguimento] nao consegui a lista de seguindo ({detalhe}); "
                           "vou conferir o botao em cada perfil (mais lento)")
            fallback = seguindo is None

            def fechar_relatorio():
                resumo = salvar_seguimento(paths, sock, ts, metodo, lista, segue_map)
                antes = estado.get("seguimento", {})
                novos = [p for p, v in segue_map.items() if v is True and antes.get(p) is False]
                saiu = [p for p, v in segue_map.items() if v is False and antes.get(p) is True]
                log(paths, f"[seguimento] {len(lista)} contas na lista | segue={resumo['sim']} "
                           f"nao_segue={resumo['nao']} solicitado={resumo['solicitado']} "
                           f"desconhecido={resumo['desconhecido']}")
                if novos:
                    log(paths, "[seguimento] passou a seguir: " + ", ".join("@" + x for x in novos))
                if saiu:
                    log(paths, "[seguimento] deixou de seguir: " + ", ".join("@" + x for x in saiu))
                for p, v in segue_map.items():
                    if v is not None:
                        estado["seguimento"][p] = (v is True)
                salvar_json(paths.state, estado)

            if args.so_seguimento and not fallback:
                fechar_relatorio()
                graceful_close(browser, proc)
                return "ok"

            # 3) visitar os perfis seguidos
            alvo = [it["perfil"] for it in lista if fallback or segue_map.get(it["perfil"]) is True]
            ordem = ordenar_perfis(alvo, estado)
            log(paths, f"[perfis] {len(ordem)} perfis a visitar "
                       + (f"(limite de {args.max_min} min)" if args.max_min else "(sem limite de tempo)"))
            out_tl = paths.perfis / f"timeline_perfis_{ts}.json"
            rnd = {"sockpuppet": sock, "inicio_brasilia": now_str(), "status": "em_andamento",
                   "n_perfis_na_lista": len(lista), "perfis": []}
            tl_all, raw_all = [], []
            abort, ruins, t1 = None, 0, time.time()
            try:
                for i, perfil in enumerate(ordem):
                    if stop_requested(paths):
                        abort = "interrompido_STOP"
                        break
                    if args.max_min and time.time() - t1 > args.max_min * 60:
                        abort = "interrompido_tempo_maximo"
                        break
                    res, tl, fatal, entry, brutos = visitar_perfil(
                        page, perfil, estado["profiles"].get(perfil, {}), args.posts_iniciais, fallback)
                    if fallback and res.get("segue"):
                        segue_map[perfil] = {"segue": True, "nao_segue": False,
                                             "solicitado": "solicitado"}.get(res["segue"])
                    log(paths, f"[perfis] {perfil} | {res['modo']} | {res['status']} | {res['n_novos']} posts")
                    if res["status"] != "erro_navegacao" and not fatal:
                        estado["profiles"][perfil] = entry
                    rnd["perfis"].append(res)
                    tl_all.extend(tl)
                    raw_all.extend(brutos)
                    if tl_all:
                        salvar_json(out_tl, tl_all)
                    salvar_json(paths.state, estado)
                    if fatal:
                        abort = fatal
                        break
                    ruins = ruins + 1 if res["status"] in ("sem_posts_capturados", "erro_navegacao") else 0
                    if ruins >= MAX_FALHAS_PERFIS:
                        abort = "interrompido_5_falhas_seguidas"
                        break
                    if i < len(ordem) - 1:
                        time.sleep(random.uniform(*PAUSA_ENTRE_PERFIS))
                        if random.random() < 0.10:
                            time.sleep(random.uniform(15, 40))
            finally:
                rnd["status"] = abort or "completo"
                rnd["fim_brasilia"] = now_str()
                rnd["n_perfis_visitados"] = len(rnd["perfis"])
                rnd["n_posts_coletados"] = sum(r["n_novos"] for r in rnd["perfis"])
                if tl_all:
                    salvar_json(out_tl, tl_all)
                salvar_json(paths.perfis / "raw" / f"perfis_{ts}_raw.json", {"meta": rnd, "posts": raw_all})
                fechar_relatorio()
            log(paths, f"[perfis] {rnd['status']} | {rnd['n_perfis_visitados']}/{len(ordem)} perfis "
                       f"| {rnd['n_posts_coletados']} posts")
            status_final = rnd["status"]
            graceful_close(browser, proc)
    finally:
        if proc is not None and proc.poll() is None:
            kill_chrome(proc)
        trava.liberar()
    return status_final


def run_loop(sock, paths, args):
    falhas = 0
    base = time.time()
    while not stop_requested(paths):
        try:
            res = rodada(sock, paths, args)
            if res in ("sessao_caida_ou_checkpoint", "sem_lista", "sem_sessao"):
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
        if args.uma_vez or stop_requested(paths):
            break
        base += args.intervalo
        espera = base + random.uniform(0, args.jitter) - time.time()
        if espera < 0:
            base = time.time()
            espera = 0
        log(paths, f"Próxima rodada em {int(espera // 3600)} h {int(espera % 3600 // 60)} min")
        esperar(paths, espera)
    if stop_requested(paths):
        log(paths, "Arquivo STOP detectado. Stalker encerrado.")
        try:
            paths.stop.unlink()
        except Exception:
            pass


def bots_existentes(instagram_dir):
    """Sockpuppets prontos (pasta sock_<nome> com login salvo)."""
    nomes = []
    for d in sorted(Path(instagram_dir).glob("sock_*")):
        n = d.name[5:]
        if d.is_dir() and NAME_RE.match(n) and (d / f"instagram_session_{n}.json").exists():
            nomes.append(n)
    return nomes


def run_loop_todos(args):
    """Uma rodada para CADA sockpuppet existente, em fila, a cada intervalo.
    Bots criados depois entram sozinhos na proxima rodada."""
    geral = Paths("todos", args.instagram_dir)          # log e STOP gerais: saida_todos/
    geral.out.mkdir(parents=True, exist_ok=True)
    if geral.stop.exists():
        geral.stop.unlink()
    print(f"Stalker de TODOS os sockpuppets de {geral.instagram_dir}")
    print(f"Para parar: Ctrl+C, ou crie o arquivo: {geral.stop}")
    base = time.time()
    while not stop_requested(geral):
        nomes = bots_existentes(args.instagram_dir)
        log(geral, f"[todos] sockpuppets nesta rodada: {', '.join(nomes) or 'nenhum'}")
        for n in nomes:
            if stop_requested(geral):
                break
            paths = Paths(n, args.instagram_dir)
            paths.stop = geral.stop                     # um unico STOP para todos
            paths.out.mkdir(parents=True, exist_ok=True)
            log(geral, f"[todos] --- {n} ---")
            try:
                res = rodada(n, paths, args)
                log(geral, f"[todos] {n}: {res}")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(paths, f"ERRO: {e}")
                log(geral, f"[todos] {n}: erro ({str(e)[:100]})")
            time.sleep(random.uniform(20, 60))
        if args.uma_vez or stop_requested(geral):
            break
        base += args.intervalo
        espera = base + random.uniform(0, args.jitter) - time.time()
        if espera < 0:
            base = time.time()
            espera = 0
        log(geral, f"Próxima rodada em {int(espera // 3600)} h {int(espera % 3600 // 60)} min")
        esperar(geral, espera)
    if stop_requested(geral):
        log(geral, "Arquivo STOP detectado. Stalker encerrado.")
        try:
            geral.stop.unlink()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Stalker: verifica quem a conta segue e monitora esses perfis.")
    ap.add_argument("--sock", help="nome do sockpuppet cuja conta/login sera usada (ex.: joao1), "
                                   "ou 'todos' para rodar em fila para todos os sockpuppets existentes")
    ap.add_argument("--instagram-dir", default=str(DEFAULT_INSTAGRAM_DIR),
                    help="pasta do projeto do sockpuppet (onde ficam sock_<nome>/ e orientacao.txt)")
    ap.add_argument("--intervalo", type=int, default=DEFAULT_INTERVAL,
                    help="segundos entre rodadas (padrao 43200 = 12 h)")
    ap.add_argument("--jitter", type=int, default=DEFAULT_JITTER, help="atraso aleatorio maximo em segundos")
    ap.add_argument("--posts-iniciais", type=int, default=DEFAULT_POSTS_INICIAIS,
                    help="1a visita de cada perfil: quantos posts guardar (padrao 3)")
    ap.add_argument("--max-min", type=int, default=DEFAULT_MAX_MIN,
                    help="limite de minutos por rodada de perfis (padrao 0 = sem limite)")
    ap.add_argument("--esperar-chrome", type=int, default=DEFAULT_ESPERAR_CHROME_MIN,
                    help="minutos esperando o Chrome do sockpuppet liberar o perfil (padrao 120)")
    ap.add_argument("--so-seguimento", action="store_true", help="so verifica quem a conta segue")
    ap.add_argument("--uma-vez", action="store_true", help="faz uma rodada e sai")
    args = ap.parse_args()

    sock = args.sock or input("Nome do sockpuppet (ex.: joao1) ou 'todos': ").strip()
    if sock.lower() in ("todos", "all"):
        args.posts_iniciais = max(1, min(args.posts_iniciais, 10))
        try:
            run_loop_todos(args)
        except KeyboardInterrupt:
            print("\nInterrompido pelo usuário.")
        return
    if not NAME_RE.match(sock):
        raise SystemExit("Nome inválido.")
    args.posts_iniciais = max(1, min(args.posts_iniciais, 10))
    paths = Paths(sock, args.instagram_dir)
    if not paths.sock_dir.exists():
        raise SystemExit(f"Não achei {paths.sock_dir}. Use --instagram-dir para indicar a pasta do projeto.")
    paths.out.mkdir(parents=True, exist_ok=True)
    if paths.stop.exists():
        paths.stop.unlink()
    print(f"Stalker de '{sock}'. Saída em: {paths.out}")
    print(f"Lista de contas: {paths.orientacao}")
    print(f"Para parar: Ctrl+C, ou crie o arquivo: {paths.stop}")
    try:
        run_loop(sock, paths, args)
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuário.")


if __name__ == "__main__":
    main()
