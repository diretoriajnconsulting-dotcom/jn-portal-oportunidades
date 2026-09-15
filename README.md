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
- `public/data/funding-opportunities-v2.json`: catálogo multifuente `2.0`.
- `schema/funding-opportunity-2.1.schema.json`: contrato canônico e versionado.

O catálogo declara `change_mode=baseline` na primeira geração, sem comunicar
o inventário inicial como novidade. Em execuções incrementais, cada registro
recebe `change_status` igual a `new`, `changed`, `closed` ou `unchanged`.

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
