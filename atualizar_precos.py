import math
import sys
from dataclasses import dataclass, field
from datetime import date

import yfinance as yf

from referencias import (
    VALIDADE_PADRAO_DIAS,
    aplicar_maxima_52,
    calcular_referencias,
    carregar_referencias,
    hoje_brasilia,
    linhas_status,
    modo_do_arquivo_config,
    salvar_referencias,
)


def _configurar_console_utf8() -> None:
    """Evita UnicodeEncodeError no terminal Windows (cp1252) ao imprimir emojis."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                pass


_configurar_console_utf8()


ORIGEM_FIFTY_TWO_WEEK_HIGH = "Yahoo Finance (fiftyTwoWeekHigh)"
ORIGEM_HIGH_UM_ANO = "Yahoo Finance (máxima do High em 1 ano)"


def _coletar_maxima_52(ticker: str, abrir=None) -> tuple[tuple[float, str] | None, str | None]:
    """Devolve a coleta ou o motivo da falha, sem alterar o ativo."""
    try:
        consultar = abrir or (lambda simbolo: yf.Ticker(simbolo))
        ticker_obj = consultar(ticker)
        maxima = ticker_obj.info.get("fiftyTwoWeekHigh")
        origem = ORIGEM_FIFTY_TWO_WEEK_HIGH
        if not maxima:
            historico = ticker_obj.history(period="1y")
            origem = ORIGEM_HIGH_UM_ANO
            if not historico.empty and "High" in historico.columns:
                maxima = historico["High"].max()
            else:
                maxima = None
        if maxima is None:
            return None, "máxima indisponível no Yahoo Finance"
        maxima = float(maxima)
        if math.isnan(maxima) or maxima <= 0:
            return None, "máxima inválida"
        return (maxima, origem), None
    except Exception as exc:
        return None, f"falha na consulta ({exc})"


def buscar_maxima_52(ticker: str, abrir=None) -> tuple[float, str] | None:
    """Coleta a máxima e a fonte usada. Não altera LPA, VPA, dividendos nem VPA de FII."""
    coleta, erro = _coletar_maxima_52(ticker, abrir)
    if erro:
        print(f"   ⚠️ Falha ao coletar a máxima de {ticker}: {erro}")
    return coleta


def gravar_maxima_coletada(ativo: dict, coleta: tuple[float, str] | None, data_cotacao: str) -> bool:
    """Grava a máxima e substitui a origem pela fonte desta coleta."""
    if coleta is None:
        return False
    maxima, origem = coleta
    if not isinstance(origem, str) or not origem.strip():
        return False
    if not aplicar_maxima_52(ativo, maxima, data_cotacao):
        return False
    ativo["preco_teto"]["origem"] = origem.strip()
    return True


def _imprimir_ativo(ativo: dict, hoje: date, validade_dias: int) -> None:
    ticker = ativo.get("ticker", "")
    nome = ativo.get("nome_amigavel", ticker)
    calculado = calcular_referencias(ativo, hoje, validade_dias)
    print(f"\n🔍 {nome} ({ticker})")
    for linha in linhas_status(calculado):
        print(f"   {linha}")


@dataclass
class ResumoAtualizacao:
    atualizados: list[str] = field(default_factory=list)
    falhas: list[str] = field(default_factory=list)
    revisao_manual: list[str] = field(default_factory=list)


def _validade(dados: dict) -> int:
    try:
        validade = int(dados.get("validade_dias", VALIDADE_PADRAO_DIAS))
    except (TypeError, ValueError):
        return VALIDADE_PADRAO_DIAS
    return validade if validade >= 1 else VALIDADE_PADRAO_DIAS


def _foto_maxima(bloco: dict) -> tuple:
    return (bloco.get("maxima_52s"), bloco.get("data_cotacao"), bloco.get("origem"))


def _campos_alterados(antes: tuple, depois: tuple) -> list[str]:
    nomes = ("máxima", "data", "origem")
    return [nome for nome, anterior, novo in zip(nomes, antes, depois) if anterior != novo]


def _pendencias_manuais(dados: dict, hoje: date) -> list[str]:
    """Usa as pendências já calculadas por calcular_referencias."""
    validade = _validade(dados)
    linhas = []
    for ativo in dados.get("ativos") or []:
        if not isinstance(ativo, dict):
            continue
        calculado = calcular_referencias(ativo, hoje, validade)
        textos = calculado.problemas_justo + calculado.problemas_teto + calculado.problemas_faixa
        if textos:
            ticker = str(ativo.get("ticker") or "").strip() or "ativo sem ticker"
            linhas.append(f"{ticker}: {'; '.join(textos)}")
    return linhas


def formatar_resumo(resumo: ResumoAtualizacao) -> str:
    """Três listas. Lista vazia é declarada explicitamente."""

    def bloco(titulo: str, itens: list[str], vazio: str) -> str:
        if not itens:
            return f"{titulo}: {vazio}"
        return titulo + ":\n" + "\n".join(f"- {item}" for item in itens)

    return "\n".join(
        [
            bloco("Atualizados", resumo.atualizados, "nenhum"),
            bloco("Falhas", resumo.falhas, "nenhuma"),
            bloco("Revisão manual", resumo.revisao_manual, "nenhuma"),
            "O script atualiza máximas de 52 semanas. Fundamentos não são coletados automaticamente.",
        ]
    )


def _atualizar_maximas_coletadas(
    dados: dict,
    hoje: date,
    abrir=None,
    modo: str = "automatico",
) -> ResumoAtualizacao:
    """Atualiza só a máxima de 52 semanas e a origem da coleta. O desconto permanece."""
    if isinstance(modo, str) and modo.strip().lower() == "manual":
        return ResumoAtualizacao()
    resumo = ResumoAtualizacao()
    data_cotacao = hoje.isoformat()
    for ativo in dados.get("ativos") or []:
        if not isinstance(ativo, dict):
            continue
        bloco = ativo.get("preco_teto") or {}
        if not isinstance(bloco, dict) or bloco.get("metodo") != "max_52_semanas":
            continue
        ticker = str(ativo.get("ticker") or "").strip() or "ativo sem ticker"
        antes = _foto_maxima(bloco)
        coleta, erro = _coletar_maxima_52(ticker, abrir)
        if erro or not gravar_maxima_coletada(ativo, coleta, data_cotacao):
            resumo.falhas.append(
                f"{ticker}: {erro or 'coleta não gravada'}. Dados anteriores preservados."
            )
            continue
        campos = _campos_alterados(antes, _foto_maxima(bloco))
        if campos:
            resumo.atualizados.append(f"{ticker}: {', '.join(campos)}")
    resumo.revisao_manual = _pendencias_manuais(dados, hoje)
    return resumo


def executar_atualizacao() -> None:
    if modo_do_arquivo_config() == "manual":
        print(
            "Modo manual ativo. Preço justo e preço teto não são recalculados. "
            "Nenhuma máxima de 52 semanas foi coletada ou gravada."
        )
        return

    try:
        dados = carregar_referencias()
    except Exception as exc:
        print(f"❌ Erro ao carregar data/referencias_precos.json: {exc}")
        return

    hoje = hoje_brasilia()
    validade = int(dados["validade_dias"])
    print("==================================================================")
    print("🎯 SNIPER: REFERÊNCIAS DE PREÇO")
    print("Fundamentos manuais são preservados. A máxima e a origem da coleta são atualizadas.")
    print("==================================================================")

    try:
        resumo = _atualizar_maximas_coletadas(dados, hoje)
    except Exception as exc:
        print(f"\n❌ Falha ao atualizar máximas de 52 semanas: {exc}")
        return

    for ativo in dados.get("ativos") or []:
        if isinstance(ativo, dict):
            _imprimir_ativo(ativo, hoje, validade)

    print("\n" + formatar_resumo(resumo))

    if resumo.atualizados:
        try:
            salvar_referencias(dados)
        except Exception as exc:
            print(f"\n❌ Falha ao gravar data/referencias_precos.json: {exc}")
            return
        print(f"\n💾 Máxima de 52 semanas e origem da coleta atualizadas em {len(resumo.atualizados)} ativo(s).")
        print("Os fundamentos informados manualmente não foram alterados.")
        return

    print("\nℹ️ Nenhuma máxima de 52 semanas foi atualizada. O arquivo de referências permanece como estava.")


if __name__ == "__main__":
    executar_atualizacao()
