# Instalar e manter o primavera-sync (Auto Style e outros clientes)

O primavera-sync corre no servidor da Auto Style, lê o Primavera (só leitura) e envia para a Lonntra (https://app.lonntra.cv):

- os **documentos de venda** (faturas, faturas-recibo, vendas a dinheiro, notas de crédito);
- o **catálogo de artigos** (descrição, família, IVA, preço, custo, código de barras, anulados);
- o **stock**: armazéns, quantidade de cada artigo em cada armazém e os movimentos de entrada e saída.

Na Lonntra tudo fica só para consulta: o stock e os documentos continuam a ser geridos no Primavera.

A cada minuto, o sync dá **sinal de vida** à Lonntra. Se parar mais de 2 horas, ou se tiver registos com erro, a Lonntra **avisa no sino** (e por email, se estiver ligado), e avisa outra vez quando voltar a funcionar.

---

## Instalação rápida (recomendada)

O `instalar.bat` faz sozinho tudo o que a Parte C descreve à mão.

**No Mac, antes de ir** (uma vez por cliente):

1. Parte A: Lonntra atualizada, empresa criada, chave gerada, vigia ligado.
2. Copiar o `cliente.example.ini` para `clientes/<cliente>/cliente.ini` e preencher o nome, o NIF, o URL e a chave. A pasta `clientes/` não vai para o Git. A da Auto Style já está em `clientes/auto-style/cliente.ini`.

**No servidor do cliente** (pelo AnyDesk, com um utilizador administrador):

1. Descarregar o ZIP do primavera-sync (GitHub, ramo `main`) e extrair, por exemplo para o Ambiente de Trabalho.
2. Copiar o `cliente.ini` do cliente para dentro dessa pasta, ao lado do `instalar.bat` (pela transferência de ficheiros do AnyDesk).
3. Fazer duplo clique em **`instalar.bat`** e aceitar o pedido de administrador.

O instalador, por esta ordem:

| Passo | O que faz | O que pode perguntar |
|---|---|---|
| Ficheiros | copia o sync para `C:\primavera-sync` e fecha a pasta só para administradores | — |
| Python | instala o Python 3.12 (se faltar) e o pyodbc; confirma o driver ODBC | — |
| Lonntra | testa a chave do `cliente.ini`. **Pode haver várias empresas**: `chave = k1, k2` no `cliente.ini`, ou digitar as chaves separadas por vírgulas | a(s) chave(s), se faltarem ou estiverem erradas |
| SQL Server | encontra a instância e entra com o utilizador do Windows | o login de administrador (ex.: `sa`), só se o Windows não entrar; não fica guardado |
| Empresa | para cada chave, lista as bases `PRI…` com o nome da empresa (propõe a de nome igual ao da empresa na Lonntra e não repete bases já usadas) | qual é, se houver várias |
| Acesso | cria o login `lonntra_leitura` (só leitura, palavra-passe gerada) ou, se o SQL Server só aceitar Windows, dá leitura à conta SYSTEM | — |
| Códigos | mostra os tipos de documento e propõe FT/FR/TV/NC/ND; propõe os métodos de pagamento; mostra os tipos de artigo com exemplos | confirmar os tipos (Enter); quais tipos de artigo são serviços |
| Stock | experimenta as consultas do catálogo e do stock. Se a base for de outra versão, **varre a base** (`descobrir.py`) à procura das tabelas de armazéns, existências e movimentos e propõe as consultas; o que não houver fica desligado | confirmar a tabela proposta |
| Histórico | — | a que horas começa (por omissão **18h**; corre das 18h às 7h) ou «ja» para já |
| Verificação | escreve o `config.ini` e corre o `--verificar` | — |
| Teste de envio | envia **10 documentos (os mais recentes), 10 artigos, 10 armazéns, 10 movimentos e 10 existências** e lista o que foi, para conferires na Lonntra antes de sair. O resto do histórico fica para o horário escolhido. Pode repetir-se depois com `python -m primavera_sync.sincronizador config.ini --teste [N]` | confirmar (por omissão sim) |
| Arranque | cria uma tarefa por empresa (com o Windows, como SYSTEM, reinicia se parar), arranca-as e espera pelo primeiro ciclo de cada uma | — |

No fim mostra o resultado, que também fica em `C:\primavera-sync\relatorio-instalacao.txt`.

**Mais do que uma empresa no mesmo servidor.** Cada empresa tem o seu `config.ini`, `estado.db`, log e tarefa em `C:\primavera-sync\empresas\<empresa>\` (tarefa `PrimaveraLonntra-<empresa>`); a lista fica em `instalacao.json`. Uma instalação antiga (com `config.ini` na raiz e a tarefa `PrimaveraLonntra`) mantém-se como está, para não reenviar o histórico. O `estado.bat` mostra todas as empresas e o `desinstalar.bat` remove todas as tarefas. As consultas de stock podem também ser descobertas à mão: `python -m primavera_sync.descobrir <config.ini>`. Se algum passo falhar, diz porquê; corrige-se e volta a correr-se o `instalar.bat` (é seguro repetir).

Depois, na pasta `C:\primavera-sync`:

- **`estado.bat`**: estado da tarefa, verificação, o que foi enviado, os erros e o fim do log;
- **`desinstalar.bat`**: para e remove o arranque automático (os ficheiros ficam).

A Parte C abaixo é a instalação à mão, para quando o instalador não puder ser usado ou para perceber o que ele faz.

---

## Parte A — Antes de ir ao servidor (Lonntra de produção)

Tudo a partir do Mac.

### A1. Pôr a Lonntra de produção atualizada

1. Ver o que vai ser aplicado. Não altera nada:
   ```bash
   ssh lonntra "cd /var/www/lonntra && git log --oneline -1 && sudo -u www-data php yii migrate/new --interactive=0"
   ```
   Têm de aparecer as migrações da integração: `m261023_100000_integracao_primavera` e `m261027_100000_primavera_stock`. Se aparecer também a `m261020_100000_documentos_comunicados_imutaveis`, confirmar antes que o POS e a API Node não alteram documentos depois de comunicados (ADR-057).
2. Atualizar. Faz a cópia de segurança, o pull, as migrações e recarrega:
   ```bash
   ssh lonntra lonntra-atualizar
   ```
3. Confirmar que a rota existe. A resposta tem de ser «Chave de integração inválida ou inativa.»; um 404 quer dizer que a atualização não foi aplicada.
   ```bash
   curl -s https://app.lonntra.cv/integracao/primavera/estado
   ```

### A2. Criar a empresa e a chave

1. Criar a Auto Style (NIF 283048808), com o repositório do e-Fatura `1` (Principal) e acesso de Administrador para o utilizador `auto.style`. O utilizador tem de existir e estar ativo.
   ```bash
   scp scripts/criar_empresa_lonntra.php lonntra:/tmp/criar_empresa_lonntra.php
   ```
   ```bash
   ssh lonntra "cd /var/www/lonntra && sudo -u www-data php /tmp/criar_empresa_lonntra.php /var/www/lonntra 'Auto Style' 283048808 auto.style 1"
   ```
   ```bash
   ssh lonntra "rm /tmp/criar_empresa_lonntra.php"
   ```
2. Gerar a chave e **guardá-la num sítio seguro** (gestor de palavras-passe). Só se mostra desta vez, e gerar outra desliga a anterior.
   ```bash
   ssh lonntra "cd /var/www/lonntra && sudo -u www-data php yii integracao/chave 283048808"
   ```
3. Dar a ti próprio acesso à Auto Style na Lonntra (Acessos), para veres os avisos do sino.

### A3. Ligar o vigia (uma vez)

O vigia corre de 15 em 15 minutos e avisa quando a ligação para, volta ou tem erros. Acrescentar ao cron do servidor:

```bash
ssh lonntra "echo '*/15 * * * * www-data cd /var/www/lonntra && php yii integracao/vigiar >> /var/log/lonntra/integracao.log 2>&1' > /etc/cron.d/lonntra-integracao && chmod 644 /etc/cron.d/lonntra-integracao"
```

### A4. Confirmar o plano

Os documentos importados contam para o limite de documentos por mês do plano da Auto Style. O histórico todo pode passá-lo: confirmar o plano antes da primeira carga (Comercial → Empresas).

### A5. Levar o que é preciso

- O AnyDesk do servidor da Auto Style (Parte B).
- A chave do passo A2.
- O ZIP do primavera-sync: no GitHub `DsK-David/primavera-sync`, ramo `main`, «Code → Download ZIP».

---

## Parte B — Pedir o acesso à Auto Style

Pedir à Auto Style:

1. Que instalem o **AnyDesk** (anydesk.com) no servidor onde está o Primavera e enviem o **número de 9 dígitos**.
2. **Acesso sem supervisão** (Definições → Segurança → palavra-passe de acesso), para não ser preciso alguém aceitar a ligação de cada vez. Fica também para a manutenção.
3. Um **utilizador do Windows com permissão de administrador** (instalar o Python e o serviço).
4. **Acesso ao SQL Server**: o SQL Server Management Studio com um utilizador que possa criar logins, ou o contacto do técnico que instalou o Primavera.
5. A **autorização por escrito** (um email chega) para aceder ao servidor e ler os dados de faturação e stock.

Mensagem pronta a enviar:

> Bom dia. Para ligar o Primavera à Lonntra preciso de acesso remoto ao servidor onde está o Primavera. Podem instalar o AnyDesk nesse servidor, ativar o acesso sem supervisão com uma palavra-passe e enviar-me o número do AnyDesk? Preciso também de um utilizador do Windows com permissões de administrador e de acesso ao SQL Server (ou do contacto do técnico do Primavera) para criar um utilizador só de leitura. A ligação só lê os dados de faturação e de stock, não altera nada no Primavera. Obrigado.

---

## Parte C — Instalar no servidor

### C1. Conferir o servidor

- Espaço livre em disco: pelo menos 1 GB.
- Abrir https://app.lonntra.cv no browser do servidor. Se não abrir, o servidor não tem saída para a internet (ver «Problemas»).
- Ver a hora do Windows: tem de estar certa (os certificados HTTPS dependem dela).

### C2. Descobrir a instância e a base da empresa

No **SQL Server Management Studio**:

```sql
SELECT @@SERVERNAME AS instancia, name AS base FROM sys.databases WHERE name LIKE 'PRI%';
```

A base da Auto Style é uma das `PRI…` (ex.: `PRIAUTOSTYLE`). Se houver várias, quem usa o Primavera sabe qual é. Daqui em diante, trocar `PRIAUTOSTYLE` pelo nome certo.

### C3. Confirmar as tabelas e anotar os códigos

Na base da empresa (`USE PRIAUTOSTYLE;`):

```sql
-- documentos
SELECT TOP 5 Id, TipoDoc, Serie, NumDoc, Data, DataVenc, Entidade, ModoPag, Moeda, TotalDocumento FROM CabecDoc WHERE TipoEntidade = 'C' ORDER BY Data DESC;
SELECT TOP 5 Artigo, Quantidade, PrecoLiquido, TaxaIva, IdLinhaOrigemCopia FROM LinhasDoc;
SELECT TipoDoc, COUNT(*) AS n FROM CabecDoc WHERE TipoEntidade = 'C' GROUP BY TipoDoc;
SELECT ModoPag, COUNT(*) AS n FROM CabecDoc WHERE TipoEntidade = 'C' GROUP BY ModoPag;

-- catálogo e stock
SELECT TOP 5 Artigo, Descricao, TipoArtigo, Iva, CodBarras, Familia, ArtigoAnulado, PCMedio FROM Artigo;
SELECT TOP 5 Artigo, Moeda, PVP1, PVP1IvaIncluido FROM ArtigoMoeda;
SELECT TOP 5 Familia, Descricao FROM Familias;
SELECT Armazem, Descricao FROM Armazens;
SELECT TOP 5 Artigo, Armazem, StkActual FROM ArtigoArmazem;
SELECT TipoArtigo, COUNT(*) AS n FROM Artigo GROUP BY TipoArtigo;
SELECT name FROM sys.tables WHERE name LIKE '%Mov%' OR name LIKE '%STK%' OR name LIKE 'INV%' ORDER BY name;
SELECT TOP 5 * FROM INV_Movimentos ORDER BY Data DESC;
```

Anotar:

| O quê | Para onde vai no `config.ini` |
|---|---|
| Códigos de documento (`TipoDoc`) de venda: FA, FR, VD, NC… | `[tipos_documento]` |
| Modos de pagamento (`ModoPag`) | `[modos_pagamento]` |
| `TipoArtigo` dos serviços (mão de obra…) | `tipos_artigo_servico` |
| A moeda em `ArtigoMoeda` (normalmente `CVE`) | `moeda` |
| Se `INV_Movimentos` não existir ou tiver outras colunas: a tabela dos movimentos de stock (penúltima consulta) | `[consultas] movimentos` |

Se alguma consulta der erro de coluna ou tabela, há duas saídas: escrever a consulta certa em `[consultas]` (só para o catálogo e o stock; ver os comentários no `config.ini`) ou ajustar `primavera_sync/evolution.py`.

### C4. Criar o login só de leitura

Escolher uma palavra-passe forte e guardá-la com a chave:

```sql
CREATE LOGIN lonntra_leitura WITH PASSWORD = 'escolher-uma-forte', CHECK_POLICY = ON;
USE PRIAUTOSTYLE;
CREATE USER lonntra_leitura FOR LOGIN lonntra_leitura;
ALTER ROLE db_datareader ADD MEMBER lonntra_leitura;
```

O SQL Server tem de aceitar logins do SQL Server (autenticação mista). Se o login falhar com «Login failed», ver «Problemas».

### C5. Instalar o Python e o pyodbc

1. Descarregar o Python 3.12 de python.org. Na instalação, escolher **«Customize installation»**, marcar **«Install Python for all users»** e **«Add Python to environment variables»**. Para todos os utilizadores, porque o serviço corre como sistema e não vê um Python instalado só para um utilizador.
2. Abrir o **PowerShell como administrador**:
   ```
   py -m pip install pyodbc
   where.exe python
   Get-OdbcDriver | Select Name
   ```
   - O `where.exe python` dá o caminho do Python (ex.: `C:\Program Files\Python312\python.exe`). Anotar, porque é preciso no C9.
   - Na lista de drivers tem de aparecer «ODBC Driver 17 for SQL Server» ou «18». Se não aparecer, instalar o «ODBC Driver 18 for SQL Server» da Microsoft.

### C6. Copiar o sync

Extrair o ZIP e deixar só isto:

```
C:\primavera-sync\
├── primavera_sync\        (__init__.py, evolution.py, lonntra.py, sincronizador.py)
└── config.ini             (cópia do config.example.ini)
```

Proteger a pasta, porque o `config.ini` tem a chave e a palavra-passe. No PowerShell como administrador:

```
icacls C:\primavera-sync /inheritance:r /grant:r "Administradores:(OI)(CI)F" "SYSTEM:(OI)(CI)F"
```

(Num Windows em inglês: `Administrators` em vez de `Administradores`.)

### C7. Preencher o `C:\primavera-sync\config.ini`

- `[primavera] ligacao`: a instância e a base (C2) e o login (C4):
  ```
  ligacao = DRIVER={ODBC Driver 17 for SQL Server};SERVER=NOME\INSTANCIA;DATABASE=PRIAUTOSTYLE;UID=lonntra_leitura;PWD=...
  ```
  Com o «ODBC Driver 18»: `DRIVER={ODBC Driver 18 for SQL Server}` e `;TrustServerCertificate=yes` no fim.
- `[lonntra] url = https://app.lonntra.cv`
- `[lonntra] chave =` a chave do A2.
- `[tipos_documento]`: cada código do C3 aponta para FT, FR, TV, NC ou ND (ex.: `FA = FT`, `VD = TV`). Só os tipos listados são enviados; FR e TV vão pagos no ato.
- `[modos_pagamento]`: cada código do C3 com o código da Lonntra (10 numerário, 20 cheque, 30 transferência, 54 cartão de crédito, 55 cartão de débito). O que não estiver na lista fica numerário.
- `tipos_artigo_servico`, `moeda` e `[consultas]`: do C3.
- **Para o primeiro teste:** `data_inicio` com uma data de há poucos dias (ex.: `data_inicio = 2026-10-20`).

### C8. Verificar e testar

1. Verificar. Não envia nada:
   ```
   cd C:\primavera-sync
   python -m primavera_sync.sincronizador config.ini --verificar
   ```
   Todas as linhas têm de dizer `OK`. Cada `ERRO` diz o que falta (ligação, consulta ou chave). Corrigir e voltar a verificar.
2. Primeiro envio, com poucos documentos:
   ```
   python -m primavera_sync.sincronizador config.ini --uma-vez
   python -m primavera_sync.sincronizador config.ini --estado
   ```
3. Entrar em https://app.lonntra.cv com o `auto.style` e conferir:
   - os documentos: números, datas, valores e clientes, com o estado e-Fatura «Outro software»;
   - os artigos: descrições, famílias e preços (sem IVA);
   - o stock: a quantidade de alguns artigos em cada armazém tem de ser igual à do Primavera, e o histórico de cada um mostra as entradas e saídas com o documento.

   O catálogo e os movimentos de stock vão todos logo na primeira corrida (não dependem do `data_inicio`).

**Se algo estiver errado, parar aqui.** Os documentos que já entraram na Lonntra não se apagam, por isso é melhor corrigir com poucos.

### C9. Instalar o serviço

Descarregar o NSSM (nssm.cc) e copiar o `nssm.exe` (pasta `win64`) para `C:\primavera-sync\`. No PowerShell como administrador, trocando o caminho do Python pelo do C5:

```
cd C:\primavera-sync
mkdir logs
.\nssm.exe install PrimaveraLonntra "C:\Program Files\Python312\python.exe" "-m primavera_sync.sincronizador C:\primavera-sync\config.ini"
.\nssm.exe set PrimaveraLonntra AppDirectory C:\primavera-sync
.\nssm.exe set PrimaveraLonntra DisplayName "Primavera → Lonntra"
.\nssm.exe set PrimaveraLonntra Description "Envia documentos, artigos e stock do Primavera para a Lonntra (só leitura)."
.\nssm.exe set PrimaveraLonntra Start SERVICE_DELAYED_AUTO_START
.\nssm.exe set PrimaveraLonntra AppExit Default Restart
.\nssm.exe set PrimaveraLonntra AppRestartDelay 30000
.\nssm.exe set PrimaveraLonntra AppStdout C:\primavera-sync\logs\servico.log
.\nssm.exe set PrimaveraLonntra AppStderr C:\primavera-sync\logs\servico.log
.\nssm.exe set PrimaveraLonntra AppRotateFiles 1
.\nssm.exe set PrimaveraLonntra AppRotateBytes 5000000
```

- **Arranque atrasado:** o serviço espera que o SQL Server arranque primeiro depois de reiniciar o Windows.
- **Reinício automático:** se o programa parar, o Windows volta a arrancá-lo 30 segundos depois.

### C10. Enviar o histórico todo (de preferência fora do horário)

1. No `config.ini`, apagar o valor de `data_inicio` (fica `data_inicio =`).
2. Fazer a primeira carga completa à mão, à noite ou ao fim de semana, porque lê anos de documentos:
   ```
   python -m primavera_sync.sincronizador config.ini --uma-vez --completo
   ```
   Pode demorar. Pode parar-se (Ctrl+C) e voltar a correr: não duplica nada.
3. Arrancar o serviço:
   ```
   .\nssm.exe start PrimaveraLonntra
   ```

### C11. Confirmar que ficou a funcionar

- `Get-Service PrimaveraLonntra` diz `Running`.
- Em `C:\primavera-sync\primavera-sync.log` aparece uma linha por minuto («Nada de novo no Primavera» ou o que foi enviado).
- No Mac:
  ```bash
  ssh lonntra "cd /var/www/lonntra && sudo -u www-data php yii integracao/estado"
  ```
  Tem de mostrar a Auto Style com o «último contacto» de há menos de 2 minutos, a versão e a máquina.
- Reiniciar o servidor uma vez (se a Auto Style deixar) e confirmar que o serviço volta sozinho.

### C12. Antes de sair

- [ ] `--verificar` só com `OK`.
- [ ] Documentos, artigos e stock conferidos na Lonntra.
- [ ] Serviço `Running`, com arranque atrasado e reinício automático.
- [ ] `integracao/estado` com contacto recente.
- [ ] Chave, palavra-passe do `lonntra_leitura` e número do AnyDesk guardados no gestor de palavras-passe.
- [ ] Pasta `C:\primavera-sync` protegida (C6).
- [ ] Pedir à Auto Style que **não desinstale o AnyDesk** e avise antes de trocar o servidor ou atualizar o Primavera.

---

## Parte D — Manutenção

### Como saber se está tudo bem

- **Avisos no sino da Lonntra** (empresa Auto Style), em três casos:
  - «A ligação ao Primavera parou»: mais de 2 horas sem sinal;
  - «A ligação ao Primavera voltou a funcionar»;
  - «Ligação ao Primavera: N registos com erro»: no máximo um por dia.
- **Do Mac, a qualquer momento**:
  ```bash
  ssh lonntra "cd /var/www/lonntra && sudo -u www-data php yii integracao/estado"
  ```
  Mostra o último contacto, a versão, a máquina, quantos documentos, artigos e movimentos foram enviados, quantos erros há e o último erro.
- **Histórico do vigia**: `ssh lonntra tail -50 /var/log/lonntra/integracao.log`

### No servidor (pelo AnyDesk)

| Para quê | Comando (em `C:\primavera-sync`) |
|---|---|
| Testar tudo, sem enviar | `python -m primavera_sync.sincronizador config.ini --verificar` |
| Ver o que foi enviado e os erros | `python -m primavera_sync.sincronizador config.ini --estado` |
| Ver o log | `Get-Content primavera-sync.log -Tail 50` |
| Estado | `estado.bat` (instalação rápida) ou `Get-Service PrimaveraLonntra` (à mão, com o NSSM) |
| Parar / arrancar (instalação rápida) | `Stop-ScheduledTask PrimaveraLonntra` / `Start-ScheduledTask PrimaveraLonntra` |
| Parar / arrancar (à mão, com o NSSM) | `.\nssm.exe stop PrimaveraLonntra` / `start` / `restart` |
| Voltar a tentar os que esgotaram as 5 tentativas | parar o serviço → `python -m primavera_sync.sincronizador config.ini --repetir-erros --uma-vez` → arrancar o serviço |
| Rever tudo (ex.: depois de corrigir uma consulta) | parar o serviço → `python -m primavera_sync.sincronizador config.ini --uma-vez --completo` → arrancar |

### Atualizar o primavera-sync

1. Descarregar o ZIP novo do GitHub (ramo `main`).
2. `.\nssm.exe stop PrimaveraLonntra`
3. Substituir **só a pasta `primavera_sync\`**. O `config.ini`, o `estado.db` e os logs ficam.
4. Ver no `config.example.ini` novo se apareceram opções; acrescentá-las ao `config.ini`.
5. `python -m primavera_sync.sincronizador config.ini --verificar`
6. `.\nssm.exe start PrimaveraLonntra`, e confirmar a versão nova em `integracao/estado`.

### Trocar a chave (perdida, ou quem tinha acesso saiu)

1. `ssh lonntra "cd /var/www/lonntra && sudo -u www-data php yii integracao/chave 283048808"`. A anterior deixa de funcionar logo.
2. No servidor: pôr a chave nova no `config.ini` e `.\nssm.exe restart PrimaveraLonntra`.

### O que é cada ficheiro

| Ficheiro | O que é | Se se perder |
|---|---|---|
| `config.ini` | ligação, chave, mapas | refazer a partir do `config.example.ini` |
| `estado.db` | o que já foi enviado e os erros | nada se perde: a Lonntra não duplica; a próxima corrida volta a enviar e a Lonntra ignora o que já tem (demora mais uma vez) |
| `primavera-sync.log` (+ `.1` a `.5`) | o que o sync fez, 5 MB cada | só histórico |
| `logs\servico.log` | o que o serviço escreveu na consola (arranques, erros fatais) | só histórico |

---

## Parte E — Problemas e soluções

| O que se vê | Causa provável | O que fazer |
|---|---|---|
| Aviso «A ligação parou» | servidor desligado, serviço parado, sem internet | AnyDesk → `Get-Service PrimaveraLonntra`; se parado, `.\nssm.exe start PrimaveraLonntra`; ver `logs\servico.log` e o fim do `primavera-sync.log` |
| `--verificar`: `ERRO chave e endereço: 401` | chave errada ou trocada | gerar chave nova (Parte D) |
| `--verificar`: erro de certificado / SSL | hora do Windows errada, ou proxy da empresa | acertar a hora; se houver proxy, falar com a informática da Auto Style |
| `--verificar`: `Login failed for user 'lonntra_leitura'` | palavra-passe errada ou SQL Server só com autenticação Windows | confirmar a palavra-passe; no SSMS: propriedades do servidor → Segurança → «SQL Server and Windows Authentication mode» e reiniciar o serviço do SQL Server (combinar com a Auto Style) |
| `--verificar`: `Data source name not found` / driver | driver ODBC com outro nome | `Get-OdbcDriver` e pôr o nome exato em `DRIVER={…}` |
| `--verificar`: `Invalid object name 'INV_Movimentos'` (ou outra tabela) | versão do Primavera com outras tabelas | escrever a consulta certa em `[consultas]` (C3); os documentos continuam a funcionar entretanto |
| Aviso «N registos com erro», `--estado` com «Não há taxa de IVA de X%» | taxa de IVA do Primavera que não existe na Lonntra | criar a taxa na Lonntra (Parametrização → Taxas de IVA) e depois `--repetir-erros` |
| `--estado`: «o cliente X não existe na tabela Clientes» | documento de uma entidade que não é cliente | verificar no Primavera; se for normal, ignorar |
| `--estado`: «moeda EUR (só se envia CVE)» | documento noutra moeda | não é enviado de propósito |
| Quantidade de stock diferente do Primavera | consulta de existências que não serve nesta versão (lotes, localizações) | comparar `SELECT Artigo, Armazem, SUM(StkActual) FROM ArtigoArmazem WHERE Artigo='…' GROUP BY Artigo, Armazem` com a Lonntra; ajustar `[consultas] existencias` |
| Primavera lento durante a carga | primeira carga completa a ler anos de dados | fazer a carga completa fora do horário (C10); o funcionamento normal é leve |
| Serviço arranca e para logo | `config.ini` com erro, ou Python sem o pyodbc para todos os utilizadores | ver `logs\servico.log`; correr `--verificar` à mão; reinstalar o pyodbc como administrador (`py -m pip install pyodbc`) |
| A Lonntra recusa: «limite de documentos do plano» | plano da Auto Style | mudar o plano (Comercial) e `--repetir-erros` |

---

## O que ainda não está coberto

- **Recibos**: as faturas a crédito ficam «por pagar» na Lonntra até haver a rota de recibos (fase 2 do ADR-061).
- **Lotes e localizações** dentro do armazém: somam-se por armazém. **Unidades de medida**: todos os artigos ficam em «Unidade».
- O tipo de cliente (coletivo/singular) sai do nome («Lda», «S.A.»…). Confirmar com a Auto Style.

## O sync adapta-se à estrutura da base

O sync não depende de nomes fixos de tabelas e colunas: ao arrancar, lê a estrutura da base do Primavera e monta as consultas (`primavera_sync/esquema.py`). Para cada dado há uma lista de nomes possíveis, procurados sem ligar a maiúsculas, acentos ou «_», e com tolerância a pequenas diferenças de escrita.

- Dado **opcional** em falta (vencimento, morada, custo, preço…): vai vazio e fica registado; o sync continua.
- Dado **indispensável** em falta (número, data, total do documento; artigo, quantidade, preço líquido e IVA da linha): o sync não envia documentos errados; para e diz qual coluna falta (aparece no log, no `--verificar` e no aviso da Lonntra).
- Se a Primavera for atualizada e uma consulta falhar por tabela ou coluna inexistente, o sync **lê a estrutura de novo e repete** a consulta, sem ninguém mexer no código.
- Stock: usa a consulta de origem se ela funcionar; senão procura as tabelas de armazéns, existências e movimentos pelas colunas (`descobrir.py`).
- Uma consulta escrita à mão em `[consultas]` do `config.ini` tem sempre prioridade.

Para ver o que o sync percebeu: `python -m primavera_sync.sincronizador config.ini --esquema` mostra a consulta de cada parte, ou o que falta e porquê. O `--verificar` lista também o que ficou vazio ou foi assumido.
