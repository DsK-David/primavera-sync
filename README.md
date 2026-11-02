# primavera-sync

Duas coisas:

1. **Sincronizador Primavera ERP Evolution → Lonntra** (`primavera_sync/sincronizador.py`): lê os documentos de venda no SQL Server do Primavera e envia-os para a API da Lonntra, que faz o tratamento. Primeiro envia o histórico todo e depois fica a correr, enviando os documentos novos.
2. Protótipo de dashboard a partir do SAF-T (mais abaixo).

## Sincronizador Primavera → Lonntra

Lê os documentos de venda no SQL Server do Primavera e envia-os, um a um, para a rota de integração da **Lonntra** (`POST /integracao/primavera/documento`, ADR-061 no repositório da Lonntra). Não passa pela API Node.

Na Lonntra os documentos ficam:

- com a **data e o número do Primavera** (ex.: `FA 2026/15`), fora da numeração da Lonntra;
- com o estado e-Fatura **«Outro software»** (`EXTERNO`): nunca são comunicados à DNRE pela Lonntra, porque o Primavera já o fez;
- imutáveis, como os documentos comunicados;
- com o cliente e os artigos criados pelo código do Primavera. As FR e VD vão pagas, e as NC ficam ligadas à fatura de origem.

O que vai: os documentos não anulados (`CabecDoc` + `LinhasDoc`) dos tipos listados em `[tipos_documento]`, e também o **catálogo de artigos** e o **stock** (armazéns, quantidades e movimentos de entrada e saída), só para consulta na Lonntra (`catalogo = sim`). Do catálogo e do stock só vai o que mudou desde o último envio. Na primeira corrida vai o histórico todo; depois, de `intervalo` em `intervalo` segundos, vão os novos.

O que já foi enviado fica em `estado.db` (SQLite). A Lonntra também recusa o mesmo documento duas vezes, por isso pode parar, arrancar ou perder o `estado.db` sem duplicar nada. Um documento recusado (por exemplo, uma taxa de IVA que não existe na Lonntra) fica registado com o motivo e não trava os outros. Se a Lonntra estiver em baixo, tenta de novo no ciclo seguinte.

### Instalar

**Instalação rápida:** pôr o `cliente.ini` do cliente (modelo em `cliente.example.ini`) junto do `instalar.bat` no servidor e fazer duplo clique. O instalador instala o Python, descobre o Primavera, cria o acesso só de leitura, propõe os mapas, verifica tudo, cria o arranque automático e mostra o resultado. Ver `docs/instalar-auto-style.md`.

À mão:

1. **Na Lonntra** (uma vez):
   - criar a empresa: `php scripts/criar_empresa_lonntra.php ../ilhapay "Auto Style" <NIF>` (ou pelo ecrã de Empresas);
   - gerar a chave: `php yii integracao/chave <NIF>`. A chave só se mostra nesse momento; gerar outra desliga a anterior.
2. **No servidor do cliente (Windows)**:
   - instalar o Python 3.9+ e o pyodbc (`pip install pyodbc`; o "ODBC Driver 17/18 for SQL Server" costuma já vir com o Primavera);
   - criar no SQL Server um login **só de leitura** na base da empresa (`PRI<empresa>`):
     ```sql
     CREATE LOGIN lonntra_leitura WITH PASSWORD = '...';
     USE PRIAUTOSTYLE; CREATE USER lonntra_leitura FOR LOGIN lonntra_leitura;
     ALTER ROLE db_datareader ADD MEMBER lonntra_leitura;
     ```
   - `copy config.example.ini config.ini` e preencher a ligação, o URL da Lonntra e a chave. Conferir `[tipos_documento]` e `[modos_pagamento]` com as tabelas do Primavera do cliente;
   - experimentar com `python -m primavera_sync.sincronizador config.ini --uma-vez` e depois ver o resultado com `--estado`;
   - deixar a correr como serviço, por exemplo com o NSSM: `nssm install PrimaveraLonntra python -m primavera_sync.sincronizador C:\primavera-sync\config.ini` (pasta de arranque `C:\primavera-sync`).

Outras opções: `--completo` revê todos os documentos, não só os dos últimos `revisao_dias`; `--repetir-erros` volta a tentar os que esgotaram as tentativas.

### Ainda não

- **Recibos** (liquidações de faturas a crédito): o sync já sabe lê-los da tabela dos recebimentos da empresa e enviá-los à Lonntra (ver «Recebimentos» abaixo), mas vem **desligado**: primeiro vê-se o relatório e só depois se liga.
- O tipo de cliente (coletivo/singular) sai do nome («Lda», «S.A.»…). Confirmar com a empresa.
- Os nomes das tabelas e colunas do Primavera (`evolution.py`) são os da v10 e ainda não foram confirmados num servidor real.

### Recebimentos (como se liga)

A Auto Style guarda os recebimentos numa tabela própria (`CNO_recibimento`: `NumeroDoc`, `Entidade`, `ValorLiquido`, `ValorIva`, `ValorRetencao`, `ValorSelo`, `Recibido`, `DataRecebimento`…). Como não há uma regra publicada de como o `NumeroDoc` se liga à fatura, nem de que valores somam o total recebido, o sync **não as supõe: infere-as dos dados e confere-as**, e só envia se os números baterem.

1. **Casar:** liga o `NumeroDoc` às faturas já enviadas à Lonntra, tentando por esta ordem o número completo (`FA 2026/12`), série/número, tipo+número e só o número (este só se for único). Ignora zeros à esquerda, espaços e maiúsculas. Chaves que servem mais de uma fatura nunca se usam.
2. **Valor:** testa líquido+IVA, menos retenção, mais selo e só líquido contra o `TotalDocumento` das faturas casadas, e fica com a que bate certo na maioria.
3. **Só envia se for fiável:** pelo menos 80 % dos movimentos casados e 70 % das faturas com o valor a bater. Senão diz porquê (no `--estado`, no log e no aviso à Lonntra) e não envia nada.
4. **Envia** um recebimento por fatura e data (a soma das linhas), com um id estável: reenviar com outro valor atualiza, não duplica. A Lonntra acerta a fatura (paga, parcial ou por pagar).

**Ver o relatório antes de ligar** (não envia nada):

```
python -m primavera_sync.sincronizador empresas\<empresa>\config.ini --recebimentos
```

ou de casa, na Lonntra: `php yii integracao/comando <NIF> recebimentos` e depois `php yii integracao/comandos <NIF> <id>`.

Quando o relatório disser **PRONTO PARA ENVIAR** e os exemplos fizerem sentido, ligar no `config.ini` (`[sincronizacao] recebimentos = sim`) ou de casa: `php yii integracao/config <NIF> recebimentos sim`.

Opcional, no `config.ini`:

```ini
[recebimentos]
tabela = CNO_recibimento      # se o nome for outro, ou houver mais de uma tabela parecida
formula = liquido+iva         # fixa a fórmula do valor (liquido+iva, liquido+iva-retencao, liquido+iva+selo, liquido)
```

Se a conta não bater de nenhuma forma, a empresa pode escrever a sua própria consulta em `[consultas] recebimentos`, que tem de devolver sempre `Id`, `IdCabecDoc` (o `Id` do documento no Primavera), `Data`, `Valor`, `ModoPag` e `Numero`; nesse caso o sync não casa nem infere nada, usa-a tal e qual.

### Recebimentos: o que falta descobrir

Para o sync enviar os recibos é preciso saber em que tabelas do Primavera da empresa ficam as **liquidações** (recibos) e o **saldo pendente** de cada documento. Os nomes mudam com a versão, por isso não se adivinham: errar aqui marcava faturas como pagas sem o estarem.

Correr tudo isto no **SQL Server Management Studio, na base da empresa** (`USE PRI<empresa>;`) e guardar o resultado de cada passo.

**Passo 1. Confirmar que se está na base certa** (se a consulta dos recibos der 0 resultados, é quase sempre porque se estava na `master` ou na `PRIEMPRE`):

```sql
SELECT DB_NAME() AS base;
SELECT COUNT(*) AS tabelas FROM sys.tables;
SELECT name FROM sys.tables WHERE name IN ('CabecDoc','LinhasDoc','Clientes','Artigo');
```

Na base certa devolve o nome da empresa (`PRI…`), umas centenas de tabelas e as quatro da lista. Se não trouxer a `CabecDoc`, trocar com `USE PRI<código da empresa>;`.

**Passo 2. Procurar as tabelas dos recibos pelo nome:**

```sql
SELECT t.name AS tabela, c.name AS coluna
FROM sys.tables t JOIN sys.columns c ON c.object_id = t.object_id
WHERE t.name LIKE '%Liq%' OR t.name LIKE '%Pend%' OR t.name LIKE '%Receb%' OR t.name LIKE '%Recib%'
ORDER BY t.name, c.column_id;
```

**Passo 3. Se o passo 2 deu 0 resultados na base certa, procurar mais largo:**

```sql
-- por nome de tabela
SELECT name FROM sys.tables
WHERE name LIKE '%Liq%' OR name LIKE '%Pend%' OR name LIKE '%Tesour%' OR name LIKE '%Cobr%'
   OR name LIKE '%Hist%' OR name LIKE '%Conta%' OR name LIKE '%Pag%' OR name LIKE '%Mov%'
ORDER BY name;

-- por nome de coluna (o que guarda saldos e liquidações)
SELECT t.name AS tabela, c.name AS coluna
FROM sys.tables t JOIN sys.columns c ON c.object_id = t.object_id
WHERE c.name LIKE '%Pendente%' OR c.name LIKE '%Liquid%' OR c.name LIKE '%Recebid%' OR c.name LIKE '%Pago%'
ORDER BY t.name, c.name;
```

Se o resultado for grande, copiar só as primeiras 100 linhas.

**Passo 4. Um exemplo real:** o número de uma fatura que foi paga depois (por exemplo `FA 2026/12`) e o do recibo dela (por exemplo `RE 2026/3`). Serve para conferir que o que o sync lê bate certo com o Primavera.

**Alternativa sem SQL:** no Primavera, em *Vendas → Tabelas → Tipos de documento*, procurar os tipos «Recibo» ou «Liquidação» e anotar o código (por exemplo `RE`). Com ele procura-se onde esses documentos ficam gravados.

**Passo 5. Decidir:** o sync envia só os recebimentos novos a cada ciclo, ou também reconcilia, uma vez por dia, o saldo de todas as faturas ainda em aberto? (Recomendado: as duas coisas.)

**Passo 6. A Auto Style guarda os recebimentos numa tabela própria (`CNO_recibimento`).** O prefixo `CNO` não é do Primavera padrão: é uma tabela criada à medida da empresa, por isso o sync **não a descobre sozinho** e é preciso conhecer a estrutura dela. Correr na base da empresa (`USE PRI<empresa>;`) e guardar o resultado de cada consulta (nos exemplos, trocar pelo nome certo se a tabela se chamar de outra maneira):

```sql
-- 6.1 o nome exato da tabela e quantas linhas tem
SELECT t.name AS tabela, SUM(p.rows) AS linhas
FROM sys.tables t JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
WHERE t.name LIKE 'CNO%'
GROUP BY t.name ORDER BY t.name;

-- 6.2 as colunas dela
SELECT c.column_id AS n, c.name AS coluna, ty.name AS tipo, c.max_length, c.is_nullable
FROM sys.columns c JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE c.object_id = OBJECT_ID('CNO_recibimento')
ORDER BY c.column_id;

-- 6.3 para onde aponta (chaves ligadas a outras tabelas: como se liga à fatura)
SELECT OBJECT_NAME(fk.parent_object_id) AS tabela, COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS coluna,
       OBJECT_NAME(fk.referenced_object_id) AS refere, COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS coluna_ref
FROM sys.foreign_keys fk JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
WHERE OBJECT_NAME(fk.parent_object_id) LIKE 'CNO%' OR OBJECT_NAME(fk.referenced_object_id) LIKE 'CNO%';

-- 6.4 as últimas 15 linhas reais (tapar nomes de clientes, se for preciso)
SELECT TOP 15 * FROM CNO_recibimento ORDER BY 1 DESC;
```

**6.5 Um caso concreto:** o número de uma fatura que foi paga depois (por exemplo `FA 2026/12`) e a linha que a representa nessa tabela.

O que se quer perceber com isto:

- que coluna **liga o recebimento à fatura** (número, série ou o `IdCabecDoc`);
- o **valor**, a **data** e o **método de pagamento** do recebimento;
- se guarda recebimentos **parciais** (vários por fatura), e se tem **anulados** ou estados;
- se a tabela vem de uma aplicação própria da empresa, caso em que pode não ter relação com o Primavera padrão.

Como é uma tabela da empresa, a leitura dela fica numa consulta própria no `config.ini` (chave `recebimentos` da secção `[consultas]`), que tem de devolver sempre estas colunas: `Id` (identificador único do recebimento), `IdCabecDoc` (o `Id` do documento no Primavera), `Data`, `Valor`, `ModoPag` e `Numero` (o do recibo). O resto do sync (envio, repetição, avisos) é o mesmo.

Com isto escreve-se a leitura dos recebimentos no `evolution.py` (que, como o resto, se adapta aos nomes de tabela e coluna da base) e o envio para a rota da Lonntra.

## Protótipo SAF-T

Protótipo que lê o ficheiro **SAF-T (PT)** exportado do Primavera, guarda faturas e recibos numa base de dados própria (SQLite) e mostra um dashboard com totais, IVA, faturação por mês, saldo por cliente e faturas em aberto.

Sem dependências externas (Python 3.9+).

### Usar

```bash
python3 -m primavera_sync.importer samples/saft-exemplo.xml dados.db   # importar (pode repetir; faz upsert)
python3 -m primavera_sync.server dados.db 8000                         # abrir http://127.0.0.1:8000
python3 -m unittest discover -s tests -t .                             # testes
```

### Estrutura

- `primavera_sync/saft.py` – parser do SAF-T (faturas, recibos, clientes; ignora faturas anuladas)
- `primavera_sync/importer.py` – carrega para a BD
- `primavera_sync/analytics.py` – cálculos (em aberto = total da fatura − recibos associados)
- `primavera_sync/server.py` + `web/` – API JSON e dashboard

### Próximos passos

- Fonte alternativa: ler diretamente do SQL Server do Primavera (só leitura) ou da API Jasmin, gravando no mesmo esquema.
- Prazos de vencimento reais (o SAF-T não os traz), notas de crédito, autenticação no dashboard.
