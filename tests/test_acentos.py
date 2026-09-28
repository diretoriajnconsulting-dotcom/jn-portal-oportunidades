"""Acentos quebrados nos nomes de programa do SICONV: o título se conserta, o id não muda.

O arquivo de programas entrega nomes como "5663 - Autonomia Econ?mica das Mulheres". O
conserto em `funding_intelligence/acentos.py` é porte do painel de execução do monorepo e
usa só o vocabulário do próprio arquivo. No radar ele troca o texto que se lê e que casa com
os temas; o contrato que estes testes guardam é que nem o `id` do v1 nem o do v2 dependem
dele.
"""
import importlib.util
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import duckdb
import jsonschema

from funding_intelligence.acentos import indice_de, reparar, reparos_de
from funding_intelligence.adapters.transferegov import TransferegovAdapter

ROOT = Path(__file__).parents[1]
HOJE = date.today()
FUTURO = HOJE + timedelta(days=30)
MUNICIPAL = "Administração Pública Municipal"


def script(nome):
    spec = importlib.util.spec_from_file_location(nome, ROOT / "scripts" / f"{nome}.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


radar = script("radar")
publicar = script("publicar")

VOCABULARIO = indice_de([
    ("Aperfeiçoamento do Sistema Único de Saúde", 5),
    ("Rede de Cuidados à Pessoa com Deficiência", 3),
    ("Apoio à Política Nacional de Desenvolvimento Urbano", 4),
    ("Oferta de Água", 3),
    ("Plano de Ação", 8),
    ("Pró-Esporte", 2), ("Pré-Escola", 2),
    ("Programa Cidadania", 1), ("Plano de Açao", 1),
])


class ReparoTests(unittest.TestCase):
    def test_palavra_quebrada_vira_a_integra_de_mesma_forma(self):
        self.assertEqual("Aperfeiçoamento do SUS - Saúde", reparar("Aperfei?oamento do SUS - Sa�de", VOCABULARIO))
        self.assertEqual("Plano de Ação", reparar("Plano de A??o", VOCABULARIO))

    def test_so_troca_quando_um_candidato_predomina(self):
        self.assertEqual("Pr?-Escola", reparar("Pr?-Escola", VOCABULARIO))  # pré ou pró: empate, fica como veio
        self.assertEqual("Ação", reparar("A??o", indice_de([("Ação", 10), ("Açao", 1)])))  # 10 contra 1 basta
        self.assertEqual("Educa??o", reparar("Educa??o", VOCABULARIO))  # sem candidato, fica

    def test_respeita_a_caixa_do_nome(self):
        self.assertEqual(
            "REDE DE CUIDADOS À PESSOA COM DEFICIÊNCIA",
            reparar("REDE DE CUIDADOS ? PESSOA COM DEFICI?NCIA", VOCABULARIO),
        )
        self.assertEqual("Sistema Único", reparar("Sistema ?nico", VOCABULARIO))  # depois de maiúscula, abre com maiúscula
        self.assertEqual("Oferta de água", reparar("Oferta de ?gua", VOCABULARIO))
        self.assertEqual("Rede de URGÊNCIA", reparar("Rede de URG?NCIA", indice_de([("Urgência", 1)])))  # sigla em nome misto

    def test_nome_quebrado_nao_ensina_palavra(self):
        self.assertEqual("Saúde", reparar("Sa?de", indice_de([("Saúde", 1), ("Sa�de", 5), ("Sa?de", 5)])))

    def test_interrogacao_sozinha_depende_da_palavra_anterior(self):
        self.assertEqual("Apoio à Implantação", reparar("Apoio ? Implantação", VOCABULARIO))
        self.assertEqual("SENASP ? AÇÃO 20ID", reparar("SENASP ? AÇÃO 20ID", VOCABULARIO))  # travessão: "senasp à" nunca aparece
        self.assertEqual("Apoio ? (RP6)", reparar("Apoio ? (RP6)", VOCABULARIO))  # antes de parêntese não é crase
        self.assertEqual("Cuidados ?", reparar("Cuidados ?", VOCABULARIO))  # no fim é pergunta

    def test_nome_integro_e_vazio_passam_intactos(self):
        self.assertEqual("Programa Cidadania", reparar("Programa Cidadania", VOCABULARIO))
        self.assertIsNone(reparar(None, VOCABULARIO))
        self.assertEqual("", reparar("", VOCABULARIO))

    def test_reparos_do_arquivo_trazem_so_o_que_mudou(self):
        self.assertEqual(
            {"Aten??o B?sica - RP6": "Atenção Básica - RP6"},
            reparos_de([("Atenção Básica", 1), ("Aten??o B?sica - RP6", 1), ("Pr?-Escola", 1), ("", 1)]),
        )


def conexao(programas, outras_ufs=(), propostas=()):
    """As tabelas que `radar.coletar` deixa prontas para `radar.analisar`.

    `programas`: (código, nome, natureza) com janela de proposta aberta na UF.
    `outras_ufs`: (nome, linhas) do resto do arquivo. Com os nomes da UF, formam a tabela
    `nome_programa`, que é o arquivo INTEIRO.
    `propostas`: (código do programa, id da proposta) enviadas depois da execução anterior.
    """
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE programa (
          ID_PROGRAMA BIGINT, COD_PROGRAMA VARCHAR, NOME_PROGRAMA VARCHAR, ORGAO VARCHAR,
          MODALIDADE VARCHAR, NATUREZA VARCHAR, SIT_PROGRAMA VARCHAR,
          DT_PROG_INI_RECEB_PROP DATE, DT_PROG_FIM_RECEB_PROP DATE,
          DT_PROG_INI_EMENDA_PAR DATE, DT_PROG_FIM_EMENDA_PAR DATE,
          DT_PROG_INI_BENEF_ESP DATE, DT_PROG_FIM_BENEF_ESP DATE
        )""")
    ids = {}
    for i, (cod, nome, natureza) in enumerate(programas):
        ids[cod] = i
        con.execute("INSERT INTO programa VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            i, cod, nome, "MINISTÉRIO DAS MULHERES", "CONVENIO", natureza, "DISPONIBILIZADO",
            HOJE, FUTURO, None, None, None, None,
        ])
    con.execute("CREATE TABLE nome_programa (nome VARCHAR, n BIGINT)")
    if outras_ufs:
        con.executemany("INSERT INTO nome_programa VALUES (?, ?)", list(outras_ufs))
    con.execute("INSERT INTO nome_programa SELECT NOME_PROGRAMA, count(*) FROM programa GROUP BY 1")
    con.execute("CREATE TABLE prog_prop (ID_PROGRAMA BIGINT, ID_PROPOSTA BIGINT)")
    con.execute("""
        CREATE TABLE proposta (
          ID_PROPOSTA BIGINT, UF VARCHAR, MUNICIPIO VARCHAR, PROPONENTE VARCHAR, NATUREZA VARCHAR,
          SITUACAO VARCHAR, OBJETO VARCHAR, DIA_PROPOSTA DATE, VL_GLOBAL_PROP DOUBLE
        )""")
    for cod, id_proposta in propostas:
        con.execute("INSERT INTO prog_prop VALUES (?, ?)", [ids[cod], id_proposta])
        con.execute("INSERT INTO proposta VALUES (?,?,?,?,?,?,?,?,?)", [
            id_proposta, "PB", "João Pessoa", "Prefeitura", MUNICIPAL, "Proposta/Plano de Trabalho Enviado",
            "objeto", HOJE, 100000.0,
        ])
    return con


def publicar_e_coletar(con):
    """Radar → contrato v1 (`publicar.py`, validado pelo schema) → catálogo v2, como no workflow."""
    ach = radar.analisar(con, "PB", radar.TEMAS_PADRAO, {})
    with tempfile.TemporaryDirectory() as pasta:
        achados, v1 = Path(pasta) / "achados.json", Path(pasta) / "oportunidades.json"
        achados.write_text(json.dumps({
            **ach, "origem_last_modified": {}, "origem_urls": {}, "gerado_em": f"{HOJE.isoformat()}T09:30:00",
        }, ensure_ascii=False), encoding="utf-8")
        argv = ["publicar.py", "--entrada", str(achados), "--saida", str(v1)]
        with mock.patch("sys.argv", argv), mock.patch("builtins.print"):
            publicar.main()
        payload = json.loads(v1.read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "schema/oportunidades.schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(payload, schema, format_checker=jsonschema.FormatChecker())
        v2 = TransferegovAdapter(fixture=str(v1)).collect()
    return payload["oportunidades"], v2


QUEBRADO = "5663 - Autonomia Econ?mica das Mulheres"
INTEIRO = "5663 - Autonomia Econômica das Mulheres"
# Nomes de outras UFs: o radar só analisa a Paraíba, mas aprende com o arquivo nacional.
NACIONAL = [("Igualdade de Direitos e Autonomia Econômica das Mulheres", 12), ("Promoção da Autonomia Econômica", 30)]


class RadarTests(unittest.TestCase):
    def test_titulo_sai_consertado_com_vocabulario_do_arquivo_nacional(self):
        con = conexao([("5663", QUEBRADO, MUNICIPAL)], NACIONAL)
        janela = radar.analisar(con, "PB", radar.TEMAS_PADRAO, {})["abertos"][0]
        self.assertEqual(INTEIRO, janela["NOME_PROGRAMA"])
        self.assertEqual(QUEBRADO, janela["NOME_PROGRAMA_SICONV"])

    def test_sem_vocabulario_o_titulo_fica_como_veio(self):
        con = conexao([("5663", QUEBRADO, MUNICIPAL)])
        janela = radar.analisar(con, "PB", radar.TEMAS_PADRAO, {})["abertos"][0]
        self.assertEqual(QUEBRADO, janela["NOME_PROGRAMA"])

    def test_temas_da_carteira_casam_com_o_titulo_consertado(self):
        # "habitaç" não casava com "Habita??o": a janela ficava fora da carteira.
        con = conexao([("1", "Apoio ? Habita??o Rural", MUNICIPAL)], [("Apoio à Habitação de Interesse Social", 40)])
        janela = radar.analisar(con, "PB", radar.TEMAS_PADRAO, {})["abertos"][0]
        self.assertEqual("Apoio à Habitação Rural", janela["NOME_PROGRAMA"])
        self.assertIn("habitaç", janela["temas"])

    def test_propostas_novas_levam_o_titulo_consertado(self):
        con = conexao([("5663", QUEBRADO, MUNICIPAL)], NACIONAL, propostas=[("5663", 7)])
        estado = {"executado_em": (HOJE - timedelta(days=1)).isoformat(), "programas_abertos": ["5663"]}
        ach = radar.analisar(con, "PB", radar.TEMAS_PADRAO, estado)
        self.assertEqual([INTEIRO], [p["NOME_PROGRAMA"] for p in ach["novas_propostas"]])


class IdentidadeTests(unittest.TestCase):
    def test_consertar_o_titulo_nao_troca_o_id_no_v1_nem_no_v2(self):
        programas = [("5663", QUEBRADO, MUNICIPAL), ("5663", QUEBRADO, "Organização da Sociedade Civil")]
        v1_cru, v2_cru = publicar_e_coletar(conexao(programas))
        v1, v2 = publicar_e_coletar(conexao(programas, NACIONAL))

        # Ordenados: as duas janelas empatam em prazo e nome, e o DuckDB não fixa a ordem do empate.
        self.assertEqual(sorted(o["id"] for o in v1_cru), sorted(o["id"] for o in v1))
        self.assertEqual(sorted(o["id"] for o in v2_cru), sorted(o["id"] for o in v2))
        self.assertEqual(2, len({o["id"] for o in v2}))
        self.assertEqual({QUEBRADO}, {o["programa"] for o in v1_cru})
        self.assertEqual({INTEIRO}, {o["programa"] for o in v1})
        self.assertEqual({INTEIRO}, {o["title"] for o in v2})

    def test_grafia_quebrada_e_integra_continuam_duas_janelas(self):
        # O SQL_ABERTOS agrupa pelo nome cru. Juntar as duas grafias depois do
        # conserto misturaria os códigos, e os códigos são a identidade do v2.
        con = conexao([("1", QUEBRADO, MUNICIPAL), ("2", INTEIRO, MUNICIPAL)], NACIONAL)
        v1, v2 = publicar_e_coletar(con)
        self.assertEqual([["1"], ["2"]], sorted(o["codigos"] for o in v1))
        self.assertEqual({INTEIRO}, {o["programa"] for o in v1})
        self.assertEqual(2, len({o["id"] for o in v1}))  # o id do v1 usa o nome cru e não colide
        self.assertEqual(2, len({o["id"] for o in v2}))


if __name__ == "__main__":
    unittest.main()
