# PONTE Funding Intelligence

Radar diário e auditável de oportunidades de financiamento. O catálogo v2
normaliza fontes oficiais, separa existência de elegibilidade e matching,
preserva retificações como histórico e torna a saúde de cada fonte explícita.
Elegibilidade por tipo de organização é um hard gate: afinidade temática ou
territorial nunca compensa um proponente incompatível.

## Fontes P0

- TransfereGov (mantém também o contrato público legado `1.0`)
- Finep
- CNPq
- FAPESQ-PB

## Fontes P1 — APIs novas do TransfereGov

Programas com janela de recebimento aberta ou agendada, uma oportunidade por janela
(`funding_intelligence/adapters/transferegov_apis.py`):

- Transferências especiais: a janela de ciência e plano de ação;
- Fundo a fundo: janelas de voluntários, emendas e beneficiários específicos;
- Parcerias: janelas de espontâneo, emenda e específico (inclui o fundo a fundo do SUAS).

Só entram programas disponibilizados, do ano atual ou do anterior: no fundo a fundo,
programas de 2020 a 2022 mantêm janela aberta até 31/12/2026 só para quem já aderiu.
TED fica de fora, porque só órgão federal recebe.

## Artefatos públicos

- `public/data/oportunidades.json`: contrato TransfereGov `1.0`, sem quebra de compatibilidade.
- `public/data/funding-opportunities-v2.json`: catálogo multifuente `2.2`.
- `schema/funding-opportunity-2.2.schema.json`: contrato canônico e versionado.

O catálogo declara `change_mode=baseline` na primeira geração, sem comunicar
o inventário inicial como novidade. Em execuções incrementais, cada registro
recebe `change_status` igual a `new`, `changed`, `closed` ou `unchanged`.

O contrato 2.2 (14/10/2026) só acrescenta; um arquivo 2.1 continua válido nele:

- **Encerradas por 24 meses.** A oportunidade que encerra fica no catálogo, com
  `dates.closed`, por 730 dias. Antes aparecia uma rodada como `closed` e sumia.
- **Fluxo contínuo.** `channel: "fluxo_continuo"` é a porta sem edital nem prazo
  (o modelo EMBRAPII, uma linha do BNDES): `status: "open"` e prazo nulo. Conta
  em `summary.continuous`, não em `summary.open`.
- **Parceiras.** `eligibility.partner_types` diz quem entra só como parceira, sem
  propor nem receber (a empresa no PPSUS, por exemplo). `partners` traz os
  parceiros do edital (Decit/MS, CNPq, a FAP).
- **Tipos novos:** `ict_privada`, `hospital` e `fundacao_apoio`. Desde então,
  `ict` quer dizer ICT pública.
- **Falha parcial.** A coleta que lê várias casas avisa em `failed_scopes` as
  UFs que não responderam; os itens anteriores delas seguem como defasados, sem
  virar encerrados.
- **Carga histórica.** O disparo manual com `carga_historica` faz cada fonte ler
  uma vez os últimos 24 meses de encerradas (`RADAR_CARGA_HISTORICA=1`).

Ordem de publicação de contrato novo: o site aceita primeiro, o radar depois.

## Execução local

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v

python -m funding_intelligence.cli collect \
  --source finep \
  --output build/sources/finep.json

python -m funding_intelligence.cli aggregate \
  --inputs "build/sources/*.json" \
  --previous public/data/funding-opportunities-v2.json \
  --output public/data/funding-opportunities-v2.json
```

O workflow diário coleta cada fonte em um job isolado. Se uma fonte falhar, o
último dado conhecido é mantido com `source.stale=true` e a falha aparece em
`sources[].status`; dados antigos nunca são apresentados como recém-verificados.
