# Stalker (Instagram)

Roda separado do sockpuppet, a cada 12 horas. Só lê: não curte, não segue, não comenta.

## O que faz
1. Lê a lista de contas do `orientacao.txt` (pasta `instagram\`).
2. Descobre quais dessas contas a conta do sockpuppet segue e quais não segue.
   Relatório em `saida_<sock>\seguimento\` (`seguimento_atual.csv` e um `.json`/`.csv` por rodada).
3. Entra só nos perfis que a conta segue:
   - 1ª visita: guarda os 3 primeiros posts (`--posts-iniciais`).
   - Depois: guarda só os posts novos.
   Saída no formato do professor em `saida_<sock>\perfis\` (`timeline_perfis_*.json` e `raw\`).

## Como usar
Dentro da pasta `stalker\`, com o venv do projeto ativo:

    python stalker.py --sock joao1              (loop de 12 em 12 horas)
    python stalker.py --sock joao1 --uma-vez    (uma rodada, para testar)
    python stalker.py --sock todos              (todos os sockpuppets existentes, em fila)
    python stalker.py --sock joao1 --so-seguimento   (só verifica quem a conta segue)

Parar: Ctrl+C, ou criar um arquivo `STOP` dentro de `saida_<sock>\`.

## Opções
- `--instagram-dir` pasta do projeto do sockpuppet (padrão: `..\instagram`)
- `--intervalo` segundos entre rodadas (padrão 43200 = 12 h)
- `--max-min` limite de minutos por rodada de perfis (padrão 0 = sem limite)
- `--esperar-chrome` minutos esperando o Chrome do sockpuppet liberar (padrão 120)

## Convivência com o sockpuppet
Os dois usam o mesmo login (mesmo perfil do Chrome), então nunca rodam ao mesmo tempo.
Ambos usam a trava `sock_<nome>\EXECUCAO.lock`: quem chega depois espera o outro terminar
(o stalker espera o ciclo do sockpuppet, e o sockpuppet espera a rodada do stalker).
Se um dos dois for fechado de repente, a trava é reconhecida como abandonada e o outro segue.
(Isso exige o `sockpuppetRun.py` atualizado.)

A rodada de perfis não tem limite de tempo (`--max-min 0`). Se quiser um teto, use `--max-min 90`, por exemplo.

## Vários sockpuppets
- `--sock <nome>` serve para qualquer sockpuppet (cada um tem a sua conta, o seu login e a sua pasta `saida_<nome>\`).
- `--sock todos` roda em fila para todos os `sock_*` que já têm login salvo. Sockpuppets criados depois entram
  sozinhos na rodada seguinte. Log geral e `STOP` geral em `saida_todos\`; os dados de cada um ficam em `saida_<nome>\`.
- A trava é por sockpuppet: o stalker de um bot não atrapalha o sockpuppet de outro.
