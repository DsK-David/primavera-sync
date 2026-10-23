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

O que vai: os documentos não anulados (`CabecDoc` + `LinhasDoc`) dos tipos listados em `[tipos_documento]`. Na primeira corrida vai o histórico todo; depois, de `intervalo` em `intervalo` segundos, vão os novos.

O que já foi enviado fica em `estado.db` (SQLite). A Lonntra também recusa o mesmo documento duas vezes, por isso pode parar, arrancar ou perder o `estado.db` sem duplicar nada. Um documento recusado (por exemplo, uma taxa de IVA que não existe na Lonntra) fica registado com o motivo e não trava os outros. Se a Lonntra estiver em baixo, tenta de novo no ciclo seguinte.

### Instalar

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

- **Recibos** (liquidações de faturas a crédito): as faturas a crédito ficam por pagar na Lonntra até haver a rota de recibos (fase 2 do ADR-061).
- O tipo de cliente (coletivo/singular) sai do nome («Lda», «S.A.»…). Confirmar com a empresa.
- Os nomes das tabelas e colunas do Primavera (`evolution.py`) são os da v10 e ainda não foram confirmados num servidor real.

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
