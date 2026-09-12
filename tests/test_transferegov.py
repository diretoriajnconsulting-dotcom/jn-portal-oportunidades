"""Testes do adaptador do TransfereGov.

Até 12/09/2026 este adaptador — que produz 87% do catálogo multifonte — não
tinha teste nenhum. Foi assim que o prazo dentro do id passou despercebido.
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from funding_intelligence.adapters.transferegov import TransferegovAdapter, identidade_janela


def janela(**over):
    base = {
        "id": "a1b2c3d4e5f6",
        "programa": "Ação 00SX - Apoio a Projetos de Desenvolvimento Sustentável",
        "orgao": "MINISTÉRIO DA INTEGRAÇÃO E DO DESENVOLVIMENTO REGIONAL",
        "natureza": "Administração Pública Municipal",
        "canal": "proposta",
        "abre": "2026-08-01",
        "fecha": "2026-09-30",
        "codigos": ["5300020260001", "5300020260002"],
        "temas": [],
    }
    base.update(over)
    return base


def coletar(*itens):
    payload = {"versao": "1.1", "uf": "PB", "origem": {"defasada": False}, "oportunidades": list(itens)}
    with tempfile.TemporaryDirectory() as pasta:
        arquivo = Path(pasta) / "v1.json"
        arquivo.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return TransferegovAdapter(fixture=str(arquivo), today=date(2026, 9, 12)).collect()


class IdentidadeTests(unittest.TestCase):
    def test_prorrogacao_mantem_o_mesmo_id(self):
        # O defeito corrigido. Antes, o id vinha do id do v1.1, que é
        # sha1(programa|órgão|natureza|canal|fecha): mudar o prazo mudava o id,
        # e a prorrogação virava janela encerrada mais janela nova.
        antes = coletar(janela(id="aaaaaaaaaaaa", fecha="2026-09-30"))[0]
        depois = coletar(janela(id="bbbbbbbbbbbb", fecha="2026-10-31"))[0]
        self.assertEqual(antes["id"], depois["id"])
        self.assertNotEqual(antes["dates"]["deadline"], depois["dates"]["deadline"])

    def test_o_id_do_v1_nao_entra_na_identidade(self):
        self.assertEqual(
            identidade_janela(janela(id="aaaaaaaaaaaa")),
            identidade_janela(janela(id="zzzzzzzzzzzz")),
        )

    def test_renomear_o_programa_nao_cria_janela_nova(self):
        self.assertEqual(
            identidade_janela(janela(programa="Nome antigo")),
            identidade_janela(janela(programa="Nome corrigido pelo órgão")),
        )

    def test_natureza_diferente_e_janela_diferente(self):
        # O mesmo programa costuma abrir uma janela por natureza de proponente.
        self.assertNotEqual(
            identidade_janela(janela(natureza="Administração Pública Municipal")),
            identidade_janela(janela(natureza="Organização da Sociedade Civil")),
        )

    def test_canal_diferente_e_janela_diferente(self):
        self.assertNotEqual(
            identidade_janela(janela(canal="proposta")),
            identidade_janela(janela(canal="emenda")),
        )

    def test_a_ordem_dos_codigos_nao_importa(self):
        self.assertEqual(
            identidade_janela(janela(codigos=["2", "1"])),
            identidade_janela(janela(codigos=["1", "2"])),
        )

    def test_formato_do_id_no_catalogo(self):
        item = coletar(janela())[0]
        ident = identidade_janela(janela())
        self.assertEqual(f"transferegov-{ident}-proposta", item["id"])


class ColisaoTests(unittest.TestCase):
    def test_identidade_duplicada_falha_alto(self):
        # `pipeline.deduplicate` descartaria o repetido em silêncio, e uma
        # janela de financiamento sumiria do catálogo.
        with self.assertRaises(ValueError) as ctx:
            coletar(
                janela(programa="Primeira", fecha="2026-09-30"),
                janela(programa="Segunda", fecha="2026-10-15"),
            )
        self.assertIn("duplicada", str(ctx.exception))

    def test_janelas_distintas_convivem(self):
        itens = coletar(
            janela(natureza="Administração Pública Municipal"),
            janela(natureza="Organização da Sociedade Civil"),
        )
        self.assertEqual(2, len({i["id"] for i in itens}))


class ComportamentoPreservadoTests(unittest.TestCase):
    def test_natureza_continua_virando_tipo_de_proponente(self):
        municipal = coletar(janela(natureza="Administração Pública Municipal"))[0]
        osc = coletar(janela(natureza="Organização da Sociedade Civil"))[0]
        self.assertEqual(["municipio"], municipal["eligibility"]["organization_types"])
        self.assertEqual(["osc"], osc["eligibility"]["organization_types"])

    def test_prazo_e_uf_seguem_do_v1(self):
        item = coletar(janela(fecha="2026-11-20"))[0]
        self.assertEqual("2026-11-20", item["dates"]["deadline"])
        self.assertEqual(["PB"], item["eligibility"]["geography"])


if __name__ == "__main__":
    unittest.main()
