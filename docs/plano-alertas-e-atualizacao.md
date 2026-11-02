# Plano: saber quando há problema e atualizar o sincronizador à distância

Estado em 01/11/2026. Resultado da revisão do `primavera-sync` e da integração na Lonntra (`ilhapay`).

## 0. Ponto de partida (o que já existe)

- O sync dá **sinal de vida** a cada ciclo (`POST /integracao/primavera/estado`) com versão, contagens, nº de erros e **um** último erro (300 caracteres).
- A Lonntra tem o cron `*/15 integracao/vigiar` (`/etc/cron.d/lonntra-integracao`) que avisa **a empresa** (sino e email da empresa) quando o sync pára, volta, ou tem erros (1 aviso por dia).
- Falta: avisar **o integrador** (tu); detalhe dos erros; erros de arranque; e qualquer forma de mudar o sync sem entrar no servidor.
- Hoje (01/11): **as duas empresas estão paradas** (Auto Style Custom desde 31/10 10:11, Auto Style desde 01/11 08:33), as duas na mesma máquina `svr-Autostyle`. Isto é o primeiro problema a resolver (ver P0).

---

## Parte A. Erros do sync → Lonntra → tu

### A1. O sync passa a relatar eventos estruturados
Um `Relator` no sync, em vez de um texto solto:

| Campo | Exemplo |
|---|---|
| `categoria` | `sql_login`, `esquema`, `documento`, `catalogo`, `stock`, `recebimento`, `lonntra_api`, `atualizacao`, `crash`, `arranque` |
| `nivel` | `info`, `aviso`, `erro`, `fatal` |
| `chave` (deduplica) | `documento:FA 2026/12`, `existencia:SERV0061`, `sql_login` |
| `mensagem` | texto curto, sem dados de clientes |
| `detalhe` | traceback (últimas linhas), só para `erro`/`fatal` |

Vai no sinal de vida: até 20 eventos novos desde o último sinal, mais a **cauda do log** (últimas 30 linhas `ERROR`/`WARNING`) e `ultimo_ciclo_ok` (hora do último ciclo sem erro).

**Erros fatais no arranque** (config inválida, `estado.db` ilegível, login do SQL recusado, falha ao importar): o `main` apanha, envia um sinal com `nivel=fatal` e só depois sai. Hoje o processo morre calado e a Lonntra só nota ao fim de 2 horas.

### A2. A Lonntra guarda os eventos
- Tabela `integracao_evento` (`Entidade_ID`, `CATEGORIA`, `NIVEL`, `CHAVE`, `MENSAGEM`, `DETALHE`, `PRIMEIRA_EM`, `ULTIMA_EM`, `VEZES`, `ESTADO` aberto/resolvido, `NOTIFICADO_EM`).
- O endpoint de estado faz upsert por `(Entidade_ID, CATEGORIA, CHAVE)`: o mesmo erro repetido incrementa `VEZES`, não cria linhas.
- Um evento fica «resolvido» quando o sync deixa de o reportar durante 2 ciclos.

### A3. Quem é avisado, e quando
Duas audiências, porque o cliente só resolve parte:

| Evento | Cliente (sino/email da empresa) | Integrador (tu) |
|---|---|---|
| Sync parado > 2 h, voltou | sim (já existe) | **sim, imediato** |
| `fatal` (arranque, login SQL, esquema) | não | **sim, imediato** |
| Erro novo de uma categoria | não | **sim, imediato** (1 por categoria) |
| Mesmo erro a repetir | não | resumo diário |
| Documento recusado por dado em falta (IVA, cliente) | sim, resumo diário | resumo diário |
| Atualização feita / revertida | não | **sim** |

Canais do integrador: **email** (a Lonntra já tem o SMTP) para a lista `params['integracao']['alertas']`; depois, opcional, **Telegram** (um bot, um `curl`) para os imediatos. Resumo diário às 08:00 com uma tabela por empresa: versão, último contacto, eventos abertos por categoria.

### A3b. Avisar de uma paragem só quando é falha, não fecho do servidor
A empresa fecha o servidor ao sábado às 13h e só o liga na segunda de manhã. Hoje isso gera avisos «parada» falsos. A regra:

| Situação | Aviso? |
|---|---|
| Silêncio dentro do **horário de fecho** da empresa (definido na Lonntra, ex.: sáb 13:00 → seg 08:00, mais feriados) | **não** |
| O sync arranca e a **máquina arrancou há pouco** (uptime do Windows < 10 min): foi um desligar e ligar | **não** |
| O sync arranca e a **máquina está ligada há muito**: o serviço morreu ou foi reiniciado pela tarefa | **sim, a posteriori**: «parou X horas sem o servidor reiniciar», com o último erro |
| O sync reporta `fatal` ou `crash` | **sim, imediato** |
| Silêncio **fora** do horário de fecho e sem arranque a seguir (servidor desligado fora de horas, ou serviço morto) | **sim** |

Para isto o sync envia no arranque um evento `arranque` com `uptime_maquina`, `motivo_paragem_anterior` (lido do `saude.json`: último ciclo bom, último erro) e a versão. A Lonntra guarda o horário de fecho por empresa.

### A4. Menos ruído (o que hoje gera 2535 «erros»)
- **Não reenviar o que a Lonntra recusou** enquanto o dado não mudar: guardar `hash + motivo` no `estado.db`.
- **Não enviar existências de serviços** (artigos com `servico=true`).
- **Instalador:** `tipos_artigo_servico` vazio passa a pedir confirmação explícita.
- Agrupar por motivo: «318 existências: é um serviço», não 318 avisos.

### A5. Painel
Página de Super Admin na Lonntra (e `php yii integracao/estado` já existe): por empresa, versão, último contacto, eventos abertos agrupados por categoria, botão «pedir log» (ver B6).

---

## Parte B. Atualizar o sync de casa, sem reinstalar

### B.0 O que condiciona o desenho (o que já sabemos do terreno)

| Facto | Consequência |
|---|---|
| O servidor do cliente é um Windows que **fecha ao sábado às 13h e só abre na segunda** | A atualização aplica-se quando o servidor liga; nunca depende de ele estar sempre ligado |
| Não podemos abrir portas no cliente | O cliente **vai buscar** (pull). Nunca empurramos nada para lá |
| Corre como **SYSTEM**, por tarefa agendada, uma por empresa (2 hoje) | Quem controla o código controla o servidor: **só código assinado por ti** |
| Tudo o que o sync envia é **idempotente** e o estado grava-se por lote | Reiniciar a meio de um ciclo é seguro: atualizações podem ser imediatas, sem esperar o fim do ciclo (que de noite dura horas) |
| Só há `pyodbc` e a biblioteca standard | O pacote do sync é só Python puro: atualizar = trocar uma pasta |
| Já temos SSH para a Lonntra e a Lonntra já autentica cada empresa (chave Bearer) | A Lonntra serve as versões; o teu Mac publica por SSH. Sem GitHub no meio e sem credenciais novas no cliente |

### B.1 Peças

```
 TEU MAC                         LONNTRA (VPS)                       SERVIDOR DO CLIENTE
 scripts/publicar.sh  ──ssh──►  storage/primavera-sync/v1.2.0.zip
   testes → zip → sha256        integracao_versao (registo)  ◄─ GET atualizacao (com a chave da empresa)
   → assinatura Ed25519         integracao_chave.CANAL/ALVO        launcher.py ─┬─ child: sincronizador (versão X)
                                integracao_comando / _config                    ├─ descarrega + verifica + testa
                                                                                └─ troca de versão / reverte
```

### B.2 O launcher (a única peça que não se atualiza sozinha)

Um ficheiro pequeno (~200 linhas, **só biblioteca standard**), que é o que a tarefa do Windows passa a correr: `python launcher.py empresas\<empresa>\config.ini`.

Faz quatro coisas, e só estas:
1. **Arranca o sync como processo filho**, com `PYTHONPATH=versoes\<atual>`. O filho pode estar partido (erro de sintaxe, import em falta) sem derrubar o launcher.
2. **Recuo no reinício:** se o filho morre, espera 10 s, 30 s, 1 min, 5 min… (acaba a tempestade de reinícios de minuto a minuto).
3. **Saúde e reversão:** depois de mudar de versão, se o filho não escrever um ciclo bom em `saude.json` em 10 min, ou morrer 3 vezes seguidas, volta à versão anterior, marca a nova como `FALHOU` (não se oferece outra vez) e **relata à Lonntra** (com o traceback). Como o launcher não depende do sync, avisa mesmo quando a versão nova nem arranca.
4. **Segue o `atual.txt`:** se o ficheiro mudar, termina o filho e arranca-o na versão nova. É assim que uma atualização é «imediata».

Estrutura no cliente (versões **partilhadas**, ponteiro **por empresa**, para atualizar uma empresa de cada vez):
```
C:\primavera-sync\
  launcher.py   launcher.py.anterior
  versoes\1.1.0\primavera_sync\...   versoes\1.2.0\primavera_sync\...      (guardam-se as últimas 3)
  empresas\<empresa>\  config.ini  config.remoto.ini  estado.db  primavera-sync.log
                       atual.txt  anterior.txt  saude.json
  (instalação antiga: config.ini na raiz, mantém-se como está)
```

### B.3 Publicar (no teu Mac): `scripts/publicar.sh 1.2.0`

1. Corre os testes; se falharem, pára.
2. Gera `primavera_sync-1.2.0.zip` **reprodutível** (ficheiros ordenados, datas fixas).
3. Assina o manifesto `{versao, serie, sha256, requer, publicado_em}` com **Ed25519** (`scripts/assinar.py`, chave privada no Keychain do Mac). `serie` é um inteiro que só sobe.
4. `scp` para a Lonntra e `ssh lonntra php yii integracao/versao-registar …` (grava a versão no canal `teste`).
5. Etiqueta o git (`sync-v1.2.0`) e escreve as notas da versão (vêm dos commits).

Promover é outro passo, de propósito: `php yii integracao/versao-promover 1.2.0` passa-a ao canal `estavel`.

### B.4 Lonntra distribui (nada de empurrar)

Tabelas novas: `integracao_versao` (versão, serie, sha256, assinatura, ficheiro, notas, canal), e em `integracao_chave` as colunas `CANAL` (`estavel` por omissão, `teste` para o piloto), `VERSAO_ALVO` (opcional: fixa uma versão) e `VERSAO_ATUAL` (o que o sync diz que corre).

A resposta ao **sinal de vida** (que já existe, de minuto a minuto) passa a trazer `"atualizar": {versao, serie, sha256, assinatura, url}` quando `alvo ≠ atual`. Zero pedidos extra. O download é `GET /integracao/primavera/pacote/<versao>` com a mesma chave Bearer.

Regras do alvo: se há `VERSAO_ALVO`, é essa; senão a mais recente do canal da empresa. **Reverter = pôr `VERSAO_ALVO` numa versão antiga**; se ela ainda estiver na pasta `versoes\`, a troca é local e instantânea (não precisa de descarregar nem de assinatura nova).

### B.5 O sync atualiza-se (no cliente)

1. Vê `atualizar` na resposta do sinal. Se tiver o `lock` de outra empresa a descarregar a mesma versão, espera e usa o resultado (a pasta é partilhada).
2. Verifica: espaço em disco, `requer` (Python, pyodbc, cryptography), e `serie` **maior** que a atual (ou versão já presente localmente).
3. Descarrega para `versoes\<v>.tmp`; confere o **SHA-256** e a **assinatura** com a chave pública fixa no launcher.
4. Extrai; `compileall`; **teste de importação** num subprocesso com o caminho novo; `--verificar` com o `config.ini` real (liga ao SQL e à Lonntra, **não envia nada**).
5. Só então: `rename` para `versoes\<v>`, `anterior.txt ← atual.txt`, `atual.txt ← <v>` (escrita atómica). O launcher vê a mudança e reinicia o filho.
6. Relata `atualizacao/ok` (ou `/erro` com o motivo) à Lonntra. Se a verificação falhar, **nada muda** e a versão fica marcada como recusada neste cliente.

### B.6 Configuração remota (lista branca)

A Lonntra pode mandar por empresa, e só estas chaves: `tipos_artigo_servico`, `intervalo`, `revisao_dias`, `catalogo`, `movimentos_stock`, `historico_horario` e consultas `[consultas]`. O sync valida o tipo (inteiro, sim/não…), grava `config.remoto.ini`, e esse ficheiro **sobrepõe-se** ao `config.ini`. **Nunca** `ligacao`, `chave` ou `url`. Se o valor for inválido, recusa e relata. Serve para «o Primavera mudou uma tabela»: acrescentas a consulta de casa, sem atualizar o código.

### B.7 Comandos remotos (lista branca)

Tabela `integracao_comando` (empresa, comando, argumentos, estado, resultado). O sync vê os pendentes na resposta do sinal, executa **uma vez** (guarda os ids executados) e devolve o resultado no sinal seguinte (limite 20 KB, sem palavras-passe).

| Comando | Para quê |
|---|---|
| `esquema` | o que o sync percebeu da base (hoje só com acesso local) |
| `verificar` | testa a ligação e as consultas |
| `log [n]` | últimas linhas do log |
| `repetir_erros` | tentar já os documentos que estão à espera |
| `completo` | rever todo o histórico |
| `teste N` | enviar N de cada tipo |
| `reiniciar` | reiniciar o filho |

### B.8 Segurança (o sync corre como SYSTEM no servidor do cliente)

| Ameaça | Defesa |
|---|---|
| A Lonntra é comprometida e manda código | O código só corre se estiver **assinado** com a tua chave; a Lonntra não a tem |
| Alguém interceta o download | HTTPS + SHA-256 + assinatura |
| Reenviarem uma versão antiga com falha | `serie` tem de subir; só se recua para versões **já presentes** no disco |
| Comando ou config maliciosos | Lista branca, tipos validados, nunca texto a executar nem a meter em SQL |
| Perda da chave privada | Cópia offline; o launcher aceita **2 chaves públicas** (atual e seguinte), para rodar a chave sem reinstalar |
| Atualização que parte tudo | Teste de importação + `--verificar` antes da troca + reversão automática depois |
| Atualizar o próprio launcher | Pode vir no pacote, mas só substitui `launcher.py` depois de passar `launcher.py --autoteste`; guarda `launcher.py.anterior`; aplica-se no próximo arranque (todas as segundas) |

Cada atualização, comando e configuração fica no **registo da Lonntra** (quem, quando, resultado).

### B.9 Alternativas que descartei

| Alternativa | Porquê não |
|---|---|
| `git pull` no servidor do cliente | Precisa de git e credenciais lá, não assina, e um repositório privado fica ligado ao servidor |
| Releases do GitHub como origem | Precisa de token no cliente e da disponibilidade do GitHub; a Lonntra já autentica cada empresa |
| Executável (PyInstaller) com auto-atualização | Antivírus dão falsos positivos, ficheiro grande, mais frágil que trocar uma pasta |
| Pacote `pip` de um índice privado | Mais infraestrutura sem ganho; não traz verificação de versão por empresa |
| Windows Service (NSSM) | O launcher já dá o recuo e a reversão; o NSSM só reinicia |
| TeamViewer/AnyDesk de rotina | Fica como **plano B** quando o launcher estiver partido; não escala nem deixa registo |

### B.10 Ordem de construção (cada passo entrega valor sozinho)

| Passo | O quê | Valor |
|---|---|---|
| **U1** ✅ *feito (01/11)* | `launcher.py` + estrutura `versoes/` + recuo + `saude.json` + reversão + instalador que o põe (instalação manual, uma última vez) | acaba a tempestade de reinícios; versões que falham revertem e avisam |
| **U2** ✅ *feito (02/11, por pôr em produção)* | `scripts/publicar.py` + assinatura Ed25519 + `integracao_versao` + endpoint do pacote + `atualizador.py` no cliente (canal `teste`, piloto = Auto Style Custom) | **atualizar de casa** |
| **U3** | Reversão automática + `VERSAO_ALVO` + `launcher.py --autoteste` | segurança |
| **U4** ✅ *feito (02/11, por pôr em produção)* | Config remota + comandos remotos (lista branca; consultas SQL só por versão assinada) | diagnosticar e corrigir sem entrar no servidor |
| **U5** | Painel de Super Admin (versões por empresa, publicar/promover/fixar, comandos) | operar sem linha de comandos |

Cada passo tem testes automáticos: pacote adulterado (SHA e assinatura) recusado, versão partida que reverte, corrida entre duas empresas a descarregar a mesma versão, `serie` a descer recusada, comando executado só uma vez, config fora da lista recusada.

---

## Parte C. Ordem de trabalho

| Fase | O quê | Resolve |
|---|---|---|
| **P0 (já)** | Ver porque as duas empresas pararam (tarefa, reinício do servidor, login SQL). Commit das correções do instalador por commitar. | sync parado |
| **P1** | Correções de robustez do sync (lista abaixo) | crashes e ruído |
| **P2** | A1 + A2 + A3 (eventos, tabela, avisos para ti) | saberes dos problemas |
| **P3** | U1 + U2 + U3 (launcher, publicar, atualizar, reverter; ver B.10) | atualizar de casa |
| **P4** | U4 (config e comandos remotos) | diagnosticar e corrigir de casa |
| **P5** | A4 + A5 + U5 (menos ruído, painéis); recebimentos do Primavera | acabamento |

### Lista de P1 (correções de robustez)
1. `main`: o sinal de vida apanha `Exception` (hoje só `ErroLigacao`).
2. `_enviar_documentos`: apanha `Exception` por documento e segue (hoje um dado estranho pára o ciclo para sempre).
3. Existências: guardar as recusadas e não reenviar; não enviar as de serviços.
4. Instalador: **não rodar a palavra-passe** de `lonntra_leitura` se já existe e há `config.ini` a usá-la; senão actualizar todos os `config.ini`.
5. Instalador: avisar se `tipos_artigo_servico` ficar vazio.
6. Relatar ao arranque (config inválida etc.) antes de sair.
7. Backoff no reinício da tarefa (hoje reinicia de minuto a minuto, 999 vezes).
8. Copiar o `estado.db` todos os dias (perdê-lo faz reenviar o histórico).

---

## Parte D. Riscos e perguntas em aberto

- **Chave privada Ed25519:** no Keychain do Mac, com cópia offline. Se se perder, o launcher só aceita assinaturas dela: é preciso reinstalar à mão em cada cliente. Por isso o launcher aceita duas chaves públicas.
- **Biblioteca de assinaturas:** a opção simples é o pacote `cryptography` (o instalador já faz `pip install pyodbc`, passa a fazer os dois). Alternativa sem dependência: uma implementação pura de Ed25519 verificada com os vetores de teste do RFC 8032. Recomendo `cryptography`.
- **O primeiro launcher:** tem de ser instalado à mão em cada servidor, uma última vez (num dia em que o servidor esteja ligado). Daí para a frente é remoto.
- **Windows Update/reinício:** a tarefa arranca com o Windows (`AtStartup` + 2 min), por isso sobrevive; o aviso de «parada» (A3b) já distingue fecho de falha.
- **Python e pyodbc** não se atualizam remotamente (o pacote declara o que exige em `requer` e recusa se não houver). É raro e fica manual.
- ❓ Quantas empresas e servidores vão ter o sync? Com mais de 3, o canal `teste` e o painel (U5) passam de opcionais a necessários.
- ❓ Telegram ou WhatsApp para os alertas imediatos? (Email chega para começar.)
- ❓ A Auto Style Custom serve de piloto (canal `teste`)? É a que tem menos risco se uma versão falhar?
