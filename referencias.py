"""
Preços de referência do B3 Sniper.

Separa preço justo, preço teto e faixa personalizada.
As fórmulas e a validação ficam aqui para o bot e o atualizador
usarem a mesma regra, sem depender de Telegram ou de rede.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REFERENCIAS_PATH = DATA_DIR / "referencias_precos.json"
ESTADO_ALERTAS_PATH = DATA_DIR / "alertas_estado.json"

TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")
VALIDADE_PADRAO_DIAS = 45

METODOS_JUSTO = frozenset({"graham", "bazin", "manual", "fii_vp"})
METODOS_TETO = frozenset({"desconto_sobre_justo", "max_52_semanas"})
_IDADE_PENDENCIA = re.compile(r"; idade \d+ dias")
_NOMES_METODO = {
    "graham": "Graham",
    "bazin": "Bazin",
    "manual": "Manual",
    "fii_vp": "FII (P/VP = 1)",
    "desconto_sobre_justo": "Desconto sobre o preço justo",
    "max_52_semanas": "Máxima de 52 semanas",
}


@dataclass
class ReferenciasCalculadas:
    justo: float | None = None
    teto: float | None = None
    faixa: tuple[float, float] | None = None
    metodo_justo: str | None = None
    metodo_teto: str | None = None
    detalhes_justo: str = ""
    detalhes_teto: str = ""
    base_justo: str = ""
    base_teto: str = ""
    problemas_justo: list[str] = field(default_factory=list)
    problemas_teto: list[str] = field(default_factory=list)
    problemas_faixa: list[str] = field(default_factory=list)
    emitir_teto: bool = False


@dataclass(frozen=True)
class Sinal:
    tipo: str
    categoria: str
    chave: str
    label: str
    ticker: str
    preco: float
    referencia: float | None = None
    referencia_fim: float | None = None
    metodo: str | None = None
    resumo: str = ""
    base: str = ""
    pendencias: tuple[str, ...] = ()
    margem_pct: float = 0.0


def hoje_brasilia(agora: datetime | None = None) -> date:
    momento = agora or datetime.now(TZ_BRASILIA)
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=TZ_BRASILIA)
    return momento.astimezone(TZ_BRASILIA).date()


def fmt_numero(valor: float, casas: int = 2) -> str:
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _numero_positivo(valor, nome: str) -> tuple[float | None, str | None]:
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None, f"{nome} ausente ou inválido"
    if not math.isfinite(numero) or numero <= 0:
        return None, f"{nome} deve ser maior que zero"
    return numero, None


def _desconto(valor, nome: str = "desconto_pct") -> tuple[float | None, str | None]:
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None, f"{nome} ausente ou inválido"
    if not math.isfinite(numero) or numero < 0 or numero >= 1:
        return None, f"{nome} deve estar entre 0 e 1 (ex.: 0,05 = 5%)"
    return numero, None


def _data_referencia(valor, hoje: date, validade_dias: int, nome: str) -> list[str]:
    if not isinstance(valor, str) or not valor.strip():
        return [f"{nome} ausente"]
    try:
        consultada = date.fromisoformat(valor.strip())
    except ValueError:
        return [f"{nome} inválida (use AAAA-MM-DD)"]
    if consultada > hoje:
        return [f"{nome} no futuro ({consultada.isoformat()})"]
    idade = (hoje - consultada).days
    if idade > validade_dias:
        return [
            f"{nome} vencida "
            f"({consultada.isoformat()}; validade {validade_dias} dias; idade {idade} dias)"
        ]
    return []


def _origem(bloco: dict) -> list[str]:
    origem = bloco.get("origem")
    if not isinstance(origem, str) or not origem.strip():
        return ["origem dos fundamentos ausente"]
    return []


def calcular_justo(bloco: dict) -> float | None:
    """Calcula o preço justo. Retorna None se os insumos não forem positivos."""
    metodo = str(bloco.get("metodo") or "").strip()
    if metodo == "graham":
        lpa, erro_lpa = _numero_positivo(bloco.get("lpa"), "LPA")
        vpa, erro_vpa = _numero_positivo(bloco.get("vpa"), "VPA")
        if erro_lpa or erro_vpa or lpa is None or vpa is None:
            return None
        return math.sqrt(22.5 * lpa * vpa)
    if metodo == "bazin":
        dividendos, erro_div = _numero_positivo(bloco.get("dividendos_12m"), "dividendos_12m")
        yield_alvo, erro_yield = _desconto(bloco.get("yield_alvo"), "yield_alvo")
        if erro_div or erro_yield or dividendos is None or yield_alvo is None or yield_alvo <= 0:
            return None
        return dividendos / yield_alvo
    if metodo == "manual":
        preco, erro = _numero_positivo(bloco.get("preco_manual"), "preco_manual")
        if erro or preco is None:
            return None
        return preco
    if metodo == "fii_vp":
        vpa, erro = _numero_positivo(bloco.get("vpa"), "VPA")
        if erro or vpa is None:
            return None
        return vpa
    return None


def validar_justo(bloco: dict, hoje: date, validade_dias: int) -> list[str]:
    if not isinstance(bloco, dict):
        return ["bloco de preço justo inválido"]
    metodo = str(bloco.get("metodo") or "").strip()
    problemas: list[str] = []
    if metodo not in METODOS_JUSTO:
        return [f"método de preço justo desconhecido: {metodo or 'ausente'}"]
    problemas.extend(_origem(bloco))
    problemas.extend(
        _data_referencia(bloco.get("data_fundamentos"), hoje, validade_dias, "data dos fundamentos")
    )
    if metodo == "graham":
        for nome, campo in (("LPA", "lpa"), ("VPA", "vpa")):
            _, erro = _numero_positivo(bloco.get(campo), nome)
            if erro:
                problemas.append(erro)
    elif metodo == "bazin":
        _, erro_div = _numero_positivo(bloco.get("dividendos_12m"), "dividendos_12m")
        if erro_div:
            problemas.append(erro_div)
        yield_alvo, erro_yield = _desconto(bloco.get("yield_alvo"), "yield_alvo")
        if erro_yield:
            problemas.append(erro_yield)
        elif yield_alvo == 0:
            problemas.append("yield_alvo deve ser maior que zero")
    elif metodo == "manual":
        _, erro = _numero_positivo(bloco.get("preco_manual"), "preco_manual")
        if erro:
            problemas.append(erro)
    else:
        _, erro = _numero_positivo(bloco.get("vpa"), "VPA")
        if erro:
            problemas.append(erro)
    return problemas


def _detalhes_justo(bloco: dict) -> tuple[str, str]:
    metodo = bloco.get("metodo")
    if metodo == "graham":
        return (
            f"LPA {fmt_numero(float(bloco['lpa']))} | VPA {fmt_numero(float(bloco['vpa']))}",
            "Preço justo = √(22,5 × LPA × VPA)",
        )
    if metodo == "bazin":
        yield_pct = float(bloco["yield_alvo"]) * 100
        return (
            f"Dividendos 12M {fmt_numero(float(bloco['dividendos_12m']))} | "
            f"Yield alvo {fmt_numero(yield_pct, 2)}%",
            "Preço justo = dividendos 12 meses / yield alvo",
        )
    if metodo == "manual":
        return (
            f"Preço informado {fmt_numero(float(bloco['preco_manual']))}",
            "Preço justo informado manualmente",
        )
    return (
        f"VPA {fmt_numero(float(bloco['vpa']))}",
        "Preço justo = valor patrimonial por cota (P/VP = 1)",
    )


def validar_faixa(faixa: dict) -> list[str]:
    if not isinstance(faixa, dict):
        return ["faixa personalizada inválida"]
    minimo, erro_min = _numero_positivo(faixa.get("preco_minimo"), "preco_minimo")
    maximo, erro_max = _numero_positivo(faixa.get("preco_maximo"), "preco_maximo")
    problemas = [erro for erro in (erro_min, erro_max) if erro]
    if not problemas and minimo is not None and maximo is not None and maximo < minimo:
        problemas.append("preco_maximo deve ser maior ou igual a preco_minimo")
    return problemas


def calcular_referencias(
    ativo: dict,
    hoje: date,
    validade_dias: int,
) -> ReferenciasCalculadas:
    resultado = ReferenciasCalculadas()
    bloco_justo = ativo.get("preco_justo")
    bloco_teto = ativo.get("preco_teto")
    faixa = ativo.get("faixa")

    if bloco_justo is None and bloco_teto is None and faixa is None:
        resultado.problemas_justo.append("ativo sem preço justo, preço teto ou faixa")
        return resultado

    if isinstance(bloco_justo, dict):
        resultado.metodo_justo = str(bloco_justo.get("metodo") or "").strip() or None
        resultado.problemas_justo = validar_justo(bloco_justo, hoje, validade_dias)
        if resultado.metodo_justo in METODOS_JUSTO:
            resultado.justo = calcular_justo(bloco_justo)
            if resultado.justo is not None:
                resultado.detalhes_justo, resultado.base_justo = _detalhes_justo(bloco_justo)
            elif not resultado.problemas_justo:
                resultado.problemas_justo.append("não foi possível calcular o preço justo")
    elif bloco_justo is not None:
        resultado.problemas_justo.append("bloco de preço justo inválido")

    if isinstance(bloco_teto, dict):
        metodo = str(bloco_teto.get("metodo") or "").strip()
        resultado.metodo_teto = metodo or None
        if metodo == "desconto_sobre_justo":
            desconto, erro = _desconto(bloco_teto.get("desconto_pct", 0))
            if erro or desconto is None:
                resultado.problemas_teto.append(erro or "desconto_pct inválido")
            elif resultado.justo is None:
                if not bloco_justo:
                    resultado.problemas_teto.append(
                        "preço justo ausente para aplicar o desconto do preço teto"
                    )
            else:
                resultado.teto = resultado.justo * (1 - desconto)
                resultado.detalhes_teto = (
                    f"Desconto {fmt_numero(desconto * 100, 2)}% sobre o preço justo"
                )
                resultado.base_teto = "Preço teto = preço justo × (1 − desconto)"
                resultado.emitir_teto = desconto > 0 and not resultado.problemas_justo
        elif metodo == "max_52_semanas":
            desconto, erro = _desconto(bloco_teto.get("desconto_pct", 0.05))
            if erro or desconto is None:
                resultado.problemas_teto.append(erro or "desconto_pct inválido")
            maxima, erro_max = _numero_positivo(bloco_teto.get("maxima_52s"), "maxima_52s")
            if erro_max:
                resultado.problemas_teto.append(erro_max)
            resultado.problemas_teto.extend(
                _data_referencia(
                    bloco_teto.get("data_cotacao"),
                    hoje,
                    validade_dias,
                    "data da cotação",
                )
            )
            if bloco_teto.get("origem") in (None, ""):
                resultado.problemas_teto.append("origem da cotação ausente")
            if erro is None and erro_max is None and maxima is not None and desconto is not None:
                resultado.teto = maxima * (1 - desconto)
                resultado.emitir_teto = not resultado.problemas_teto
                resultado.detalhes_teto = (
                    f"Máxima {fmt_numero(maxima)} | Desconto {fmt_numero(desconto * 100, 2)}%"
                )
                resultado.base_teto = "Preço teto = máxima de 52 semanas × (1 − desconto)"
        else:
            resultado.problemas_teto.append(
                f"método de preço teto desconhecido: {metodo or 'ausente'}"
            )
    elif bloco_teto is not None:
        resultado.problemas_teto.append("bloco de preço teto inválido")

    if isinstance(faixa, dict):
        resultado.problemas_faixa = validar_faixa(faixa)
        if not resultado.problemas_faixa:
            resultado.faixa = (float(faixa["preco_minimo"]), float(faixa["preco_maximo"]))
    elif faixa is not None:
        resultado.problemas_faixa.append("faixa personalizada inválida")

    return resultado


def _limpar_exceto(episodios: set[str], prefixo: str, manter: str | None) -> None:
    for chave in list(episodios):
        if chave.startswith(prefixo) and chave != manter:
            episodios.discard(chave)


def _sinal_alerta(
    *,
    categoria: str,
    ticker: str,
    label: str,
    preco: float,
    referencia: float,
    referencia_fim: float | None,
    metodo: str | None,
    resumo: str,
    base: str,
    margem_pct: float,
    episodios: set[str],
) -> Sinal | None:
    fim = f"|{referencia_fim:.8f}" if referencia_fim is not None else ""
    chave = f"alerta|{categoria}|{ticker}|{metodo or '-'}|{referencia:.8f}{fim}"
    prefixo = f"alerta|{categoria}|{ticker}|"
    envio = None
    if chave not in episodios:
        envio = Sinal(
            tipo="alerta",
            categoria=categoria,
            chave=chave,
            label=label,
            ticker=ticker,
            preco=preco,
            referencia=referencia,
            referencia_fim=referencia_fim,
            metodo=metodo,
            resumo=resumo,
            base=base,
            margem_pct=margem_pct,
        )
    _limpar_exceto(episodios, prefixo, chave)
    episodios.add(chave)
    _limpar_exceto(episodios, f"pendencia|{categoria}|{ticker}|", None)
    return envio


def _codigo_pendencia(pendencias: list[str]) -> str:
    """Identifica a pendência sem a idade, que muda todo dia."""
    return "; ".join(_IDADE_PENDENCIA.sub("", item) for item in pendencias)


def linhas_status(calculado: ReferenciasCalculadas) -> list[str]:
    """Texto do /status. Valor suspenso aparece junto com a pendência."""
    linhas: list[str] = []
    if calculado.metodo_justo or calculado.justo is not None or calculado.problemas_justo:
        if calculado.justo is not None:
            metodo = _NOMES_METODO.get(calculado.metodo_justo or "", calculado.metodo_justo or "preço justo")
            linhas.append(f"Justo ({metodo}): R$ {fmt_numero(calculado.justo)}")
        elif calculado.problemas_justo:
            linhas.append("Justo: não calculado")
        if calculado.problemas_justo:
            linhas.append("Pendência do preço justo: " + "; ".join(calculado.problemas_justo))

    if calculado.metodo_teto or calculado.teto is not None or calculado.problemas_teto:
        if calculado.teto is not None and calculado.emitir_teto:
            metodo = _NOMES_METODO.get(calculado.metodo_teto or "", calculado.metodo_teto or "preço teto")
            linhas.append(f"Teto ({metodo}): R$ {fmt_numero(calculado.teto)}")
        elif (
            calculado.teto is not None
            and calculado.justo is not None
            and abs(calculado.teto - calculado.justo) <= 1e-9
            and not calculado.problemas_teto
        ):
            linhas.append(f"Teto: igual ao preço justo (R$ {fmt_numero(calculado.teto)})")
        elif calculado.teto is not None:
            metodo = _NOMES_METODO.get(calculado.metodo_teto or "", calculado.metodo_teto or "preço teto")
            linhas.append(f"Teto ({metodo}): R$ {fmt_numero(calculado.teto)}")
        elif calculado.problemas_teto:
            linhas.append("Teto: não calculado")
        if calculado.problemas_teto:
            linhas.append("Pendência do preço teto: " + "; ".join(calculado.problemas_teto))

    if calculado.faixa is not None:
        linhas.append(
            f"Faixa: R$ {fmt_numero(calculado.faixa[0])} a R$ {fmt_numero(calculado.faixa[1])}"
        )
    elif calculado.problemas_faixa:
        linhas.append("Faixa: não calculada")
        linhas.append("Pendência da faixa: " + "; ".join(calculado.problemas_faixa))
    return linhas


def _sinal_pendencia(
    *,
    categoria: str,
    ticker: str,
    label: str,
    preco: float,
    pendencias: list[str],
    episodios: set[str],
) -> Sinal | None:
    texto = _codigo_pendencia(pendencias)
    chave = f"pendencia|{categoria}|{ticker}|{texto}"
    prefixo = f"pendencia|{categoria}|{ticker}|"
    envio = None
    if chave not in episodios:
        envio = Sinal(
            tipo="pendencia",
            categoria=categoria,
            chave=chave,
            label=label,
            ticker=ticker,
            preco=preco,
            pendencias=tuple(pendencias),
        )
    _limpar_exceto(episodios, prefixo, chave)
    episodios.add(chave)
    _limpar_exceto(episodios, f"alerta|{categoria}|{ticker}|", None)
    return envio


def _encerrar_categoria(episodios: set[str], categoria: str, ticker: str) -> None:
    _limpar_exceto(episodios, f"alerta|{categoria}|{ticker}|", None)
    _limpar_exceto(episodios, f"pendencia|{categoria}|{ticker}|", None)


def avaliar_ativo(
    ativo: dict,
    preco: float,
    *,
    hoje: date,
    validade_dias: int,
    margem_pct: float,
    episodios: set[str],
) -> list[Sinal]:
    """
    Atualiza os episódios e devolve somente os sinais que ainda não foram enviados.
    O preço justo é calculado para o /status e não gera alerta de compra.
    O alerta de compra sai pelo preço teto quando o desconto é maior que zero.
    """
    ticker = str(ativo.get("ticker") or "").strip().upper()
    label = str(ativo.get("nome_amigavel") or ticker).strip() or ticker
    calculado = calcular_referencias(ativo, hoje, validade_dias)
    margem = margem_pct / 100 if math.isfinite(margem_pct) and margem_pct > 0 else 0.0
    envios: list[Sinal] = []

    if ativo.get("preco_justo") is not None or (
        not ativo.get("preco_justo") and calculado.problemas_justo and not ativo.get("preco_teto") and not ativo.get("faixa")
    ):
        if calculado.problemas_justo:
            sinal = _sinal_pendencia(
                categoria="justo",
                ticker=ticker,
                label=label,
                preco=preco,
                pendencias=calculado.problemas_justo,
                episodios=episodios,
            )
            if sinal:
                envios.append(sinal)
        else:
            _encerrar_categoria(episodios, "justo", ticker)

    if ativo.get("preco_teto") is not None:
        if calculado.problemas_teto:
            sinal = _sinal_pendencia(
                categoria="teto",
                ticker=ticker,
                label=label,
                preco=preco,
                pendencias=calculado.problemas_teto,
                episodios=episodios,
            )
            if sinal:
                envios.append(sinal)
        elif calculado.emitir_teto and calculado.teto is not None:
            dentro = preco <= calculado.teto * (1 + margem)
            if dentro:
                sinal = _sinal_alerta(
                    categoria="teto",
                    ticker=ticker,
                    label=label,
                    preco=preco,
                    referencia=calculado.teto,
                    referencia_fim=None,
                    metodo=calculado.metodo_teto,
                    resumo=calculado.detalhes_teto,
                    base=calculado.base_teto,
                    margem_pct=margem_pct if margem else 0.0,
                    episodios=episodios,
                )
                if sinal:
                    envios.append(sinal)
            else:
                _encerrar_categoria(episodios, "teto", ticker)
        else:
            _encerrar_categoria(episodios, "teto", ticker)

    if ativo.get("faixa") is not None:
        if calculado.problemas_faixa:
            sinal = _sinal_pendencia(
                categoria="faixa",
                ticker=ticker,
                label=label,
                preco=preco,
                pendencias=calculado.problemas_faixa,
                episodios=episodios,
            )
            if sinal:
                envios.append(sinal)
        elif calculado.faixa is not None:
            minimo, maximo = calculado.faixa
            dentro = minimo <= preco <= maximo
            if dentro:
                sinal = _sinal_alerta(
                    categoria="faixa",
                    ticker=ticker,
                    label=label,
                    preco=preco,
                    referencia=minimo,
                    referencia_fim=maximo,
                    metodo="faixa",
                    resumo=f"{fmt_numero(minimo)} a {fmt_numero(maximo)}",
                    base="Faixa de preço configurada",
                    margem_pct=0.0,
                    episodios=episodios,
                )
                if sinal:
                    envios.append(sinal)
            else:
                _encerrar_categoria(episodios, "faixa", ticker)

    return envios


def carregar_referencias(path: Path | None = None) -> dict:
    arquivo = path or REFERENCIAS_PATH
    with open(arquivo, encoding="utf-8") as handle:
        dados = json.load(handle)
    if not isinstance(dados, dict) or not isinstance(dados.get("ativos"), list):
        raise ValueError(f"Arquivo de referências inválido: {arquivo}")
    try:
        validade = int(dados.get("validade_dias", VALIDADE_PADRAO_DIAS))
    except (TypeError, ValueError):
        validade = VALIDADE_PADRAO_DIAS
    if validade < 1:
        validade = VALIDADE_PADRAO_DIAS
    dados["validade_dias"] = validade
    try:
        margem = float(dados.get("margem_proximidade_pct", 0))
    except (TypeError, ValueError):
        margem = 0.0
    if not math.isfinite(margem) or margem < 0:
        margem = 0.0
    dados["margem_proximidade_pct"] = margem
    return dados


def salvar_referencias(dados: dict, path: Path | None = None) -> None:
    arquivo = path or REFERENCIAS_PATH
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    temporario = arquivo.with_suffix(arquivo.suffix + ".tmp")
    temporario.write_text(json.dumps(dados, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporario.replace(arquivo)


def aplicar_maxima_52(ativo: dict, maxima: float, data_cotacao: str) -> bool:
    """Grava a máxima e a data da coleta. Não altera fundamentos manuais."""
    bloco = ativo.get("preco_teto")
    if not isinstance(bloco, dict) or bloco.get("metodo") != "max_52_semanas":
        return False
    valor, erro = _numero_positivo(maxima, "maxima_52s")
    if erro or valor is None:
        return False
    try:
        date.fromisoformat(data_cotacao)
    except ValueError:
        return False
    bloco["maxima_52s"] = round(valor, 4)
    bloco["data_cotacao"] = data_cotacao
    if not bloco.get("origem"):
        bloco["origem"] = "Yahoo Finance"
    return True


def atualizar_maximas(dados: dict, buscar, hoje: date) -> int:
    """Atualiza só máximas de 52 semanas. `buscar` recebe o ticker e devolve float ou None."""
    alterados = 0
    for ativo in dados.get("ativos") or []:
        if not isinstance(ativo, dict):
            continue
        bloco = ativo.get("preco_teto") or {}
        if not isinstance(bloco, dict) or bloco.get("metodo") != "max_52_semanas":
            continue
        ticker = str(ativo.get("ticker") or "")
        maxima = buscar(ticker)
        if aplicar_maxima_52(ativo, maxima, hoje.isoformat()):
            alterados += 1
    return alterados


def carregar_episodios(path: Path | None = None) -> set[str]:
    arquivo = path or ESTADO_ALERTAS_PATH
    if not arquivo.is_file():
        return set()
    try:
        with open(arquivo, encoding="utf-8") as handle:
            dados = json.load(handle)
        chaves = dados.get("chaves") if isinstance(dados, dict) else None
        if not isinstance(chaves, list):
            return set()
        return {str(chave) for chave in chaves if isinstance(chave, str) and chave}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return set()


def salvar_episodios(episodios: set[str], path: Path | None = None) -> None:
    arquivo = path or ESTADO_ALERTAS_PATH
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    payload = {"chaves": sorted(episodios)}
    temporario = arquivo.with_suffix(arquivo.suffix + ".tmp")
    temporario.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporario.replace(arquivo)


def tickers_com_fundamentos(dados: dict) -> list[str]:
    nomes = []
    for ativo in dados.get("ativos") or []:
        if not isinstance(ativo, dict):
            continue
        bloco = ativo.get("preco_justo")
        if isinstance(bloco, dict) and bloco.get("metodo") in METODOS_JUSTO:
            nomes.append(str(ativo.get("ticker") or "").strip())
    return [nome for nome in nomes if nome]
