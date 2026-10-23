# Auditoria de Timeline: Instagram

Mesmo modelo do projeto TikTok: um diretório por sockpuppet e execução por
`sockpuppetRun.py`. Uso exclusivamente acadêmico, sob autorização dos autores.

Este projeto tem duas ferramentas independentes:

- **Bot de coleta** (`instagram/sockpuppetRun.py`): coleta o feed geral e a aba "Seguindo" de hora em hora e,
  conforme a orientação do sockpuppet, curte posts.
- **Stalker** (`stalker/stalker.py`): verifica quais contas da lista o sockpuppet segue e acompanha os
  posts desses perfis, de 12 em 12 horas. Só lê: não curte, não segue e não comenta.

## 1. Estrutura
    projeto-audit-timeline/
      instagram/
        sockpuppetRun.py      bot de coleta
        sockpuppets.txt       orientação de cada sockpuppet
        orientacao.txt        contas, partido e lado (@perfil,PARTIDO,lado)
        requirements.txt
        sock_[nome]/          um diretório por sockpuppet (criado automaticamente)
          navegador_dados_[nome]/        perfil do Chrome (login)
          instagram_session_[nome].json
          feed_geral/  feed_seguindo/    coletas (timeline_*.json e raw/)
          interacoes/curtidas.jsonl      registro de curtidas
          coleta.log
      stalker/
        stalker.py
        saida_[nome]/         resultados do stalker (seguimento/, perfis/, stalker.log)
Nomes de sockpuppet: começam com letra, só letras e números, únicos no projeto (ana01, joao02).
Nunca compartilhe perfis ou sessões entre sockpuppets.

## 2. Dependências
Python 3.10+ e Google Chrome instalado (é usado o Chrome real, não o Chromium).

    cd instagram
    python -m venv venv
    venv\Scripts\Activate.ps1        (PowerShell; no Git Bash: source venv/Scripts/activate)
    pip install -r requirements.txt

Não é necessário `playwright install`. Se o Chrome estiver em local não padrão,
defina a variável de ambiente `CHROME_PATH`. O stalker usa o mesmo venv.

## 3. Conta
Crie a conta do Instagram com e-mail dedicado ao experimento, de preferência
pela mesma rede (IP) em que a coleta vai rodar. Mantenha dados de cada
sockpuppet separados. Não use a conta em outros dispositivos durante a coleta.

## 4. Orientação dos sockpuppets
Em `sockpuppets.txt`, uma linha por sockpuppet: `nome orientacao`.

| Orientação | Curte |
|---|---|
| neutro | nada (só coleta) |
| esquerda | contas do PT |
| direita | contas do PL |
| balanceado | esquerda e direita |
| total esquerda | todas as contas de esquerda |
| total direita | todas as contas de direita |

`orientacao.txt` lista as contas no formato `@perfil,PARTIDO,lado`. Só posts de contas
listadas ali são rotulados. Curtidas ocorrem apenas no feed geral. O bot não segue contas.
Registro das curtidas em `sock_[nome]/interacoes/curtidas.jsonl`.
Diagnóstico sem abrir o Chrome: `python sockpuppetRun.py --nome joao1 --testar-curtidas`.
Desligar curtidas: `--curtidas-por-ciclo 0`.

## 5. Configuração inicial
    python sockpuppetRun.py
Informe o nome. O Chrome abre: faça login (senha, 2FA e verificações), configure
o perfil conforme o protocolo e pressione ENTER no terminal. O script confirma que
o login foi salvo, fecha o navegador e inicia a primeira coleta.
A primeira execução exige terminal interativo (não use o Agendador de Tarefas nela).

## 6. Periodicidade
Padrão: 1 hora (`--intervalo 3600`), com atraso aleatório de até 5 min
(`--jitter 300`). Teste com `--uma-vez`. Para parar: Ctrl+C, ou crie um arquivo
`STOP` dentro de `sock_[nome]/`.

## 7. Reinício
Rode `python sockpuppetRun.py --nome [nome]`. A sessão é reutilizada.
Se aparecer SESSÃO CAÍDA OU CHECKPOINT, a coleta para: resolva manualmente e
reinicie. O script nunca tenta resolver verificações sozinho.

## 8. Reconfiguração completa (evite)
Apague `navegador_dados_[nome]` e `instagram_session_[nome].json` e rode de novo.

## 9. Saídas (por coleta)
- `feed_geral/timeline_<ts>.json` e `feed_seguindo/timeline_following_<ts>.json`:
  modelo padronizado do projeto (lista de itens).
- `raw/*_raw.json`: dados brutos e ordem do DOM.
- `visual_feed_index`: ordem dos posts na tela (DOM). `api_arrival_index`: ordem de chegada dos dados.
- `playCount` e `collectCount` ficam `null`: o Instagram não os entrega no feed.

## 10. Stalker
Ver `stalker/README.md`. Resumo:

    cd stalker
    ..\instagram\venv\Scripts\Activate.ps1
    python stalker.py --sock joao1          (a cada 12 h)
    python stalker.py --sock todos          (todos os sockpuppets, em fila)

Gera `saida_[nome]/seguimento/seguimento_atual.csv` (quem a conta segue, entre as contas da lista)
e `saida_[nome]/perfis/` (3 posts na primeira visita e depois só os novos).
Bot e stalker usam o mesmo login, então nunca rodam ao mesmo tempo: a trava
`sock_[nome]/EXECUCAO.lock` faz quem chega depois esperar o outro terminar.

## 11. Dados e ética
Arquivos de sessão e coletas contêm dados de terceiros e cookies. Não versionar
(ver .gitignore) e tratar conforme o protocolo da pesquisa.
A coleta automatizada pode violar os Termos de Uso do Instagram e a conta pode
ser limitada ou banida. Curtidas são interações reais: use somente dentro do protocolo.
