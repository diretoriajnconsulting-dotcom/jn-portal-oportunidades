#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radar de Oportunidades TransfereGov - Ponte Estruturacao de Projetos
Download e processamento analitico com DuckDB para a carteira de fomento (PB).
"""

import argparse
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from datetime import date, datetime

import duckdb

BASE_URL = "https://api-publica.transferegov.gestao.gov.br/downloads"
DATA_DIR = "dados_siconv"
OUTPUT_DIR = "public/data"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

KEYWORDS_CARTEIRA = {
    "Regularização Fundiária": [
        "regularização fundiária", "reurb", "fundiária", "matrícula",
        "cartório", "assentamento urbano", "núcleo urbano informal"
    ],
    "ATHIS / Habitação": [
        "athis", "habitação", "moradia", "interesse social", "minha casa",
        "melhorias habitacionais", "assistência técnica"
    ],
    "Socioassistencial (MROSC)": [
        "assistência social", "socioassistencial", "mrosc", "cras", "creas",
        "vulnerabilidade", "idoso", "criança", "adolescente",
        "segurança alimentar", "cozinha comunitária"
    ],
    "Cultura": [
        "cultura", "patrimônio", "preservação", "acervo", "audiovisual",
        "artes", "museu", "memória", "aldir blanc", "pnab"
    ],
    "Inovação": [
        "inovação", "tecnologia", "transformação digital", "p&d", "ict",
        "pesquisa", "startup", "hub", "ciência", "digital"
    ]
}


def categorizar_programa(nome_prog: str) -> str:
    texto = (nome_prog or "").lower()
    for categoria, palavras_chave in KEYWORDS_CARTEIRA.items():
        if any(palavra in texto for palavra in palavras_chave):
            return categoria
    return "Multisetorial / Geral"


def baixar_e_extrair(base_name: str) -> bool:
    candidatos = [f"{base_name}.zip", f"{base_name}.csv.zip"]

    for filename in candidatos:
        url = f"{BASE_URL}/{filename}"
        zip_dest = os.path.join(DATA_DIR, filename)
        print(f"-> Tentando download de {url}...", flush=True)

        try:
            requisicao = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(requisicao, timeout=120) as response, open(
                zip_dest, "wb"
            ) as arquivo_saida:
                shutil.copyfileobj(response, arquivo_saida)

            print(
                f"✓ Download concluído: {filename} "
                f"({os.path.getsize(zip_dest)} bytes)",
                flush=True,
            )
            with zipfile.ZipFile(zip_dest, "r") as arquivo_zip:
                arquivo_zip.extractall(DATA_DIR)
            print(f"✓ Arquivos extraídos com sucesso em {DATA_DIR}", flush=True)
            return True
        except Exception as erro:
            print(f"Aviso: Não foi possível obter {filename}: {erro}", flush=True)
            if os.path.exists(zip_dest):
                os.remove(zip_dest)

    print(
        f"Atenção: Não foi possível baixar {base_name} da origem governamental.",
        flush=True,
    )
    return False


def encontrar_csv_programa() -> str | None:
    candidatos = [
        os.path.join(DATA_DIR, "siconv_programa.csv"),
        os.path.join(DATA_DIR, "siconv_programa.csv.zip"),
        os.path.join(DATA_DIR, "siconv_programa.zip"),
    ]
    return next((caminho for caminho in candidatos if os.path.isfile(caminho)), None)


def processar_radar(uf: str = "PB") -> None:
    hoje = datetime.now().date()
    hoje_str = hoje.strftime("%Y-%m-%d")
    csv_path = encontrar_csv_programa()

    if not csv_path:
        print(
            "Erro: Arquivo siconv_programa não encontrado para processamento.",
            file=sys.stderr,
        )
        sys.exit(1)

    con = duckdb.connect()
    try:
        query = f"""
        WITH programas AS (
            SELECT
                COD_PROGRAMA,
                NOME_PROGRAMA,
                DESC_ORGAO_SUP_PROGRAMA,
                SIT_PROGRAMA,
                TRY_STRPTIME(NULLIF(DT_PROG_INI_RECEB_PROP, ''), '%d/%m/%Y') AS dt_ini_prop,
                TRY_STRPTIME(NULLIF(DT_PROG_FIM_RECEB_PROP, ''), '%d/%m/%Y') AS dt_fim_prop,
                TRY_STRPTIME(NULLIF(DT_PROG_INI_EMENDA_PAR, ''), '%d/%m/%Y') AS dt_ini_emenda,
                TRY_STRPTIME(NULLIF(DT_PROG_FIM_EMENDA_PAR, ''), '%d/%m/%Y') AS dt_fim_emenda
            FROM read_csv('{csv_path}', delim=';', header=True, all_varchar=True)
            WHERE SIT_PROGRAMA IN ('DISPONIBILIZADO', 'CADASTRADO')
              AND (
                  TRY_STRPTIME(NULLIF(DT_PROG_FIM_RECEB_PROP, ''), '%d/%m/%Y') >= DATE '{hoje_str}'
                  OR TRY_STRPTIME(NULLIF(DT_PROG_FIM_EMENDA_PAR, ''), '%d/%m/%Y') >= DATE '{hoje_str}'
              )
        )
        SELECT
            NOME_PROGRAMA,
            DESC_ORGAO_SUP_PROGRAMA AS orgao,
            SIT_PROGRAMA AS status,
            MIN(dt_fim_prop) AS prazo_proposta,
            MIN(dt_fim_emenda) AS prazo_emenda,
            STRING_AGG(DISTINCT CAST(COD_PROGRAMA AS VARCHAR), ', ') AS codigos_programa,
            COUNT(COD_PROGRAMA) AS qtd_codigos
        FROM programas
        GROUP BY NOME_PROGRAMA, DESC_ORGAO_SUP_PROGRAMA, SIT_PROGRAMA
        ORDER BY COALESCE(prazo_proposta, prazo_emenda) ASC;
        """

        print("Executando consulta analítica via DuckDB...", flush=True)
        dataframe = con.execute(query).df()
    finally:
        con.close()

    print(
        f"Total de programas com janelas abertas encontrados: {len(dataframe)}",
        flush=True,
    )
    lista_oportunidades = []
    for _, linha in dataframe.iterrows():
        prazo_proposta = linha["prazo_proposta"]
        prazo_ativo = (
            prazo_proposta
            if prazo_proposta is not None and prazo_proposta >= hoje
            else linha["prazo_emenda"]
        )

        if prazo_ativo is not None:
            prazo_date = prazo_ativo if isinstance(prazo_ativo, date) else prazo_ativo.date()
            dias_restantes = (prazo_date - hoje).days
            prazo_formatado = prazo_date.strftime("%d/%m/%Y")
        else:
            dias_restantes = 999
            prazo_formatado = "A definir"

        codigos = str(linha["codigos_programa"])
        lista_oportunidades.append({
            "id": codigos.split(",")[0].strip(),
            "codigo_programa": codigos,
            "nome": linha["NOME_PROGRAMA"],
            "orgao": linha["orgao"] or "Órgão Concedente Federal",
            "status": linha["status"],
            "carteira": categorizar_programa(linha["NOME_PROGRAMA"]),
            "publico_alvo": "Administração Pública / OSCs / Consórcios",
            "prazo_fim": prazo_formatado,
            "dias_restantes": dias_restantes,
            "link_transferegov": "https://portal.transferegov.sistema.gov.br/",
        })

    estado_path = os.path.join(OUTPUT_DIR, "estado.json")
    programas_anteriores = set()
    if os.path.exists(estado_path):
        try:
            with open(estado_path, "r", encoding="utf-8") as arquivo:
                programas_anteriores = {
                    programa.get("nome")
                    for programa in json.load(arquivo).get("programas", [])
                }
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    novos_programas = [
        programa
        for programa in lista_oportunidades
        if programa["nome"] not in programas_anteriores
    ]
    resultado_final = {
        "metadata": {
            "ultima_atualizacao": datetime.now().isoformat(),
            "data_formatada": hoje.strftime("%d/%m/%Y"),
            "uf": uf,
            "total_programas_abertos": len(lista_oportunidades),
            "total_urgentes": sum(
                programa["dias_restantes"] <= 15 for programa in lista_oportunidades
            ),
            "novos_nesta_execucao": len(novos_programas),
        },
        "programas": lista_oportunidades,
    }

    for nome_arquivo in ("oportunidades.json", "estado.json"):
        with open(os.path.join(OUTPUT_DIR, nome_arquivo), "w", encoding="utf-8") as arquivo:
            json.dump(resultado_final, arquivo, ensure_ascii=False, indent=2)

    print(
        f"✓ Processamento concluído: {len(lista_oportunidades)} programas abertos mapeados.",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Atualiza o radar TransfereGov.")
    parser.add_argument("--uf", default="PB", help="UF usada nos metadados da saída.")
    argumentos = parser.parse_args()
    baixar_e_extrair("siconv_programa")
    processar_radar(argumentos.uf)