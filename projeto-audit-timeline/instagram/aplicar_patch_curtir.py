"""Aplica a correcao de curtir_post no sockpuppetRun.py (faz backup antes).
Uso (na pasta do projeto):  python aplicar_patch_curtir.py
"""
import re, shutil, sys, py_compile
from pathlib import Path

alvo = Path("sockpuppetRun.py")
if not alvo.exists():
    sys.exit("sockpuppetRun.py nao encontrado nesta pasta.")

src = alvo.read_text(encoding="utf-8")
if "def achar_artigo(" in src:
    sys.exit("Patch ja aplicado. Nada a fazer.")

novo = '''def achar_artigo(page, code, max_passos=40):
    """Procura o post na tela; se a pagina ja o descartou, volta ao topo e desce ate achar."""
    sel = f'a[href*="/p/{code}/"], a[href*="/reel/{code}/"]'

    def tenta():
        art = page.locator("article").filter(has=page.locator(sel)).first
        return art if art.count() > 0 else None

    art = tenta()
    if art:
        return art, 0
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(random.uniform(1.5, 2.5))
    for passo in range(1, max_passos + 1):
        art = tenta()
        if art:
            return art, passo
        page.mouse.wheel(0, random.randint(500, 800))
        time.sleep(random.uniform(1.0, 2.0))
    return None, max_passos


def curtir_post(page, code):
    """Clica em Curtir no post `code` que esta no feed. Retorna o status."""
    art, passos = achar_artigo(page, code)
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


'''
m = re.search(r"def curtir_post\(page, code\):.*?(?=\ndef montar_cfg_curtidas)", src, re.S)
if not m:
    sys.exit("Nao achei curtir_post no arquivo. Nada foi alterado.")
out = src[:m.start()] + novo.rstrip("\n") + "\n\n" + src[m.end():]

shutil.copy(alvo, "sockpuppetRun.py.bak")
alvo.write_text(out, encoding="utf-8")
try:
    py_compile.compile(str(alvo), doraise=True)
except Exception as e:
    shutil.copy("sockpuppetRun.py.bak", alvo)
    sys.exit(f"Erro de sintaxe apos o patch; arquivo restaurado. Detalhe: {e}")
print("Patch aplicado. Backup em sockpuppetRun.py.bak")
