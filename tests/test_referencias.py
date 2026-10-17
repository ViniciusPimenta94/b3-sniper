import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import atualizar_precos
import referencias as ref


HOJE = date(2026, 10, 16)
RAIZ = Path(__file__).resolve().parents[1]


def _graham(**extras):
    bloco = {
        "metodo": "graham",
        "lpa": 4.23,
        "vpa": 18.83,
        "origem": "Fundamentus",
        "data_fundamentos": "2026-10-14",
    }
    bloco.update(extras)
    return bloco


def _ativo_graham(**extras):
    ativo = {
        "ticker": "ITUB4.SA",
        "nome_amigavel": "Itaú",
        "preco_justo": _graham(),
        "preco_teto": {"metodo": "desconto_sobre_justo", "desconto_pct": 0},
        "faixa": None,
    }
    ativo.update(extras)
    return ativo


class FormulasTest(unittest.TestCase):
    def test_graham_bazin_manual_fii_e_desconto(self):
        graham = math.sqrt(22.5 * 4.23 * 18.83)
        self.assertAlmostEqual(ref.calcular_justo(_graham()), graham)

        bazin = ref.calcular_justo(
            {
                "metodo": "bazin",
                "dividendos_12m": 4.5901,
                "yield_alvo": 0.08,
                "origem": "Fundamentus",
                "data_fundamentos": "2026-10-14",
            }
        )
        self.assertAlmostEqual(bazin, 4.5901 / 0.08)

        manual = ref.calcular_justo(
            {
                "metodo": "manual",
                "preco_manual": 33.5,
                "origem": "planilha",
                "data_fundamentos": "2026-10-14",
            }
        )
        self.assertAlmostEqual(manual, 33.5)

        fii = ref.calcular_justo(
            {
                "metodo": "fii_vp",
                "vpa": 112,
                "origem": "relatório",
                "data_fundamentos": "2026-10-14",
            }
        )
        self.assertAlmostEqual(fii, 112)

        ativo = _ativo_graham()
        ativo["preco_teto"] = {"metodo": "desconto_sobre_justo", "desconto_pct": 0.1}
        calculado = ref.calcular_referencias(ativo, HOJE, 45)
        self.assertAlmostEqual(calculado.teto, graham * 0.9)
        self.assertTrue(calculado.emitir_teto)

    def test_maxima_de_52_semanas(self):
        ativo = {
            "ticker": "IVVB11.SA",
            "preco_justo": None,
            "preco_teto": {
                "metodo": "max_52_semanas",
                "desconto_pct": 0.05,
                "maxima_52s": 100,
                "data_cotacao": "2026-10-16",
                "origem": "Yahoo Finance",
            },
            "faixa": None,
        }
        calculado = ref.calcular_referencias(ativo, HOJE, 45)
        self.assertAlmostEqual(calculado.teto, 95)
        self.assertIsNone(calculado.justo)
        self.assertTrue(calculado.emitir_teto)
        self.assertEqual(calculado.problemas_teto, [])

    def test_desconto_zero_nao_emite_teto(self):
        calculado = ref.calcular_referencias(_ativo_graham(), HOJE, 45)
        self.assertAlmostEqual(calculado.teto, calculado.justo)
        self.assertFalse(calculado.emitir_teto)


class ValidacaoTest(unittest.TestCase):
    def test_valor_invalido_origem_ausente_e_data_vencida(self):
        sem_lpa = _ativo_graham()
        sem_lpa["preco_justo"] = _graham(lpa=0)
        problemas = ref.calcular_referencias(sem_lpa, HOJE, 45).problemas_justo
        self.assertTrue(any("LPA" in item for item in problemas))

        sem_origem = _ativo_graham()
        sem_origem["preco_justo"] = _graham(origem=" ")
        problemas = ref.calcular_referencias(sem_origem, HOJE, 45).problemas_justo
        self.assertTrue(any("origem" in item for item in problemas))

        vencido = _ativo_graham()
        vencido["preco_justo"] = _graham(data_fundamentos=(HOJE - timedelta(days=46)).isoformat())
        problemas = ref.calcular_referencias(vencido, HOJE, 45).problemas_justo
        self.assertTrue(any("vencida" in item for item in problemas))

        no_limite = _ativo_graham()
        no_limite["preco_justo"] = _graham(data_fundamentos=(HOJE - timedelta(days=45)).isoformat())
        self.assertEqual(ref.calcular_referencias(no_limite, HOJE, 45).problemas_justo, [])

        futuro = _ativo_graham()
        futuro["preco_justo"] = _graham(data_fundamentos="2026-10-17")
        problemas = ref.calcular_referencias(futuro, HOJE, 45).problemas_justo
        self.assertTrue(any("futuro" in item for item in problemas))

    def test_yield_e_faixa_invalidos(self):
        ativo = _ativo_graham()
        ativo["preco_justo"] = {
            "metodo": "bazin",
            "dividendos_12m": 1,
            "yield_alvo": 0,
            "origem": "teste",
            "data_fundamentos": "2026-10-14",
        }
        problemas = ref.calcular_referencias(ativo, HOJE, 45).problemas_justo
        self.assertTrue(any("yield_alvo" in item for item in problemas))

        ativo["faixa"] = {"preco_minimo": 10, "preco_maximo": 9}
        self.assertTrue(ref.calcular_referencias(ativo, HOJE, 45).problemas_faixa)

    def test_fii_sem_data_nao_calcula_alerta(self):
        ativo = {
            "ticker": "VISC11.SA",
            "nome_amigavel": "Vinci",
            "preco_justo": {
                "metodo": "fii_vp",
                "vpa": 112,
                "origem": "config.json (vpa_fallback)",
                "data_fundamentos": None,
            },
            "preco_teto": {"metodo": "desconto_sobre_justo", "desconto_pct": 0},
            "faixa": None,
        }
        episodios: set[str] = set()
        sinais = ref.avaliar_ativo(
            ativo, 100, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=episodios
        )
        self.assertEqual(len(sinais), 1)
        self.assertEqual(sinais[0].tipo, "pendencia")
        self.assertEqual(sinais[0].categoria, "justo")
        self.assertFalse(any(item.tipo == "alerta" for item in sinais))


class AlertasTest(unittest.TestCase):
    def test_justo_e_teto_independentes_sem_duplicar(self):
        ativo = _ativo_graham()
        ativo["preco_teto"] = {"metodo": "desconto_sobre_justo", "desconto_pct": 0.1}
        calculado = ref.calcular_referencias(ativo, HOJE, 45)
        episodios: set[str] = set()
        comum = dict(hoje=HOJE, validade_dias=45, margem_pct=0, episodios=episodios)

        meio = ref.avaliar_ativo(ativo, (calculado.teto + calculado.justo) / 2, **comum)
        self.assertEqual(meio, [])
        self.assertTrue(any("Justo" in linha for linha in ref.linhas_status(calculado)))

        no_teto = ref.avaliar_ativo(ativo, calculado.teto, **comum)
        self.assertEqual([item.categoria for item in no_teto], ["teto"])

        repetido = ref.avaliar_ativo(ativo, calculado.teto - 0.01, **comum)
        self.assertEqual(repetido, [])

        acima = ref.avaliar_ativo(ativo, calculado.justo + 1, **comum)
        self.assertEqual(acima, [])
        self.assertFalse(any(chave.startswith("alerta|") for chave in episodios))

        de_novo = ref.avaliar_ativo(ativo, calculado.teto - 0.01, **comum)
        self.assertEqual([item.categoria for item in de_novo], ["teto"])

    def test_desconto_zero_nao_duplica_alerta(self):
        episodios: set[str] = set()
        sinais = ref.avaliar_ativo(
            _ativo_graham(),
            10,
            hoje=HOJE,
            validade_dias=45,
            margem_pct=0,
            episodios=episodios,
        )
        self.assertEqual(sinais, [])
        self.assertFalse(any("|justo|" in chave or "|teto|" in chave for chave in episodios))
        linhas = ref.linhas_status(ref.calcular_referencias(_ativo_graham(), HOJE, 45))
        self.assertTrue(any(linha.startswith("Justo (") for linha in linhas))

    def test_transicao_de_faixa_e_margem(self):
        ativo = {
            "ticker": "B3SA3.SA",
            "nome_amigavel": "B3",
            "preco_justo": None,
            "preco_teto": None,
            "faixa": {"preco_minimo": 19, "preco_maximo": 20.5},
        }
        episodios: set[str] = set()
        comum = dict(hoje=HOJE, validade_dias=45, margem_pct=0, episodios=episodios)

        self.assertEqual(ref.avaliar_ativo(ativo, 18.9, **comum), [])
        dentro = ref.avaliar_ativo(ativo, 19, **comum)
        self.assertEqual([item.categoria for item in dentro], ["faixa"])
        self.assertEqual(ref.avaliar_ativo(ativo, 20.5, **comum), [])
        self.assertEqual(ref.avaliar_ativo(ativo, 21, **comum), [])
        retorno = ref.avaliar_ativo(ativo, 20, **comum)
        self.assertEqual([item.categoria for item in retorno], ["faixa"])

        com_margem = _ativo_graham()
        com_margem["preco_teto"] = {"metodo": "desconto_sobre_justo", "desconto_pct": 0.25}
        calculado_margem = ref.calcular_referencias(com_margem, HOJE, 45)
        sinais = ref.avaliar_ativo(
            com_margem,
            calculado_margem.teto * 1.10,
            hoje=HOJE,
            validade_dias=45,
            margem_pct=10,
            episodios=set(),
        )
        self.assertEqual([item.categoria for item in sinais], ["teto"])
        fora = ref.avaliar_ativo(
            com_margem,
            calculado_margem.teto * 1.10 + 0.01,
            hoje=HOJE,
            validade_dias=45,
            margem_pct=10,
            episodios=set(),
        )
        self.assertEqual(fora, [])

    def test_pendencia_nao_repete_e_libera_o_alerta_quando_corrigida(self):
        ativo = _ativo_graham()
        ativo["preco_justo"] = _graham(data_fundamentos="2020-01-01")
        episodios: set[str] = set()
        comum = dict(hoje=HOJE, validade_dias=45, margem_pct=0, episodios=episodios)
        primeiro = ref.avaliar_ativo(ativo, 10, **comum)
        self.assertEqual(primeiro[0].tipo, "pendencia")
        self.assertEqual(ref.avaliar_ativo(ativo, 10, **comum), [])

        ativo["preco_justo"] = _graham()
        ativo["preco_teto"] = {"metodo": "desconto_sobre_justo", "desconto_pct": 0.25}
        corrigido = ref.avaliar_ativo(ativo, 10, **comum)
        self.assertEqual([(item.tipo, item.categoria) for item in corrigido], [("alerta", "teto")])
        self.assertFalse(any(chave.startswith("pendencia|") for chave in episodios))

    def test_faixa_independente_de_justo_invalido(self):
        ativo = _ativo_graham()
        ativo["preco_justo"] = _graham(lpa=-1)
        ativo["faixa"] = {"preco_minimo": 19, "preco_maximo": 20.5}
        sinais = ref.avaliar_ativo(
            ativo, 20, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=set()
        )
        categorias = {item.categoria: item.tipo for item in sinais}
        self.assertEqual(categorias["justo"], "pendencia")
        self.assertEqual(categorias["faixa"], "alerta")


class PersistenciaEArquivoTest(unittest.TestCase):
    def test_episodios_e_maxima_em_arquivo_temporario(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "estado.json"
            ref.salvar_episodios({"alerta|justo|ABC|graham|1.00000000"}, caminho)
            self.assertEqual(
                ref.carregar_episodios(caminho),
                {"alerta|justo|ABC|graham|1.00000000"},
            )
            ref.salvar_episodios(set(), caminho)
            self.assertEqual(ref.carregar_episodios(caminho), set())

            dados = {
                "ativos": [
                    {
                        "ticker": "IVVB11.SA",
                        "preco_teto": {
                            "metodo": "max_52_semanas",
                            "desconto_pct": 0.05,
                            "maxima_52s": None,
                            "data_cotacao": None,
                            "origem": "Yahoo Finance",
                        },
                    },
                    {"ticker": "ITUB4.SA", "preco_justo": _graham(), "preco_teto": None, "faixa": None},
                ]
            }
            alterados = ref.atualizar_maximas(dados, lambda ticker: 200 if ticker.startswith("IVVB") else None, HOJE)
            self.assertEqual(alterados, 1)
            self.assertEqual(dados["ativos"][0]["preco_teto"]["maxima_52s"], 200)
            self.assertEqual(dados["ativos"][0]["preco_teto"]["data_cotacao"], "2026-10-16")
            self.assertEqual(dados["ativos"][1]["preco_justo"]["lpa"], 4.23)
            self.assertEqual(dados["ativos"][0]["preco_teto"]["origem"], "Yahoo Finance")

            destino = Path(pasta) / "referencias_precos.json"
            ref.salvar_referencias(dados, destino)
            recarregado = ref.carregar_referencias(destino)
            self.assertEqual(recarregado["ativos"][0]["preco_teto"]["maxima_52s"], 200)

            com_origem = ref.atualizar_maximas(
                dados,
                lambda ticker: (210, "Yahoo Finance (fiftyTwoWeekHigh)") if ticker.startswith("IVVB") else None,
                HOJE,
            )
            self.assertEqual(com_origem, 1)
            self.assertEqual(dados["ativos"][0]["preco_teto"]["maxima_52s"], 210)
            self.assertEqual(dados["ativos"][0]["preco_teto"]["origem"], "Yahoo Finance (fiftyTwoWeekHigh)")
            self.assertEqual(dados["ativos"][0]["preco_teto"]["desconto_pct"], 0.05)

    def test_origem_da_maxima_acompanha_a_fonte_do_yahoo(self):
        class _Serie:
            empty = False
            columns = ["High"]

            def __init__(self, valor):
                self.valor = valor

            def __getitem__(self, coluna):
                self.coluna = coluna
                return self

            def max(self):
                return self.valor

        class _Vazio:
            empty = True

        class _Ticker:
            def __init__(self, info, high=None):
                self.info = info
                self.high = high

            def history(self, period):
                self.periodo = period
                if self.high is None:
                    return _Vazio()
                return _Serie(self.high)

        def ativo():
            return {
                "ticker": "IVVB11.SA",
                "preco_justo": None,
                "preco_teto": {
                    "metodo": "max_52_semanas",
                    "desconto_pct": 0.05,
                    "maxima_52s": 458.10,
                    "data_cotacao": "2026-10-16",
                    "origem": "Status Invest",
                },
                "faixa": {"preco_minimo": 1, "preco_maximo": 2},
            }

        info = _Ticker({"fiftyTwoWeekHigh": 460.5})
        dados = {"ativos": [ativo(), {"ticker": "ITUB4.SA", "preco_justo": _graham(), "preco_teto": None}]}
        resumo = atualizar_precos._atualizar_maximas_coletadas(
            dados, HOJE, abrir=lambda ticker: info
        )
        bloco = dados["ativos"][0]["preco_teto"]
        self.assertEqual(len(resumo.atualizados), 1)
        self.assertEqual(bloco["maxima_52s"], 460.5)
        self.assertEqual(bloco["origem"], atualizar_precos.ORIGEM_FIFTY_TWO_WEEK_HIGH)
        self.assertEqual(bloco["desconto_pct"], 0.05)
        self.assertEqual(dados["ativos"][0]["faixa"]["preco_minimo"], 1)
        self.assertEqual(dados["ativos"][1]["preco_justo"]["lpa"], 4.23)

        historico = _Ticker({}, high=153.37)
        dados_high = {"ativos": [ativo()]}
        atualizar_precos._atualizar_maximas_coletadas(dados_high, HOJE, abrir=lambda ticker: historico)
        self.assertEqual(historico.periodo, "1y")
        self.assertEqual(
            dados_high["ativos"][0]["preco_teto"]["origem"],
            atualizar_precos.ORIGEM_HIGH_UM_ANO,
        )
        self.assertEqual(dados_high["ativos"][0]["preco_teto"]["maxima_52s"], 153.37)
        self.assertEqual(dados_high["ativos"][0]["preco_teto"]["desconto_pct"], 0.05)

        sem_coleta = {"ativos": [ativo()]}
        resumo_vazio = atualizar_precos._atualizar_maximas_coletadas(
            sem_coleta, HOJE, abrir=lambda ticker: _Ticker({})
        )
        self.assertEqual(resumo_vazio.atualizados, [])
        self.assertEqual(sem_coleta["ativos"][0]["preco_teto"]["origem"], "Status Invest")
        self.assertEqual(sem_coleta["ativos"][0]["preco_teto"]["maxima_52s"], 458.10)

    def test_resumo_separa_atualizados_falhas_e_revisao_manual(self):
        class _Ticker:
            def __init__(self, info=None, erro=False):
                self.info = {} if info is None else info
                self.erro = erro

            def history(self, period):
                if self.erro:
                    raise RuntimeError("rede")
                return type("Vazio", (), {"empty": True})()

        def maxima(ticker, valor, origem="Status Invest"):
            return {
                "ticker": ticker,
                "preco_justo": None,
                "preco_teto": {
                    "metodo": "max_52_semanas",
                    "desconto_pct": 0.05,
                    "maxima_52s": valor,
                    "data_cotacao": "2026-10-16",
                    "origem": origem,
                },
                "faixa": None,
            }

        def abrir(ticker):
            if ticker == "FALHA.SA":
                return _Ticker(erro=True)
            if ticker == "INVALIDA.SA":
                return _Ticker({"fiftyTwoWeekHigh": -1})
            if ticker == "IGUAL.SA":
                return _Ticker({"fiftyTwoWeekHigh": 100})
            if ticker == "NOVA.SA":
                return _Ticker({"fiftyTwoWeekHigh": 200})
            raise AssertionError(ticker)

        vencido = {
            "ticker": "TAEE11.SA",
            "preco_justo": _graham(data_fundamentos="2020-01-01"),
            "preco_teto": {"metodo": "desconto_sobre_justo", "desconto_pct": 0.25},
            "faixa": None,
        }
        dados = {
            "validade_dias": 45,
            "ativos": [
                maxima("FALHA.SA", 10),
                maxima("INVALIDA.SA", 11),
                maxima("IGUAL.SA", 100, atualizar_precos.ORIGEM_FIFTY_TWO_WEEK_HIGH),
                maxima("NOVA.SA", 50),
                vencido,
            ],
        }
        antes_falha = dict(dados["ativos"][0]["preco_teto"])
        antes_invalida = dict(dados["ativos"][1]["preco_teto"])
        resumo = atualizar_precos._atualizar_maximas_coletadas(dados, HOJE, abrir=abrir)

        self.assertEqual(resumo.atualizados, ["NOVA.SA: máxima, origem"])
        self.assertTrue(any(item.startswith("FALHA.SA:") and "preservados" in item for item in resumo.falhas))
        self.assertTrue(any(item.startswith("INVALIDA.SA:") and "preservados" in item for item in resumo.falhas))
        self.assertFalse(any(item.startswith("IGUAL.SA:") for item in resumo.atualizados))
        self.assertEqual(dados["ativos"][0]["preco_teto"], antes_falha)
        self.assertEqual(dados["ativos"][1]["preco_teto"], antes_invalida)
        self.assertEqual(dados["ativos"][2]["preco_teto"]["maxima_52s"], 100)
        self.assertEqual(dados["ativos"][3]["preco_teto"]["maxima_52s"], 200)
        self.assertEqual(dados["ativos"][3]["preco_teto"]["desconto_pct"], 0.05)
        self.assertTrue(any(item.startswith("TAEE11.SA:") and "vencida" in item for item in resumo.revisao_manual))
        self.assertFalse(any(item.startswith("NOVA.SA:") for item in resumo.revisao_manual))

        texto = atualizar_precos.formatar_resumo(atualizar_precos.ResumoAtualizacao())
        self.assertIn("Atualizados: nenhum", texto)
        self.assertIn("Falhas: nenhuma", texto)
        self.assertIn("Revisão manual: nenhuma", texto)
        self.assertIn("Fundamentos não são coletados automaticamente", texto)

    def test_arquivo_real_cobre_os_ativos_e_preserva_o_config(self):
        with open(RAIZ / "config.json", encoding="utf-8") as handle:
            config = json.load(handle)
        dados = ref.carregar_referencias(RAIZ / "data" / "referencias_precos.json")
        por_ticker = {item["ticker"]: item for item in dados["ativos"]}

        esperados = {item["ticker"] for item in config["assets"]}
        for categoria in ("venda", "compra", "analise"):
            esperados.update(item["ticker"] for item in config["opportunity_alerts"][categoria])
        self.assertEqual(set(por_ticker), esperados)

        for item in config["opportunity_alerts"]["compra"]:
            bloco = por_ticker[item["ticker"]]["preco_justo"]
            self.assertEqual(bloco["metodo"], "graham")
            self.assertEqual(bloco["lpa"], item["lpa"])
            self.assertEqual(bloco["vpa"], item["vpa"])
            self.assertEqual(bloco["data_fundamentos"], "2026-10-14")
            self.assertTrue(bloco["origem"])

        for item in config["opportunity_alerts"]["analise"]:
            faixa = por_ticker[item["ticker"]]["faixa"]
            self.assertEqual(faixa["preco_minimo"], item["preco_minimo"])
            self.assertEqual(faixa["preco_maximo"], item["preco_maximo"])

        vpa_confirmado = {
            "VISC11.SA": 115.60,
            "XPLG11.SA": 105.03,
            "PVBI11.SA": 104.53,
            "BTHF11.SA": 9.86,
            "CPTS11.SA": 8.65,
        }
        vpa_fallback_config = {
            "VISC11.SA": 112.0,
            "XPLG11.SA": 105.56,
            "PVBI11.SA": 101.5,
            "BTHF11.SA": 9.4,
            "CPTS11.SA": 8.85,
        }
        for item in config["assets"]:
            if item["estrategia"] == "fii_vp":
                bloco = por_ticker[item["ticker"]]["preco_justo"]
                self.assertEqual(bloco["metodo"], "fii_vp")
                self.assertEqual(bloco["vpa"], vpa_confirmado[item["ticker"]])
                self.assertEqual(bloco["origem"], "Status Invest")
                self.assertEqual(bloco["data_fundamentos"], "2026-10-16")
                self.assertEqual(item["parametros"]["vpa_fallback"], vpa_fallback_config[item["ticker"]])
            if item["estrategia"] == "max_52_semanas":
                bloco = por_ticker[item["ticker"]]["preco_teto"]
                self.assertEqual(bloco["metodo"], "max_52_semanas")
                self.assertEqual(bloco["desconto_pct"], 0.05)
                self.assertIsNone(por_ticker[item["ticker"]]["preco_justo"])

        bbse = por_ticker["BBSE3.SA"]["preco_justo"]
        self.assertEqual(bbse["metodo"], "bazin")
        self.assertEqual(bbse["yield_alvo"], 0.08)
        self.assertEqual(bbse["dividendos_12m"], 4.5901)
        self.assertEqual(por_ticker["BBSE3.SA"]["preco_teto"]["desconto_pct"], 0.15)
        self.assertEqual(por_ticker["TIMS3.SA"]["preco_teto"]["desconto_pct"], 0.15)
        for ticker in ("ITUB4.SA", "TAEE11.SA", "TOTS3.SA", "SBSP3.SA", "CMIG4.SA"):
            self.assertEqual(por_ticker[ticker]["preco_teto"]["desconto_pct"], 0.25)
        for ticker in ("VISC11.SA", "XPLG11.SA", "PVBI11.SA", "BTHF11.SA", "CPTS11.SA"):
            self.assertEqual(por_ticker[ticker]["preco_teto"]["desconto_pct"], 0)

        tims = por_ticker["TIMS3.SA"]["preco_justo"]
        self.assertEqual(tims["metodo"], "bazin")
        self.assertEqual(tims["yield_alvo"], 0.06)
        self.assertEqual(tims["dividendos_12m"], 1.4723)

        self.assertEqual(config["assets"][0]["preco_teto"], 413.03)
        self.assertEqual(dados["margem_proximidade_pct"], 0)
        self.assertEqual(dados["validade_dias"], 45)

        hoje = date(2026, 10, 16)
        for ticker in ("ITUB4.SA", "BBSE3.SA", "B3SA3.SA"):
            calculado = ref.calcular_referencias(por_ticker[ticker], hoje, dados["validade_dias"])
            self.assertFalse(calculado.problemas_justo and ticker != "B3SA3.SA")
            if ticker == "B3SA3.SA":
                self.assertEqual(calculado.faixa, (19, 20.5))
                self.assertEqual(calculado.problemas_faixa, [])


class CorrecoesFinaisTest(unittest.TestCase):
    def test_mesma_pendencia_nao_repete_em_dias_seguidos(self):
        ativo = _ativo_graham()
        ativo["preco_justo"] = _graham(data_fundamentos="2020-01-01")
        episodios: set[str] = set()
        primeiro = ref.avaliar_ativo(
            ativo, 10, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=episodios
        )
        segundo = ref.avaliar_ativo(
            ativo,
            10,
            hoje=HOJE + timedelta(days=1),
            validade_dias=45,
            margem_pct=0,
            episodios=episodios,
        )
        self.assertEqual(len(primeiro), 1)
        self.assertEqual(primeiro[0].tipo, "pendencia")
        self.assertEqual(segundo, [])

        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "alertas_estado.json"
            ref.salvar_episodios(episodios, caminho)
            recuperados = ref.carregar_episodios(caminho)
            terceiro = ref.avaliar_ativo(
                ativo,
                12,
                hoje=HOJE + timedelta(days=2),
                validade_dias=45,
                margem_pct=0,
                episodios=recuperados,
            )
        self.assertEqual(terceiro, [])

    def test_pendencia_de_outra_natureza_gera_novo_aviso(self):
        ativo = _ativo_graham()
        ativo["preco_justo"] = _graham(data_fundamentos=None)
        episodios: set[str] = set()
        comum = dict(hoje=HOJE, validade_dias=45, margem_pct=0, episodios=episodios)
        primeiro = ref.avaliar_ativo(ativo, 10, **comum)
        self.assertIn("ausente", primeiro[0].pendencias[0])
        ativo["preco_justo"] = _graham(lpa=0)
        segundo = ref.avaliar_ativo(ativo, 10, **comum)
        self.assertEqual(len(segundo), 1)
        self.assertTrue(any("LPA" in item for item in segundo[0].pendencias))

    def test_status_mostra_valor_suspenso_sem_alerta(self):
        vencido = _ativo_graham()
        vencido["preco_justo"] = _graham(data_fundamentos="2020-01-01")
        calculado = ref.calcular_referencias(vencido, HOJE, 45)
        esperado = math.sqrt(22.5 * 4.23 * 18.83)
        self.assertAlmostEqual(calculado.justo, esperado)
        linhas = ref.linhas_status(calculado)
        self.assertTrue(any(ref.fmt_numero(esperado) in linha for linha in linhas))
        self.assertTrue(any("Pendência do preço justo" in linha and "vencida" in linha for linha in linhas))
        sinais = ref.avaliar_ativo(
            vencido, calculado.justo, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=set()
        )
        self.assertTrue(sinais)
        self.assertTrue(all(item.tipo == "pendencia" for item in sinais))

        dados = ref.carregar_referencias(RAIZ / "data" / "referencias_precos.json")
        visc = next(item for item in dados["ativos"] if item["ticker"] == "VISC11.SA")
        self.assertEqual(visc["preco_justo"]["vpa"], 115.60)
        suspenso = json.loads(json.dumps(visc))
        suspenso["preco_justo"]["data_fundamentos"] = None
        calculado_fii = ref.calcular_referencias(suspenso, HOJE, 45)
        self.assertEqual(calculado_fii.justo, 115.60)
        linhas_fii = ref.linhas_status(calculado_fii)
        self.assertTrue(any("R$ 115,60" in linha for linha in linhas_fii))
        self.assertTrue(any("ausente" in linha for linha in linhas_fii))
        sinais_fii = ref.avaliar_ativo(
            suspenso, 100, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=set()
        )
        self.assertTrue(sinais_fii)
        self.assertTrue(all(item.tipo == "pendencia" for item in sinais_fii))
        self.assertFalse(any(item.tipo == "alerta" for item in sinais_fii))

    def test_tims3_e_preco_acima_nao_geram_venda(self):
        dados = ref.carregar_referencias(RAIZ / "data" / "referencias_precos.json")
        tims = next(item for item in dados["ativos"] if item["ticker"] == "TIMS3.SA")
        self.assertEqual(tims["preco_justo"]["metodo"], "bazin")
        calculado = ref.calcular_referencias(tims, HOJE, 45)
        self.assertAlmostEqual(calculado.justo, 1.4723 / 0.06)
        self.assertAlmostEqual(calculado.teto, calculado.justo * 0.85)
        self.assertTrue(any(linha.startswith("Justo (") for linha in ref.linhas_status(calculado)))
        no_justo = ref.avaliar_ativo(
            tims, calculado.justo, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=set()
        )
        self.assertEqual(no_justo, [])
        no_teto = ref.avaliar_ativo(
            tims, calculado.teto, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=set()
        )
        self.assertEqual([(item.tipo, item.categoria) for item in no_teto], [("alerta", "teto")])
        acima = ref.avaliar_ativo(
            tims,
            calculado.justo + 0.01,
            hoje=HOJE,
            validade_dias=45,
            margem_pct=0,
            episodios=set(),
        )
        self.assertFalse(any(item.tipo == "alerta" for item in acima))

        for ativo in dados["ativos"]:
            sinais = ref.avaliar_ativo(
                ativo, 1_000_000, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=set()
            )
            self.assertFalse(any(item.categoria == "venda" or item.tipo == "alerta" for item in sinais))

    def test_compra_e_faixa_nos_limites(self):
        compra = _ativo_graham()
        compra["preco_teto"] = {"metodo": "desconto_sobre_justo", "desconto_pct": 0.25}
        calculado = ref.calcular_referencias(compra, HOJE, 45)
        self.assertAlmostEqual(calculado.teto, calculado.justo * 0.75)
        self.assertTrue(any(linha.startswith("Justo (") for linha in ref.linhas_status(calculado)))
        entre = ref.avaliar_ativo(
            compra, calculado.teto + 0.01, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=set()
        )
        self.assertEqual(entre, [])
        sinais = ref.avaliar_ativo(
            compra, calculado.teto, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=set()
        )
        self.assertEqual([(item.tipo, item.categoria) for item in sinais], [("alerta", "teto")])

        faixa = {
            "ticker": "B3SA3.SA",
            "nome_amigavel": "B3",
            "preco_justo": None,
            "preco_teto": None,
            "faixa": {"preco_minimo": 19, "preco_maximo": 20.5},
        }
        no_teto = ref.avaliar_ativo(
            faixa, 20.5, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=set()
        )
        self.assertEqual([(item.tipo, item.categoria) for item in no_teto], [("alerta", "faixa")])
        acima_faixa = ref.avaliar_ativo(
            faixa, 20.51, hoje=HOJE, validade_dias=45, margem_pct=0, episodios=set()
        )
        self.assertEqual(acima_faixa, [])


if __name__ == "__main__":
    unittest.main()
