# Instalar o primavera-sync na Auto Style

Guia para ligar o Primavera da Auto Style à Lonntra (https://app.lonntra.cv). O sync corre no servidor da Auto Style, lê o Primavera (só leitura) e envia os documentos de venda para a Lonntra.

## 0. Antes de ir ao servidor (do lado da Lonntra)

1. A Lonntra de produção tem de estar atualizada: `ssh lonntra lonntra-atualizar`. Verificar que a rota existe:
   ```bash
   curl -s https://app.lonntra.cv/integracao/primavera/estado
   ```
   A resposta tem de ser «Chave de integração inválida ou inativa.» (401). Se der 404, a atualização não foi aplicada.
2. Criar a empresa Auto Style (NIF 283048808), com o repositório do e-Fatura `1` (Principal) e acesso de Administrador para o utilizador `auto.style`. O utilizador tem de existir e estar ativo na Lonntra. Correr no Mac, por esta ordem:
   ```bash
   scp scripts/criar_empresa_lonntra.php lonntra:/tmp/criar_empresa_lonntra.php
   ```
   ```bash
   ssh lonntra "cd /var/www/lonntra && sudo -u www-data php /tmp/criar_empresa_lonntra.php /var/www/lonntra 'Auto Style' 283048808 auto.style 1"
   ```
   ```bash
   ssh lonntra "rm /tmp/criar_empresa_lonntra.php"
   ```
3. Gerar a chave do sync. Guardá-la, porque só se mostra esta vez; gerar outra desliga a anterior.
   ```bash
   ssh lonntra "cd /var/www/lonntra && sudo -u www-data php yii integracao/chave 283048808"
   ```
4. Confirmar o **plano** da Auto Style na Lonntra: os documentos importados contam para o limite de documentos por mês, e o histórico todo pode passá-lo.

## 1. Pedir acesso pelo AnyDesk

Pedir à Auto Style:

1. Que instalem o **AnyDesk** (anydesk.com) no servidor onde está o Primavera e enviem o **número de 9 dígitos**.
2. **Acesso sem supervisão**: em Definições → Segurança, definir uma palavra-passe de acesso, para não ser preciso alguém aceitar a ligação de cada vez.
3. Um **utilizador do Windows que possa instalar programas** (Python e o serviço).
4. **Acesso ao SQL Server**: o SQL Server Management Studio com um utilizador que possa criar logins, ou o contacto do técnico que instalou o Primavera.
5. A **autorização por escrito** (um email chega) para aceder ao servidor e ler os dados de faturação.

Mensagem pronta a enviar:

> Bom dia. Para ligar o Primavera à Lonntra preciso de acesso remoto ao servidor onde está o Primavera. Podem instalar o AnyDesk nesse servidor, ativar o acesso sem supervisão com uma palavra-passe e enviar-me o número do AnyDesk? Preciso também de um utilizador do Windows que possa instalar programas e de acesso ao SQL Server (ou do contacto do técnico do Primavera) para criar um utilizador só de leitura. A ligação só lê os dados de faturação, não altera nada no Primavera. Obrigado.

## 2. Descobrir a instância e a base da empresa

No servidor, abrir o **SQL Server Management Studio** e correr:

```sql
SELECT @@SERVERNAME AS instancia, name AS base FROM sys.databases WHERE name LIKE 'PRI%';
```

A base da Auto Style é uma das `PRI…` (ex.: `PRIAUTOSTYLE`). Se houver várias, quem usa o Primavera sabe qual é.

## 3. Confirmar as tabelas e os códigos

Na base da empresa:

```sql
SELECT TOP 5 Id, TipoDoc, Serie, NumDoc, Data, DataVenc, Entidade, ModoPag, Moeda, TotalDocumento FROM CabecDoc WHERE TipoEntidade = 'C' ORDER BY Data DESC;
SELECT TOP 5 Artigo, Quantidade, PrecoLiquido, TaxaIva, IdLinhaOrigemCopia FROM LinhasDoc;
SELECT TipoDoc, COUNT(*) AS n FROM CabecDoc WHERE TipoEntidade = 'C' GROUP BY TipoDoc;
SELECT ModoPag, COUNT(*) AS n FROM CabecDoc WHERE TipoEntidade = 'C' GROUP BY ModoPag;
```

- Se alguma coluna der erro, é preciso ajustar `primavera_sync/evolution.py` antes de continuar.
- As duas últimas consultas mostram os códigos de documento e de pagamento que a Auto Style usa. Servem para preencher o `config.ini` (passo 7).

## 4. Criar o login só de leitura

Trocar o nome da base pelo do passo 2 e escolher uma palavra-passe forte:

```sql
CREATE LOGIN lonntra_leitura WITH PASSWORD = 'escolher-uma-forte';
USE PRIAUTOSTYLE;
CREATE USER lonntra_leitura FOR LOGIN lonntra_leitura;
ALTER ROLE db_datareader ADD MEMBER lonntra_leitura;
```

## 5. Instalar o Python e o pyodbc

1. Instalar o Python a partir de python.org. Na instalação, marcar **«Add Python to PATH»**.
2. No PowerShell:
   ```
   pip install pyodbc
   Get-OdbcDriver | Select Name
   ```
   Na lista tem de aparecer «ODBC Driver 17 for SQL Server» ou «18». Se não aparecer, instalar o driver a partir do site da Microsoft.
3. Confirmar que o servidor chega à Lonntra: abrir https://app.lonntra.cv no browser do servidor.

## 6. Copiar o sync

No GitHub, `DsK-David/primavera-sync`, ramo `feat/integracao-primavera` (ou `main`, depois de juntado), descarregar com «Code → Download ZIP». No servidor fica só isto:

```
C:\primavera-sync\
├── primavera_sync\   (__init__.py, evolution.py, lonntra.py, sincronizador.py)
└── config.ini        (cópia do config.example.ini)
```

O resto do repositório (protótipo SAF-T, testes, scripts) não é preciso no servidor. O `estado.db` e o `primavera-sync.log` são criados sozinhos na primeira corrida.

## 7. Preencher o `C:\primavera-sync\config.ini`

- `ligacao`: o nome da instância, a base e a palavra-passe do passo 4. Com o «ODBC Driver 18», trocar o nome do driver e acrescentar `;TrustServerCertificate=yes` no fim.
  ```
  ligacao = DRIVER={ODBC Driver 17 for SQL Server};SERVER=NOME\INSTANCIA;DATABASE=PRIAUTOSTYLE;UID=lonntra_leitura;PWD=...
  ```
- `url = https://app.lonntra.cv`
- `chave =` a chave gerada no passo 0.3.
- `[tipos_documento]`: os códigos do passo 3. Cada um aponta para FT, FR, TV, NC ou ND (ex.: `FA = FT`, `VD = TV`). Só os tipos listados são enviados; FR e TV vão pagos no ato.
- `[modos_pagamento]`: os códigos do passo 3, cada um com o código de pagamento da Lonntra (10 numerário, 20 cheque, 30 transferência, 54 cartão de crédito, 55 cartão de débito). Sem correspondência, fica numerário.
- **Para o primeiro teste**, `data_inicio` com uma data de há poucos dias (ex.: `data_inicio = 2026-10-20`). Assim só vão alguns documentos.

## 8. Testar

```
cd C:\primavera-sync
python -m primavera_sync.sincronizador config.ini --uma-vez
python -m primavera_sync.sincronizador config.ini --estado
```

Entrar em https://app.lonntra.cv com o `auto.style` e conferir esses documentos: números, datas, valores e clientes. Na Lonntra aparecem com o estado e-Fatura «Outro software».

Se algo estiver errado, **parar aqui** e ver o `primavera-sync.log`. Os documentos que já entraram na Lonntra não se apagam, por isso é melhor corrigir com poucos documentos.

## 9. Enviar o histórico todo

Com tudo certo, apagar o valor de `data_inicio` (fica `data_inicio =`) e correr com `--completo`, para rever todas as datas:

```
python -m primavera_sync.sincronizador config.ini --uma-vez --completo
```

Com muitos anos de documentos pode demorar. Pode parar-se e voltar a correr: não duplica nada.

## 10. Deixar a correr sempre

Descarregar o NSSM (nssm.cc) e instalar o sync como serviço do Windows:

```
nssm install PrimaveraLonntra python -m primavera_sync.sincronizador C:\primavera-sync\config.ini
nssm set PrimaveraLonntra AppDirectory C:\primavera-sync
nssm start PrimaveraLonntra
```

O serviço arranca sozinho com o Windows e envia os documentos novos a cada minuto (`intervalo` no `config.ini`).

## Depois de instalado

- Ver o que foi enviado e os erros: `python -m primavera_sync.sincronizador config.ini --estado`
- Voltar a tentar os documentos que esgotaram as tentativas: `--repetir-erros`
- Log: `C:\primavera-sync\primavera-sync.log`
- Se a chave mudar (`php yii integracao/chave` outra vez), atualizar o `config.ini` e reiniciar o serviço: `nssm restart PrimaveraLonntra`

## Ainda não está coberto

- **Recibos**: as faturas a crédito ficam «por pagar» na Lonntra até haver a rota de recibos (fase 2 do ADR-061).
- O tipo de cliente (coletivo/singular) sai do nome («Lda», «S.A.»…). Confirmar com a Auto Style.
