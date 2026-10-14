"""Tabela das 27 UFs do PPSUS: quem é a FAP, a SES e por onde o adaptador lê.

Cada linha diz qual leitor (`leitor`) serve à FAP da UF. Os leitores são por
plataforma, não por FAP: a mudança de site de uma FAP vira uma mudança de linha
aqui, sem mexer na regra de prazo, que é uma só (`ppsus.py`).

Medições do estudo de 14/10/2026 (`radar-fontes-saude/estudo/ppsus_*.md`):

- AP: o `robots.txt` da FAPEAP responde 403. Não se lê a FAPEAP; a UF só entra
  pela sentinela do SISC&T (o PDF do edital baixado na sessão pública).
- MT: o `robots.txt` da FAPEMAT proíbe todo robô. Não se lê a FAPEMAT; a UF entra
  pela sentinela e, na carga histórica, pelo PDF que a SES-MT publicou.
- CE: o site institucional da FUNCAP tem certificado vencido e a API do
  `www.ce.gov.br` deu 403. A leitura é pela plataforma Montenegro, com certificado
  válido. Nunca se desliga a verificação de TLS.
- RN: o site da FAPERN responde 503 (suspensão eleitoral até 25/10/2026). O 503
  vira UF com falha (`failed_scopes`), sem derrubar a coleta.
- FAPEAM: o `robots.txt` proíbe `/wp-json/` e a busca; a leitura é pelo RSS.
- FAPEMIG: a API headless deu 401 e o `robots.txt` proíbe `/pt/*`; a leitura é
  pelo sitemap das chamadas e pela página SSR.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConfigUF:
    uf: str
    fap: str
    ses: str
    # Leitor por plataforma (ver `PpsusAdapter._leitores`):
    #   wp_media   API de mídia do WordPress (`/wp-json/wp/v2/media?search=ppsus`)
    #   rss        feed RSS de editais com o texto integral (FAPEAM)
    #   facepe     busca HTML do WordPress 3.8 da FACEPE
    #   fapitec    busca HTML da FAPITEC (REST fechada, 401)
    #   montenegro tabela de editais da plataforma Montenegro (FUNCAP)
    #   fapt       índices de editais do portal do Tocantins e pasta da edição
    #   fapdf      página fixa do PPSUS na FAPDF (Liferay)
    #   fapes      páginas de editais abertos e encerrados da FAPES (ASP.NET)
    #   fapemig    sitemap das chamadas e página SSR da FAPEMIG
    #   fapesp     página do programa na FAPESP e página da chamada
    #   faperj     lista de editais da FAPERJ, com o cronograma no próprio HTML
    #   fappr      página de programas por ano da Fundação Araucária (Drupal)
    #   matriz     CMS Matriz da FAPERGS (página do ano e serviço JSON da lista)
    #   links      página HTML qualquer, filtrando links do PPSUS (FAPERN)
    #   sisct      sem leitura da FAP: só a sentinela do SISC&T (AP, MT)
    leitor: str
    url: str | None = None
    # Executora "serviços de saúde" além da lista nacional (IES, ICT, centro de
    # P&D, empresa pública). Na BA, o edital 03/2025 aceita; vira `hospital`.
    aceita_servico_saude: bool = False
    # Documentos de edições passadas para a carga histórica, quando a FAP não pode
    # ser lida (robots, site fora do ar). Pares (url, título). Só entram com
    # `historical=True`; o número e o prazo saem do próprio PDF.
    sementes: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    observacao: str = ""


UFS: dict[str, ConfigUF] = {c.uf: c for c in (
    # Norte
    ConfigUF("AC", "FAPAC", "SESACRE", "wp_media", "https://fapac.ac.gov.br"),
    ConfigUF("AM", "FAPEAM", "SES-AM", "rss", "https://www.fapeam.am.gov.br/editais/feed/",
             observacao="robots.txt proíbe /wp-json/ e a busca; o RSS traz o texto integral"),
    ConfigUF("AP", "FAPEAP", "SESA-AP", "sisct",
             observacao="robots.txt da FAPEAP respondeu 403 em 14/10/2026; só a sentinela do SISC&T"),
    ConfigUF("PA", "FAPESPA", "SESPA", "wp_media", "https://www.fapespa.pa.gov.br"),
    ConfigUF("RO", "FAPERO", "SESAU-RO", "wp_media", "https://rondonia.ro.gov.br",
             observacao="multisite do governo; os PDFs da FAPERO ficam na mídia do site principal"),
    ConfigUF("RR", "FAPERR", "SESAU-RR", "wp_media", "https://faperr.rr.gov.br"),
    ConfigUF("TO", "FAPT", "SES-TO", "fapt", "https://www.to.gov.br/fapt/"),
    # Nordeste
    ConfigUF("AL", "FAPEAL", "SESAU-AL", "wp_media", "https://www.fapeal.br"),
    ConfigUF("BA", "FAPESB", "SESAB", "wp_media", "https://www.fapesb.ba.gov.br",
             aceita_servico_saude=True,
             observacao="todo upload fica em /wp-content/uploads/2019/08/; a data sai da API"),
    ConfigUF("CE", "FUNCAP", "SESA-CE", "montenegro", "https://montenegro.funcap.ce.gov.br/sugba/editais/",
             observacao="site institucional com certificado vencido; lê-se só a plataforma Montenegro"),
    ConfigUF("MA", "FAPEMA", "SES-MA", "wp_media", "https://www.fapema.br"),
    # PB fica fora: decisão do supervisor (14/10/2026). O adaptador `fapesq` é dono
    # do PPSUS da Paraíba (emite `fapesq-2-2025` com Decit/MS, CNPq e SES-PB e o tema
    # `pesquisa_sus`). Aqui a FAPESQ não é lida e nenhum item sai com geography PB,
    # para a mesma chamada não aparecer duas vezes na tela. Ver `UFS_FORA`.
    ConfigUF("PE", "FACEPE", "SES-PE", "facepe", "https://www.facepe.br/?s=ppsus"),
    ConfigUF("PI", "FAPEPI", "SESAPI", "wp_media", "https://www.fapepi.pi.gov.br"),
    ConfigUF("RN", "FAPERN", "SESAP-RN", "links", "https://fapern.rn.gov.br/",
             sementes=((
                 "https://funpec.br/wp-content/uploads/2025/12/FAPERN-%E2%80%94-25-2025.pdf",
                 "Edital FAPERN 25/2025 – Chamada PPSUS (cópia publicada pela FUNPEC/UFRN)",
             ),),
             observacao="503 de suspensão eleitoral até 25/10/2026; a carga histórica lê a cópia da FUNPEC"),
    ConfigUF("SE", "FAPITEC", "SES-SE", "fapitec", "https://fapitec.se.gov.br/?s=ppsus",
             observacao="REST do WordPress fechada (401); a busca HTML funciona"),
    # Centro-Oeste
    ConfigUF("DF", "FAPDF", "SES-DF", "fapdf", "https://www.fap.df.gov.br/edital-ppsus"),
    ConfigUF("GO", "FAPEG", "SES-GO", "wp_media", "https://goias.gov.br/fapeg"),
    ConfigUF("MT", "FAPEMAT", "SES-MT", "sisct",
             sementes=((
                 "https://www.saude.mt.gov.br/storage/files/E1McYXAMYDS5PP0CqrckEecDlj73C1PwWPmMfBGs.pdf",
                 "Edital FAPEMAT 004/2025 – PPSUS (publicado pela SES-MT)",
             ),),
             observacao="robots.txt da FAPEMAT proíbe robôs; sentinela do SISC&T e, no histórico, o PDF da SES-MT"),
    ConfigUF("MS", "FUNDECT", "SES-MS", "wp_media", "https://www.fundect.ms.gov.br"),
    # Sudeste
    ConfigUF("ES", "FAPES", "SESA-ES", "fapes", "https://fapes.es.gov.br/editais-abertos-pesquisa-4"),
    ConfigUF("MG", "FAPEMIG", "SES-MG", "fapemig", "https://fapemig.br/__sitemap__/calls.xml",
             observacao="API headless com 401 e robots.txt proibindo /pt/*; sitemap e página SSR"),
    ConfigUF("RJ", "FAPERJ", "SES-RJ", "faperj", "https://www.faperj.br/?id=28.5.7",
             observacao="o número do SISC&T divergiu do edital (10/2025 × 11/2025); vale o da FAPERJ"),
    ConfigUF("SP", "FAPESP", "SES-SP", "fapesp", "https://fapesp.br/ppsus"),
    # Sul
    ConfigUF("PR", "Fundação Araucária", "SESA-PR", "fappr", "https://www.fappr.pr.gov.br/Pagina/Programas-{ano}"),
    ConfigUF("RS", "FAPERGS", "SES-RS", "matriz", "https://fapergs.rs.gov.br/{ano}"),
    ConfigUF("SC", "FAPESC", "SES-SC", "wp_media", "https://fapesc.sc.gov.br"),
)}

# UFs que o PPSUS não emite, com o motivo. A sentinela do SISC&T ainda reconhece a
# sigla (para não confundir com outra chamada), mas a UF não é lida nem emitida.
UFS_FORA: dict[str, str] = {
    "PB": "o PPSUS da Paraíba vem pelo adaptador fapesq (decisão do supervisor, 14/10/2026)",
}

# Rótulo da UF no título do SISC&T ("RJ - EDITAL FAPERJ ..."): as 27 siglas.
SIGLAS_UF = frozenset(UFS) | frozenset(UFS_FORA)
