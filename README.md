# PONTE Funding Intelligence

Radar diário e auditável de oportunidades de financiamento. O catálogo v2
normaliza fontes oficiais, separa existência de elegibilidade e matching,
preserva retificações como histórico e torna a saúde de cada fonte explícita.

## Fontes P0

- TransfereGov (mantém também o contrato público legado `1.0`)
- Finep
- CNPq
- FAPESQ-PB

## Artefatos públicos

- `public/data/oportunidades.json`: contrato TransfereGov `1.0`, sem quebra de compatibilidade.
- `public/data/funding-opportunities-v2.json`: catálogo multifuente `2.0`.
- `schema/funding-opportunity-2.0.schema.json`: contrato canônico e versionado.

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
