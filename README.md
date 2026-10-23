# primavera-sync

Protótipo que lê o ficheiro **SAF-T (PT)** exportado do Primavera, guarda faturas e recibos numa base de dados própria (SQLite) e mostra um dashboard com totais, IVA, faturação por mês, saldo por cliente e faturas em aberto.

Sem dependências externas (Python 3.9+).

## Usar

```bash
python3 -m primavera_sync.importer samples/saft-exemplo.xml dados.db   # importar (pode repetir; faz upsert)
python3 -m primavera_sync.server dados.db 8000                         # abrir http://127.0.0.1:8000
python3 -m unittest discover -s tests -t .                             # testes
```

## Estrutura

- `primavera_sync/saft.py` – parser do SAF-T (faturas, recibos, clientes; ignora faturas anuladas)
- `primavera_sync/importer.py` – carrega para a BD
- `primavera_sync/analytics.py` – cálculos (em aberto = total da fatura − recibos associados)
- `primavera_sync/server.py` + `web/` – API JSON e dashboard

## Próximos passos

- Fonte alternativa: ler diretamente do SQL Server do Primavera (só leitura) ou da API Jasmin, gravando no mesmo esquema.
- Prazos de vencimento reais (o SAF-T não os traz), notas de crédito, autenticação no dashboard.
