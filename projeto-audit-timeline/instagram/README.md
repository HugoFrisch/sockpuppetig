# Auditoria de Timeline: Instagram

Mesmo modelo do projeto TikTok: um diretório por sockpuppet e execução por
`sockpuppetRun.py`. Uso exclusivamente acadêmico, sob autorização dos autores.

## 1. Estrutura
instagram/sock_[nome]/ contém: navegador_dados_[nome]/ (perfil do Chrome),
instagram_session_[nome].json, coleta.log e snapshots/. Um diretório por
sockpuppet; nunca compartilhe perfis ou sessões entre eles.
Nomes: começam com letra, só letras e números, únicos no projeto (ana01, joao02).

## 2. Dependências
Python 3.10+ e Google Chrome instalado (é usado o Chrome real, não o Chromium).
    pip install -r requirements.txt
Não é necessário `playwright install`. Se o Chrome estiver em local não padrão,
defina a variável de ambiente CHROME_PATH.

## 3. Conta
Crie a conta do Instagram com e-mail dedicado ao experimento, de preferência
pela mesma rede (IP) em que a coleta vai rodar. Mantenha dados de cada
sockpuppet separados. Não use a conta em outros dispositivos durante a coleta.

## 4. Configuração inicial
    python sockpuppetRun.py
Informe o nome. O Chrome abre: faça login (senha, 2FA e verificações), configure
o perfil conforme o protocolo e pressione ENTER no terminal. O script confirma que
o login foi salvo, fecha o navegador e inicia a primeira coleta.
A primeira execução exige terminal interativo (não use o Agendador de Tarefas
nela).

## 5. Periodicidade
Padrão: 1 hora (`--intervalo 3600`), com atraso aleatório de até 5 min
(`--jitter 300`). Teste com `--uma-vez`.

## 6. Reinício
Rode `python sockpuppetRun.py` e informe o mesmo nome. A sessão é reutilizada.
Se aparecer SESSÃO CAÍDA OU CHECKPOINT, a coleta para: resolva manualmente e
reinicie. O script nunca tenta resolver verificações sozinho.

## 7. Reconfiguração completa (evite)
Apague navegador_dados_[nome] e instagram_session_[nome].json e rode de novo.

## 8. Saídas (por coleta, em snapshots/)
- feed_<ts>.json      modelo completo + diagnóstico de ordem
- timeline_<ts>.json  modelo padronizado do projeto (lista de itens)
- raw/feed_<ts>_raw.json  dados brutos e ordem do DOM
Ordem (`visual_feed_index` / `position`): ordem dos posts na tela (DOM).
`api_arrival_index`: ordem de chegada dos dados.

## 9. Dados e ética
Arquivos de sessão e coletas contêm dados de terceiros e cookies. Não versionar
(ver .gitignore) e tratar conforme o protocolo da pesquisa.
A coleta automatizada pode violar os Termos de Uso do Instagram e a conta pode
ser limitada ou banida.