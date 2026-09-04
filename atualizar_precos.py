import json
import math
import datetime
import sys
from pathlib import Path
from typing import Any, Callable

import yfinance as yf


def _configurar_console_utf8() -> None:
    """Evita UnicodeEncodeError no terminal Windows (cp1252) ao imprimir emojis."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                pass


_configurar_console_utf8()

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

ESTRATEGIAS_PADRAO: dict[str, dict[str, Any]] = {
    "max_52_semanas": {
        "descricao": "Desconto sobre a Máxima de 52 Semanas",
        "parametros": {
            "desconto_pct": 0.05,
        },
    },
    "bazin": {
        "descricao": "Décio Bazin focado em Proventos (Preço = Dividendos 12M / Yield Alvo)",
        "parametros": {
            "yield_alvo": 0.08,
            "proventos_fallback": None,
        },
    },
    "graham": {
        "descricao": "Fórmula de Benjamin Graham (√(22,5 × LPA × VPA))",
        "parametros": {
            "lpa_fallback": None,
            "vpa_fallback": None,
        },
    },
    "fii_vp": {
        "descricao": "Fundo Imobiliário: Alvo P/VP = 1.0 (Comprar no Valor Patrimonial)",
        "parametros": {
            "vpa_fallback": None,
        },
    },
}


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def obter_dividendos_12m(ticker_obj: yf.Ticker) -> float | None:
    """Tenta consolidar a soma de dividendos pagos nos últimos 12 meses via Yahoo Finance."""
    try:
        divs = ticker_obj.dividends
        if not divs.empty:
            um_ano_atras = datetime.datetime.now() - datetime.timedelta(days=365)
            total = 0.0
            for dt, val in divs.items():
                if dt.replace(tzinfo=None) > um_ano_atras:
                    total += val
            if total > 0:
                return total
    except Exception:
        pass
    return None


def _resolver_parametros(
    estrategia_id: str, asset: dict, catalogo: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Mescla parâmetros padrão da estratégia com overrides do ativo."""
    meta = catalogo.get(estrategia_id) or ESTRATEGIAS_PADRAO.get(estrategia_id, {})
    params = dict(meta.get("parametros") or {})
    overrides = asset.get("parametros") or {}
    params.update(overrides)
    return params


def _descricao_estrategia(estrategia_id: str, catalogo: dict[str, dict[str, Any]]) -> str:
    meta = catalogo.get(estrategia_id) or ESTRATEGIAS_PADRAO.get(estrategia_id, {})
    return meta.get("descricao") or estrategia_id


def _aplicar_max_52_semanas(
    asset: dict, nome: str, ticker_obj: yf.Ticker, params: dict[str, Any]
) -> float | None:
    desconto = float(params.get("desconto_pct", 0.05))
    fator = 1.0 - desconto

    high_52 = ticker_obj.info.get("fiftyTwoWeekHigh")
    if not high_52:
        hist = ticker_obj.history(period="1y")
        if not hist.empty:
            high_52 = hist["High"].max()

    if high_52 and not math.isnan(high_52):
        novo_teto = round(high_52 * fator, 2)
        print(
            f"   ✅ {nome}: Máxima R$ {high_52:.2f} "
            f"(desconto {desconto * 100:.0f}%) -> Novo Preço Teto: R$ {novo_teto:.2f}"
        )
        return novo_teto

    teto_atual = asset.get("preco_teto")
    print(
        f"   ⚠️ Não foi possível coletar dados de preço para {nome}. "
        f"Mantido: R$ {teto_atual}"
    )
    return None


def _aplicar_bazin(
    asset: dict, nome: str, ticker_obj: yf.Ticker, params: dict[str, Any]
) -> float | None:
    yield_alvo = float(params.get("yield_alvo", 0.08))
    if yield_alvo <= 0:
        print(f"   ⚠️ yield_alvo inválido para {nome}. Mantido.")
        return None

    fallback = params.get("proventos_fallback")
    divs = obter_dividendos_12m(ticker_obj)
    if divs is None and fallback is not None:
        divs = float(fallback)

    if divs is None or divs <= 0:
        print(f"   ⚠️ Proventos indisponíveis para {nome}. Mantido.")
        return None

    novo_teto = round(divs / yield_alvo, 2)
    print(
        f"   ✅ {nome}: Proventos 12M R$ {divs:.2f} "
        f"(yield alvo {yield_alvo * 100:.0f}%) -> Novo Preço Teto: R$ {novo_teto:.2f}"
    )
    return novo_teto


def _aplicar_graham(
    asset: dict, nome: str, ticker_obj: yf.Ticker, params: dict[str, Any]
) -> float | None:
    lpa = ticker_obj.info.get("trailingEps")
    vpa = ticker_obj.info.get("bookValue")

    if lpa is None and params.get("lpa_fallback") is not None:
        lpa = float(params["lpa_fallback"])
    if vpa is None and params.get("vpa_fallback") is not None:
        vpa = float(params["vpa_fallback"])

    if lpa and vpa and lpa > 0 and vpa > 0:
        preco_justo = math.sqrt(22.5 * lpa * vpa)
        novo_teto = round(preco_justo, 2)
        print(
            f"   ✅ {nome}: LPA {lpa:.2f} | VPA {vpa:.2f} "
            f"-> Preço Justo (Graham): R$ {novo_teto:.2f}"
        )
        return novo_teto

    print(f"   ⚠️ Indicadores zerados ou indisponíveis para {nome}. Mantido.")
    return None


def _aplicar_fii_vp(
    asset: dict, nome: str, ticker_obj: yf.Ticker, params: dict[str, Any]
) -> float | None:
    vpa = ticker_obj.info.get("bookValue")
    if (not vpa or vpa <= 0) and params.get("vpa_fallback") is not None:
        vpa = float(params["vpa_fallback"])

    if vpa and vpa > 0:
        novo_teto = round(vpa, 2)
        print(f"   ✅ {nome}: VP por cota R$ {vpa:.2f} -> Novo Preço Teto: R$ {novo_teto:.2f}")
        return novo_teto

    print(f"   ⚠️ Impossível determinar o VPA contábil de {nome}. Mantido.")
    return None


APLICADORES: dict[str, Callable[..., float | None]] = {
    "max_52_semanas": _aplicar_max_52_semanas,
    "bazin": _aplicar_bazin,
    "graham": _aplicar_graham,
    "fii_vp": _aplicar_fii_vp,
}


def processar_ativo(
    asset: dict, catalogo: dict[str, dict[str, Any]]
) -> None:
    ticker = asset.get("ticker", "")
    nome = asset.get("nome_amigavel", ticker)
    estrategia_id = (asset.get("estrategia") or "").strip()

    print(f"\n🔍 Analisando Ativo: {nome} ({ticker})")

    if not estrategia_id:
        teto_atual = asset.get("preco_teto")
        if teto_atual is not None:
            print(f"   ⚠️ Campo 'estrategia' ausente para {nome}. Mantido: R$ {teto_atual}")
        else:
            print(f"   ⚠️ Campo 'estrategia' ausente para {nome}.")
        return

    aplicador = APLICADORES.get(estrategia_id)
    if not aplicador:
        estrategias_validas = ", ".join(sorted(APLICADORES))
        print(
            f"   ⚠️ Estratégia '{estrategia_id}' desconhecida para {nome}. "
            f"Opções: {estrategias_validas}. Mantido."
        )
        return

    descricao = _descricao_estrategia(estrategia_id, catalogo)
    print(f"   ↳ [Estratégia] {descricao}")

    params = _resolver_parametros(estrategia_id, asset, catalogo)

    try:
        ticker_obj = yf.Ticker(ticker)
        novo_teto = aplicador(asset, nome, ticker_obj, params)
        if novo_teto is not None:
            asset["preco_teto"] = novo_teto
    except Exception as e:
        print(f"   ❌ Erro ao processar {nome} ({estrategia_id}): {e}")


def executar_atualizacao() -> None:
    try:
        config = load_config()
    except Exception as e:
        print(f"❌ Erro ao carregar o arquivo config.json: {e}")
        return

    catalogo = dict(ESTRATEGIAS_PADRAO)
    catalogo.update(config.get("estrategias") or {})

    assets = config.get("assets", [])

    print("==================================================================")
    print("🎯 SNIPER FINANCEIRO: SISTEMA AUTOMÁTICO DE PREÇO TETO")
    print("==================================================================")

    for asset in assets:
        processar_ativo(asset, catalogo)

    try:
        save_config(config)
        print("\n💾 [SUCESSO] O seu arquivo 'config.json' foi reconfigurado com inteligência e segurança!")
    except Exception as e:
        print(f"\n❌ Falha catastrófica ao gravar alterações no config.json: {e}")


if __name__ == "__main__":
    executar_atualizacao()
