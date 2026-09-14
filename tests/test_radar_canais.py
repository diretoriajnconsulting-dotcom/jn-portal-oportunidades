"""As três janelas de datas do SICONV viram três canais.

Testa o `SQL_ABERTOS` de `scripts/radar.py` sobre uma tabela `programa` montada
à mão, com as mesmas colunas que `coletar` carrega. Até o contrato 1.1 a janela
BENEF_ESP ficava de fora; em 13/09/2026 eram 113 janelas na Paraíba.
"""
import importlib.util
import unittest
from datetime import date, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("radar", ROOT / "scripts" / "radar.py")
radar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(radar)

HOJE = date.today()
FUTURO = HOJE + timedelta(days=30)
PASSADO = HOJE - timedelta(days=1)


def programa(con, linhas):
    con.execute("""
        CREATE OR REPLACE TABLE programa (
          COD_PROGRAMA VARCHAR, NOME_PROGRAMA VARCHAR, ORGAO VARCHAR, MODALIDADE VARCHAR,
          NATUREZA VARCHAR, SIT_PROGRAMA VARCHAR,
          DT_PROG_INI_RECEB_PROP DATE, DT_PROG_FIM_RECEB_PROP DATE,
          DT_PROG_INI_EMENDA_PAR DATE, DT_PROG_FIM_EMENDA_PAR DATE,
          DT_PROG_INI_BENEF_ESP DATE, DT_PROG_FIM_BENEF_ESP DATE
        )""")
    for l in linhas:
        con.execute("INSERT INTO programa VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
            l["cod"], l.get("nome", "Programa"), "Ministério", "Convênio",
            l.get("natureza", "Administração Pública Municipal"), l.get("sit", "DISPONIBILIZADO"),
            HOJE, l.get("fim_prop"), HOJE, l.get("fim_emenda"), HOJE, l.get("fim_benef"),
        ])
    cur = con.execute(radar.SQL_ABERTOS)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class CanaisTests(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")

    def test_as_tres_janelas_viram_tres_canais(self):
        r = programa(self.con, [
            {"cod": "1", "fim_prop": FUTURO},
            {"cod": "2", "fim_emenda": FUTURO},
            {"cod": "3", "fim_benef": FUTURO},
        ])
        self.assertEqual(
            {("1", "proposta"), ("2", "emenda"), ("3", "beneficiario_especifico")},
            {(x["codigos"][0], x["canal"]) for x in r},
        )

    def test_programa_com_duas_janelas_abertas_vira_duas_linhas(self):
        # 21 programas da Paraíba tinham emenda e beneficiário específico ao mesmo tempo.
        r = programa(self.con, [{"cod": "9", "fim_emenda": FUTURO, "fim_benef": FUTURO}])
        self.assertEqual(["beneficiario_especifico", "emenda"], sorted(x["canal"] for x in r))

    def test_janela_de_beneficiario_vencida_nao_entra(self):
        r = programa(self.con, [{"cod": "4", "fim_benef": PASSADO}])
        self.assertEqual([], r)

    def test_programa_fora_de_situacao_valida_nao_entra(self):
        r = programa(self.con, [{"cod": "5", "fim_benef": FUTURO, "sit": "INATIVO"}])
        self.assertEqual([], r)

    def test_o_prazo_da_linha_e_o_da_propria_janela(self):
        outro = FUTURO + timedelta(days=60)
        r = programa(self.con, [{"cod": "6", "fim_prop": FUTURO, "fim_benef": outro}])
        prazos = {x["canal"]: x["fecha"] for x in r}
        self.assertEqual(FUTURO, prazos["proposta"])
        self.assertEqual(outro, prazos["beneficiario_especifico"])


if __name__ == "__main__":
    unittest.main()
