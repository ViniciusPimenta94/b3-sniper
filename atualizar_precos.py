import math
import sys
from datetime import date

import yfinance as yf

from referencias import (
    atualizar_maximas,
    calcular_referencias,
    carregar_referencias,
    hoje_brasilia,
    linhas_status,
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


def buscar_maxima_52(ticker: str) -> float | None:
    """Coleta a máxima de 52 semanas. Não altera LPA, VPA, dividendos nem VPA de FII."""
    try:
        ticker_obj = yf.Ticker(ticker)
        maxima = ticker_obj.info.get("fiftyTwoWeekHigh")
        if not maxima:
            historico = ticker_obj.history(period="1y")
            if not historico.empty and "High" in historico.columns:
                maxima = historico["High"].max()
        if maxima is None:
            return None
        maxima = float(maxima)
        if math.isnan(maxima) or maxima <= 0:
            return None
        return maxima
    except Exception as exc:
        print(f"   ⚠️ Falha ao coletar a máxima de {ticker}: {exc}")
        return None


def _imprimir_ativo(ativo: dict, hoje: date, validade_dias: int) -> None:
    ticker = ativo.get("ticker", "")
    nome = ativo.get("nome_amigavel", ticker)
    calculado = calcular_referencias(ativo, hoje, validade_dias)
    print(f"\n🔍 {nome} ({ticker})")
    for linha in linhas_status(calculado):
        print(f"   {linha}")


def executar_atualizacao() -> None:
    try:
        dados = carregar_referencias()
    except Exception as exc:
        print(f"❌ Erro ao carregar data/referencias_precos.json: {exc}")
        return

    hoje = hoje_brasilia()
    validade = int(dados["validade_dias"])
    print("==================================================================")
    print("🎯 SNIPER: REFERÊNCIAS DE PREÇO")
    print("Fundamentos manuais são preservados. Só a máxima de 52 semanas é atualizada.")
    print("==================================================================")

    try:
        alterados = atualizar_maximas(dados, buscar_maxima_52, hoje)
    except Exception as exc:
        print(f"\n❌ Falha ao atualizar máximas de 52 semanas: {exc}")
        return

    for ativo in dados.get("ativos") or []:
        if isinstance(ativo, dict):
            _imprimir_ativo(ativo, hoje, validade)

    if alterados:
        try:
            salvar_referencias(dados)
        except Exception as exc:
            print(f"\n❌ Falha ao gravar data/referencias_precos.json: {exc}")
            return
        print(f"\n💾 Máxima de 52 semanas atualizada em {alterados} ativo(s).")
        print("Os fundamentos informados manualmente não foram alterados.")
        return

    print("\nℹ️ Nenhuma máxima de 52 semanas foi atualizada. O arquivo de referências permanece como estava.")


if __name__ == "__main__":
    executar_atualizacao()
