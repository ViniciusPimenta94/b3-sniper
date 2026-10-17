"""
B3 Sniper
Bot Telegram para monitoramento de oportunidades e ordens na B3.

Funcionalidades:
- Monitoramento de preço justo, preço teto e faixa
- Monitoramento de ordens de compra
- Alertas via Telegram
- Comando /status
- Comando /ordens
- Comando /help
- Resumo diário das ordens
- Servidor Flask para Render/UptimeRobot
- Heartbeat nos logs

Bibliotecas:
- yfinance
- pyTelegramBotAPI
- schedule
- Flask
"""

from __future__ import annotations

import json
import calendar
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


# ===========================================================================
# TIMEZONE
# ===========================================================================

# Define Brasília como timezone do processo.
# Isso ajuda a biblioteca "schedule" a interpretar horários corretamente.
os.environ["TZ"] = "America/Sao_Paulo"

if hasattr(time, "tzset"):
    time.tzset()


import schedule
import telebot
import yfinance as yf
from flask import Flask

import referencias


# ===========================================================================
# CAMINHOS
# ===========================================================================

BASE_DIR = Path(__file__).resolve().parent

CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_LOCAL_PATH = BASE_DIR / "config.local.json"
ORDENS_PATH = BASE_DIR / "ordens.json"


# ===========================================================================
# TIMEZONE
# ===========================================================================

TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")


# ===========================================================================
# LOGGING
# ===========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger(__name__)


# ===========================================================================
# SERVIDOR WEB - RENDER (sem ping 24/7; horas só com tráfego inbound)
# ===========================================================================

app = Flask(__name__)


@app.before_request
def log_request():
    log.info(
        "🌐 Ping HTTP recebido - %s",
        datetime.now(TZ_BRASILIA).strftime("%d/%m/%Y %H:%M:%S"),
    )


@app.route("/")
def home():
    return "🤖 B3 Sniper Online!", 200


@app.route("/health")
def health():
    return {
        "status": "online",
        "service": "B3 Sniper",
        "time": datetime.now(TZ_BRASILIA).isoformat(),
    }, 200


def run_web_server():
    """
    Inicia o servidor HTTP necessário para o Render Web Service.
    """

    port = int(os.environ.get("PORT", 10000))

    log.info(
        "🌐 Servidor web iniciado na porta %s",
        port,
    )

    app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False,
    )


# ===========================================================================
# CONFIGURAÇÃO
# ===========================================================================

def load_config() -> dict:
    """
    Carrega a configuração do bot.

    Usa config.local.json para credenciais locais, quando existir;
    no Render, usa o config.json versionado e as variáveis de ambiente.

    As variáveis de ambiente do Render sobrescrevem
    as configurações do Telegram.
    """

    config_file = None

    if CONFIG_LOCAL_PATH.is_file():
        config_file = CONFIG_LOCAL_PATH

    elif CONFIG_PATH.is_file():
        config_file = CONFIG_PATH

    else:
        raise FileNotFoundError(
            "Nenhum arquivo de configuração encontrado. "
            f"Esperado: {CONFIG_PATH} ou {CONFIG_LOCAL_PATH}"
        )

    with open(config_file, encoding="utf-8") as f:
        config = json.load(f)

    # Garante que a seção telegram exista
    if "telegram" not in config:
        config["telegram"] = {}

    # Variáveis de ambiente têm prioridade
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if telegram_token:
        config["telegram"]["bot_token"] = telegram_token

    if telegram_chat_id:
        config["telegram"]["chat_id"] = telegram_chat_id

    log.info(
        "📄 Configuração carregada de: %s",
        config_file.name,
    )

    return config


def get_bot_token(cfg: dict) -> str:
    """
    Prioridade:

    1. TELEGRAM_BOT_TOKEN do ambiente
    2. telegram.bot_token do config.json
    """

    token = (
        os.environ.get(
            "TELEGRAM_BOT_TOKEN"
        )
        or ""
    ).strip()

    if token:
        return token

    token = (
        (cfg.get("telegram") or {})
        .get("bot_token")
        or ""
    )

    token = str(token).strip()

    if not token:
        raise ValueError(
            "Token do bot ausente. "
            "Defina TELEGRAM_BOT_TOKEN "
            "ou telegram.bot_token no config."
        )

    return token


# ===========================================================================
# MODELOS
# ===========================================================================

@dataclass
class AssetConfig:
    ticker: str
    preco_teto: float
    nome_amigavel: str | None = None

    @property
    def label(self) -> str:
        return self.nome_amigavel or self.ticker


@dataclass
class OrderConfig:
    ticker: str
    preco_ordem: float
    nome_amigavel: str | None = None

    @property
    def label(self) -> str:
        return (
            self.nome_amigavel
            or display_ticker(self.ticker)
        )


# ===========================================================================
# ATIVOS E ORDENS
# ===========================================================================

def display_ticker(ticker: str) -> str:
    """
    Exibe CMIG4 em vez de CMIG4.SA.
    """

    base = ticker.strip().upper()

    if base.endswith(".SA"):
        return base[:-3]

    return base


def load_ordens() -> list[OrderConfig]:
    """
    Carrega ordens.json.
    """

    if not ORDENS_PATH.is_file():

        log.info(
            "ℹ️ Arquivo ordens.json não encontrado; "
            "monitoramento de ordens desativado."
        )

        return []

    try:

        with open(
            ORDENS_PATH,
            encoding="utf-8",
        ) as f:

            data = json.load(f)

    except Exception as e:

        log.exception(
            "Erro ao carregar ordens.json: %s",
            e,
        )

        return []

    raw = data.get("ordens") or []

    out: list[OrderConfig] = []

    for item in raw:

        if not isinstance(item, dict):
            continue

        ticker = str(
            item.get("ticker", "")
        ).strip()

        if not ticker:
            continue

        try:

            preco_ordem = float(
                item.get("preco_ordem")
            )

        except (
            TypeError,
            ValueError,
        ):

            log.warning(
                "Ignorando ordem sem preco_ordem válido: %s",
                item,
            )

            continue

        nome = item.get("nome_amigavel")

        nome = (
            str(nome).strip()
            if nome
            else None
        )

        out.append(
            OrderConfig(
                ticker=ticker,
                preco_ordem=preco_ordem,
                nome_amigavel=nome,
            )
        )

    return out


def parse_assets(
    cfg: dict,
) -> list[AssetConfig]:
    """
    Lê a lista legada de config.json.
    Os alertas de preço usam data/referencias_precos.json.
    """

    raw = cfg.get("assets") or []

    out: list[AssetConfig] = []

    for item in raw:

        if not isinstance(item, dict):
            continue

        ticker = str(
            item.get("ticker", "")
        ).strip()

        if not ticker:
            continue

        try:

            preco_teto = float(
                item.get("preco_teto")
            )

        except (
            TypeError,
            ValueError,
        ):

            log.warning(
                "Ignorando ativo sem preco_teto válido: %s",
                item,
            )

            continue

        nome = item.get("nome_amigavel")

        nome = (
            str(nome).strip()
            if nome
            else None
        )

        out.append(
            AssetConfig(
                ticker=ticker,
                preco_teto=preco_teto,
                nome_amigavel=nome,
            )
        )

    return out


def _rotulo_metodo(metodo: str | None) -> str:
    nomes = {
        "graham": "Graham",
        "bazin": "Bazin",
        "manual": "Manual",
        "fii_vp": "FII (P/VP = 1)",
        "desconto_sobre_justo": "Desconto sobre o preço justo",
        "max_52_semanas": "Máxima de 52 semanas",
        "faixa": "Faixa personalizada",
    }
    return nomes.get(metodo or "", metodo or "referência")


def _nome_categoria(categoria: str) -> str:
    return {"justo": "preço justo", "teto": "preço teto", "faixa": "faixa"}.get(categoria, categoria)


def build_signal_message(sinal: referencias.Sinal, symbol: str) -> tuple[str, str | None]:
    """Monta alerta ou pendência. Pendência vai em texto simples."""
    if sinal.tipo == "pendencia":
        return (
            f"⚠️ Referência de {_nome_categoria(sinal.categoria)} não calculada\n\n"
            f"Ativo: {sinal.label} ({display_ticker(symbol)})\n"
            f"Preço atual: {format_money(sinal.preco)}\n"
            f"Pendências: {'; '.join(sinal.pendencias)}\n\n"
            "Nenhum alerta de preço foi emitido para esta referência."
        ), None

    titulos = {
        "justo": "📉 Alerta de preço justo",
        "teto": "🎯 Alerta de preço teto",
        "faixa": "🔎 Alerta de faixa de análise",
    }
    titulo = titulos.get(sinal.categoria, "Alerta de referência")
    linhas = [
        f"*{md_escape(titulo)}*",
        "",
        f"*{md_escape(sinal.label)}* \\({md_escape(display_ticker(symbol))}\\)",
        "",
        f"Preço atual: *{md_escape(format_money(sinal.preco))}*",
    ]
    if sinal.categoria == "faixa" and sinal.referencia is not None and sinal.referencia_fim is not None:
        linhas.append(
            "Faixa configurada: "
            f"*{md_escape(format_money(sinal.referencia))} a "
            f"{md_escape(format_money(sinal.referencia_fim))}*"
        )
        descricao = "O preço atual está dentro da faixa definida."
    else:
        rotulo = "Preço justo" if sinal.categoria == "justo" else "Preço teto"
        linhas.append(
            f"{md_escape(rotulo)}: *{md_escape(format_money(sinal.referencia or 0))}*"
        )
        linhas.append(f"Método: *{md_escape(_rotulo_metodo(sinal.metodo))}*")
        if sinal.resumo:
            linhas.append(md_escape(sinal.resumo))
        if sinal.margem_pct > 0:
            descricao = "O preço está próximo ou abaixo da referência."
        else:
            descricao = "O preço está igual ou abaixo da referência."
    linhas.extend(["", md_escape(descricao)])
    if sinal.base:
        linhas.append(f"_{md_escape(sinal.base)}_")
    return "\n".join(linhas), "MarkdownV2"


# ===========================================================================
# YAHOO FINANCE
# ===========================================================================

def _mensagem_excecao_segura(exc: BaseException) -> str:
    """Resume a exceção sem query string, corpo longo ou traceback."""
    texto = " ".join(str(exc).split())
    if "?" in texto:
        texto = texto.split("?", 1)[0].rstrip()
    if len(texto) > 160:
        texto = texto[:160] + "..."
    return texto


def _price_from_ticker(
    ticker: yf.Ticker,
) -> tuple[float | None, str | None]:
    """
    Obtém o último preço conhecido pelo histórico de cotações.

    Prioriza o histórico para evitar chamadas auxiliares do `fast_info`,
    associadas aos erros 401/Invalid Crumb observados no Render.
    O segundo item descreve o motivo quando não há preço. Não registra log.
    """

    try:

        hist = ticker.history(
            period="5d",
            interval="1d",
        )

    except Exception as exc:

        return None, (
            f"exceção {type(exc).__name__}: "
            f"{_mensagem_excecao_segura(exc)}"
        )

    if hist is None or getattr(hist, "empty", True):
        return None, "histórico vazio"

    if "Close" not in getattr(hist, "columns", ()):
        return None, "coluna Close ausente"

    try:

        value = float(hist["Close"].iloc[-1])

    except Exception:
        return None, "Close inválido"

    if not value > 0:
        return None, "Close inválido"

    return value, None


def resolve_yahoo_symbol(
    user_ticker: str,
    falhas: list[str] | None = None,
) -> tuple[str, float | None]:
    """
    Resolve o ticker no Yahoo Finance. Para ativos brasileiros,
    tenta primeiro o sufixo .SA e só usa o símbolo puro como fallback.

    Exemplos:

    CMIG4 -> CMIG4.SA
    XPLG11 -> XPLG11.SA

    `falhas` recebe os motivos somente quando nenhum candidato tem preço.
    """

    base = user_ticker.strip().upper()

    if not base:
        if falhas is not None:
            falhas.append("ticker vazio")
        return base, None

    candidates = [base] if base.endswith(".SA") else [f"{base}.SA", base]

    last_symbol = candidates[-1]
    tentativas: list[str] = []

    for symbol in candidates:

        last_symbol = symbol

        try:

            ticker = yf.Ticker(symbol)

            price, motivo = _price_from_ticker(
                ticker
            )

            if price is not None:
                return symbol, price

            tentativas.append(f"{symbol}: {motivo}")

        except Exception as exc:

            tentativas.append(
                f"{symbol}: exceção {type(exc).__name__}: "
                f"{_mensagem_excecao_segura(exc)}"
            )

            continue

    if falhas is not None:
        falhas.extend(tentativas)

    return last_symbol, None


def get_current_price(
    symbol: str,
) -> float | None:
    """
    Obtém o preço atual ou último preço disponível.
    """

    try:

        ticker = yf.Ticker(symbol)

        price, _motivo = _price_from_ticker(
            ticker
        )

        return price

    except Exception as e:

        log.warning(
            "Falha ao obter preço de %s: %s",
            symbol,
            e,
        )

        return None


def get_daily_close(
    symbol: str,
) -> float | None:
    """
    Obtém o fechamento do último pregão disponível.
    """

    try:

        hist = yf.Ticker(
            symbol
        ).history(
            period="5d",
            interval="1d",
        )

        if (
            hist is not None
            and not hist.empty
            and "Close" in hist.columns
        ):

            value = float(
                hist["Close"].iloc[-1]
            )

            if value > 0:
                return value

    except Exception as e:

        log.debug(
            "Fechamento diário indisponível para %s: %s",
            symbol,
            e,
        )

    return None


# ===========================================================================
# CÁLCULOS
# ===========================================================================

def calc_order_diff_pct(
    preco_ordem: float,
    preco_referencia: float,
) -> float:
    """
    Calcula a diferença percentual entre
    nossa ordem e o preço de referência.

    Fórmula:

    (ordem / referência - 1) * 100
    """

    if preco_referencia <= 0:
        return 0.0

    return (
        preco_ordem
        / preco_referencia
        - 1.0
    ) * 100.0


def format_pct(
    value: float,
) -> str:

    result = (
        f"{value:+.1f}"
        .replace(".", ",")
    )

    return result + "%"


def format_money(
    value: float,
) -> str:

    return (
        f"R$ {value:,.2f}"
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )


# ===========================================================================
# TELEGRAM / MARKDOWN V2
# ===========================================================================

_MD_V2_SPECIAL = frozenset(
    r"_*[]()~`>#+-=|{}.!"
)


def md_escape(
    text: str,
) -> str:

    return "".join(
        "\\" + char
        if char in _MD_V2_SPECIAL
        else char
        for char in str(text)
    )


# ===========================================================================
# JANELA DE PREGÃO
# ===========================================================================

def get_schedule_config(
    cfg: dict,
) -> dict:

    return (
        cfg.get("schedule")
        or {}
    )


def get_trading_hours_config(
    cfg: dict,
) -> dict:

    return (
        get_schedule_config(cfg)
        .get("trading_hours")
        or {}
    )


def get_schedule_tz(
    cfg: dict,
) -> ZoneInfo:

    schedule_config = (
        get_trading_hours_config(cfg)
    )

    timezone_name = (
        schedule_config.get("timezone")
        or "America/Sao_Paulo"
    )

    try:
        return ZoneInfo(
            timezone_name
        )

    except Exception:

        log.warning(
            "Timezone inválido: %s. "
            "Usando America/Sao_Paulo.",
            timezone_name,
        )

        return TZ_BRASILIA


def is_weekday(
    cfg: dict,
) -> bool:

    trading_config = (
        get_trading_hours_config(cfg)
    )

    if not trading_config.get(
        "weekdays_only",
        True,
    ):
        return True

    now = datetime.now(
        get_schedule_tz(cfg)
    )

    return now.weekday() < 5


def is_trading_window(
    cfg: dict,
) -> bool:
    """
    Verifica se estamos dentro da janela
    configurada para monitoramento.
    """

    trading_config = (
        get_trading_hours_config(cfg)
    )

    if not trading_config.get(
        "enabled",
        True,
    ):
        return True

    timezone = get_schedule_tz(cfg)

    now = datetime.now(
        timezone
    )

    # -----------------------------------------------------------------------
    # FIM DE SEMANA
    # -----------------------------------------------------------------------

    if (
        trading_config.get(
            "weekdays_only",
            True,
        )
        and now.weekday() >= 5
    ):

        return False

    # -----------------------------------------------------------------------
    # HORÁRIOS
    # -----------------------------------------------------------------------

    start_s = (
        trading_config.get("start")
        or "10:00"
    )

    end_s = (
        trading_config.get("end")
        or "17:55"
    )

    try:

        start_hour, start_minute = map(
            int,
            start_s.split(":")[:2],
        )

        end_hour, end_minute = map(
            int,
            end_s.split(":")[:2],
        )

    except Exception:

        log.warning(
            "Horário inválido na configuração. "
            "Usando janela padrão."
        )

        start_hour = 10
        start_minute = 0

        end_hour = 17
        end_minute = 55

    minutes_now = (
        now.hour * 60
        + now.minute
    )

    start_minutes = (
        start_hour * 60
        + start_minute
    )

    end_minutes = (
        end_hour * 60
        + end_minute
    )

    return (
        start_minutes
        <= minutes_now
        <= end_minutes
    )


# ===========================================================================
# MENSAGENS TELEGRAM
# ===========================================================================

def _bloco_status(ativo: dict, hoje, validade_dias: int) -> str:
    ticker = str(ativo.get("ticker") or "")
    falhas: list[str] = []
    symbol, price = resolve_yahoo_symbol(ticker, falhas)
    if price is None:
        log.warning(
            "Cotação indisponível para %s (%s): %s",
            ticker,
            ativo.get("nome_amigavel") or ticker,
            "; ".join(falhas) or "motivo não identificado",
        )
    calculado = referencias.calcular_referencias(ativo, hoje, validade_dias)
    label = md_escape(str(ativo.get("nome_amigavel") or ticker))
    symbol_display = md_escape(display_ticker(symbol))
    lines = [f"*{label}* \\({symbol_display}\\)"]

    if price is None:
        lines.append("⚠️ Preço indisponível no momento\\.")
    else:
        lines.append(f"• Atual: *{md_escape(format_money(price))}*")

    for linha in referencias.linhas_status(calculado):
        if linha.startswith("Pendência"):
            lines.append(f"• _{md_escape(linha)}_")
        else:
            lines.append(f"• {md_escape(linha)}")

    return "\n".join(lines)


def build_status_messages(dados: dict) -> list[str]:
    """Monta o /status a partir das referências, em partes abaixo do limite do Telegram."""
    hoje = referencias.hoje_brasilia()
    validade = int(dados["validade_dias"])
    blocos = [
        "\n".join(
            [
                "*📊 Status dos ativos*",
                "",
                f"_Atualizado: {md_escape(datetime.now(TZ_BRASILIA).strftime('%d/%m/%Y %H:%M'))}_",
            ]
        )
    ]
    for ativo in dados.get("ativos") or []:
        if isinstance(ativo, dict) and ativo.get("ticker"):
            blocos.append(_bloco_status(ativo, hoje, validade))

    mensagens: list[str] = []
    atual = ""
    for bloco in blocos:
        if atual and len(atual) + len(bloco) + 2 > 3500:
            mensagens.append(atual.strip())
            atual = bloco
        else:
            atual = f"{atual}\n\n{bloco}" if atual else bloco
    if atual.strip():
        mensagens.append(atual.strip())
    return mensagens or ["Nenhuma referência cadastrada\\."]


def build_ordens_comparison_message(
    orders: list[OrderConfig],
    *,
    use_close: bool = False,
) -> str:
    """
    Monta comparação entre o preço atual/fechamento
    e as ordens configuradas.
    """

    price_label = (
        "Fechamento"
        if use_close
        else "Preço atual"
    )

    title = (
        "*📊 Resumo do dia — comparação com nossas ordens*"
        if use_close
        else "*🎯 Comparação com nossas ordens*"
    )

    lines = [
        title,
        "",
        (
            f"_"
            f"{md_escape(datetime.now(TZ_BRASILIA).strftime('%d/%m/%Y %H:%M'))}"
            f"_"
        ),
        "",
    ]

    for order in orders:

        if use_close:
            symbol = order.ticker.strip().upper()
            if not symbol.endswith(".SA"):
                symbol = f"{symbol}.SA"
            price = get_daily_close(symbol)
        else:
            symbol, price = resolve_yahoo_symbol(order.ticker)

        label = md_escape(
            order.label
        )

        symbol_display = md_escape(
            display_ticker(symbol)
        )

        # -------------------------------------------------------------------
        # PREÇO INDISPONÍVEL
        # -------------------------------------------------------------------

        if price is None:

            lines.append(
                f"*{label}* "
                f"\\({symbol_display}\\)"
            )

            lines.append(
                f"• {md_escape(price_label)}: "
                f"indisponível"
            )

            lines.append(
                f"• Nossa ordem: "
                f"*{md_escape(format_money(order.preco_ordem))}*"
            )

            lines.append("")

            continue

        # -------------------------------------------------------------------
        # DIFERENÇA
        # -------------------------------------------------------------------

        diff = calc_order_diff_pct(
            order.preco_ordem,
            price,
        )

        if diff >= 0:
            emoji = "🟢"
        else:
            emoji = "🔴"

        lines.append(
            f"*{label}* "
            f"\\({symbol_display}\\)"
        )

        lines.append(
            f"• {md_escape(price_label)}: "
            f"*{md_escape(format_money(price))}*"
        )

        lines.append(
            f"• Nossa ordem: "
            f"*{md_escape(format_money(order.preco_ordem))}*"
        )

        lines.append(
            f"• Diferença: "
            f"{emoji} "
            f"*{md_escape(format_pct(diff))}*"
        )

        lines.append("")

    return "\n".join(
        lines
    ).rstrip()


def build_order_alert_message(
    order: OrderConfig,
    symbol: str,
    price: float,
    preco_ordem: float,
) -> str:
    """
    Mensagem quando o preço atual
    atinge o preço da ordem.
    """

    label = md_escape(
        order.label
    )

    symbol_display = md_escape(
        display_ticker(symbol)
    )

    return (
        f"*🎯 Ordem atingida*\n\n"

        f"*{label}* "
        f"\\({symbol_display}\\)\n\n"

        f"Preço atual: "
        f"*{md_escape(format_money(price))}*\n"

        f"Sua ordem: "
        f"*{md_escape(format_money(preco_ordem))}*\n\n"

        f"🚨 O preço está igual ou abaixo "
        f"do valor da sua ordem\\.\n\n"

        f"_Verifique na corretora se a ordem "
        f"foi executada\\._"
    )


# ===========================================================================
# CONTROLE DE ALERTAS
# ===========================================================================

_alert_episode_keys: set[str] = set()
_alert_lock = threading.Lock()

_order_alert_episode_keys: set[str] = set()

_eod_summary_sent_date: str | None = None
_fundamentals_reminder_sent_month: str | None = None


def run_telegram_polling(bot: telebot.TeleBot) -> None:
    """Inicia polling e aguarda conflitos 409 transitórios entre instâncias."""
    retry_seconds = 10
    while True:
        try:
            bot.infinity_polling(
                skip_pending=True,
                restart_on_change=False,
            )
            return
        except Exception as exc:
            if getattr(exc, "error_code", None) != 409:
                log.exception("❌ Polling encerrado com erro: %s", exc)
                raise

            log.warning(
                "⚠️ Telegram recusou o polling por outra instância (409). "
                "Tentando novamente em %s segundos.",
                retry_seconds,
            )
            time.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, 60)


def _order_alert_episode_key(
    symbol: str,
    preco_ordem: float,
) -> str:

    return (
        f"ordem|{symbol}|{preco_ordem}"
    )


# ===========================================================================
# MONITORAMENTO DE ATIVOS
# ===========================================================================

def run_price_check(
    bot: telebot.TeleBot,
    chat_id: str,
    cfg: dict,
) -> None:
    """Verifica preço justo, preço teto e faixa cadastrados nas referências."""

    if not is_trading_window(cfg):

        log.info(
            "⏸️ Fora da janela de pregão; "
            "pulando verificação de ativos."
        )

        return

    try:
        dados = referencias.carregar_referencias()
    except Exception as exc:
        log.exception("Não foi possível carregar as referências de preço: %s", exc)
        return

    ativos = [item for item in dados.get("ativos") or [] if isinstance(item, dict)]
    if not ativos:
        log.info("Nenhuma referência de preço cadastrada.")
        return

    hoje = referencias.hoje_brasilia()
    pendentes = []

    with _alert_lock:
        for ativo in ativos:
            ticker = str(ativo.get("ticker") or "")
            try:
                symbol, price = resolve_yahoo_symbol(ticker)
                if price is None:
                    log.warning(
                        "Não foi possível obter preço para %s (resolvido: %s)",
                        ticker,
                        symbol,
                    )
                    continue
                sinais = referencias.avaliar_ativo(
                    ativo,
                    price,
                    hoje=hoje,
                    validade_dias=int(dados["validade_dias"]),
                    margem_pct=float(dados["margem_proximidade_pct"]),
                    episodios=_alert_episode_keys,
                )
                pendentes.extend((sinal, symbol, price) for sinal in sinais)
            except Exception as exc:
                log.exception("Erro ao processar referência %s: %s", ticker, exc)

    for sinal, symbol, price in pendentes:
        texto, modo = build_signal_message(sinal, symbol)
        try:
            bot.send_message(chat_id, texto, parse_mode=modo)
            if sinal.tipo == "alerta":
                log.info("🚨 Alerta de %s enviado: %s @ %.2f", sinal.categoria, symbol, price)
            else:
                log.warning("Aviso de referência incompleta enviado: %s (%s)", symbol, sinal.categoria)
        except Exception as exc:
            with _alert_lock:
                _alert_episode_keys.discard(sinal.chave)
            log.exception("Falha ao enviar alerta Telegram: %s", exc)

    with _alert_lock:
        try:
            referencias.salvar_episodios(_alert_episode_keys)
        except Exception as exc:
            log.exception("Falha ao gravar o estado dos alertas: %s", exc)


# ===========================================================================
# MONITORAMENTO DE ORDENS
# ===========================================================================

def run_order_check(
    bot: telebot.TeleBot,
    chat_id: str,
    cfg: dict,
    orders: list[OrderConfig],
) -> None:
    """
    Verifica se o preço atingiu
    as ordens configuradas.
    """

    if not orders:
        return

    if not is_trading_window(cfg):

        log.info(
            "⏸️ Fora da janela de pregão; "
            "pulando verificação de ordens."
        )

        return

    for order in orders:

        try:

            # ---------------------------------------------------------------
            # PREÇO
            # ---------------------------------------------------------------

            symbol, price_try = (
                resolve_yahoo_symbol(
                    order.ticker
                )
            )

            price = price_try

            if price is None:

                log.warning(
                    "Não foi possível obter preço para ordem %s "
                    "(resolvido: %s)",
                    order.ticker,
                    symbol,
                )

                continue

            limite = order.preco_ordem

            key = _order_alert_episode_key(
                symbol,
                limite,
            )

            # ---------------------------------------------------------------
            # ORDEM ATINGIDA
            # ---------------------------------------------------------------

            if price <= limite:

                if key in _order_alert_episode_keys:

                    log.info(
                        "ℹ️ Alerta de ordem já enviado: %s",
                        symbol,
                    )

                    continue

                message = build_order_alert_message(
                    order,
                    symbol,
                    price,
                    limite,
                )

                try:

                    bot.send_message(
                        chat_id,
                        message,
                        parse_mode="MarkdownV2",
                    )

                    _order_alert_episode_keys.add(
                        key
                    )

                    log.info(
                        "🎯 Alerta de ordem enviado: %s @ %.2f",
                        symbol,
                        price,
                    )

                except Exception as e:

                    log.exception(
                        "Falha ao enviar alerta de ordem: %s",
                        e,
                    )

            # ---------------------------------------------------------------
            # PREÇO VOLTOU A SUBIR
            # ---------------------------------------------------------------

            else:

                if key in _order_alert_episode_keys:

                    log.info(
                        "🔄 %s voltou acima da ordem.",
                        symbol,
                    )

                _order_alert_episode_keys.discard(
                    key
                )

        except Exception as e:

            log.exception(
                "Erro ao processar ordem %s: %s",
                order.ticker,
                e,
            )


# ===========================================================================
# RESUMO DE FIM DE DIA
# ===========================================================================

def run_eod_summary(
    bot: telebot.TeleBot,
    chat_id: str,
    cfg: dict,
    orders: list[OrderConfig],
) -> None:
    """
    Envia um resumo diário comparando
    o fechamento com as ordens.
    """

    global _eod_summary_sent_date

    if not orders:

        log.info(
            "ℹ️ Sem ordens para resumo diário."
        )

        return

    if not is_weekday(cfg):

        log.info(
            "ℹ️ Hoje não é dia útil; "
            "resumo diário não será enviado."
        )

        return

    timezone = get_schedule_tz(cfg)

    today = datetime.now(
        timezone
    ).strftime(
        "%Y-%m-%d"
    )

    # Evita envio duplicado no mesmo dia
    if _eod_summary_sent_date == today:

        log.info(
            "ℹ️ Resumo diário já enviado hoje."
        )

        return

    log.info(
        "📊 Gerando resumo diário..."
    )

    message = build_ordens_comparison_message(
        orders,
        use_close=True,
    )

    try:

        bot.send_message(
            chat_id,
            message,
            parse_mode="MarkdownV2",
        )

        _eod_summary_sent_date = today

        log.info(
            "📊 Resumo diário de ordens enviado (%s)",
            today,
        )

    except Exception as e:

        log.exception(
            "Falha ao enviar resumo diário de ordens: %s",
            e,
        )


def run_fundamentals_update_reminder(
    bot: telebot.TeleBot,
    chat_id: str,
    cfg: dict,
    now: datetime | None = None,
) -> None:
    """Lembra mensalmente de revisar os fundamentos em data/referencias_precos.json."""
    global _fundamentals_reminder_sent_month

    schedule_config = get_schedule_config(cfg)
    timezone = get_schedule_tz(cfg)
    current = now or datetime.now(timezone)

    try:
        reminder_day = int(schedule_config.get("fundamentals_update_reminder_day", 1))
    except (TypeError, ValueError):
        reminder_day = 1
    reminder_day = max(1, min(reminder_day, 31))
    reminder_day = min(
        reminder_day,
        calendar.monthrange(current.year, current.month)[1],
    )
    reminder_time = str(schedule_config.get("fundamentals_update_reminder_time") or "21:00")

    if current.day != reminder_day or current.strftime("%H:%M") < reminder_time:
        return

    month_key = current.strftime("%Y-%m")
    if _fundamentals_reminder_sent_month == month_key:
        return

    try:
        dados_referencias = referencias.carregar_referencias()
        fundamentos = ", ".join(referencias.tickers_com_fundamentos(dados_referencias))
    except Exception as exc:
        log.exception("Não foi possível listar fundamentos para o lembrete: %s", exc)
        fundamentos = ""

    message = (
        "🔔 Lembrete mensal — revisar fundamentos do B3 Sniper\n\n"
        "Revise data/referencias_precos.json. Este lembrete não atualiza os dados automaticamente.\n"
        f"• Preço justo: origem, data e insumos de {fundamentos or 'nenhum ativo cadastrado'}.\n"
        "• FIIs: VPA, fonte e data de consulta.\n"
        "• Ações e ETFs: máxima de 52 semanas, fonte e data da cotação.\n"
        "• Confira dados ausentes, inválidos ou vencidos.\n"
        "• Execute as validações existentes antes de considerar a atualização concluída.\n\n"
        "Após alterar os valores usados no Render, faça commit e deploy para aplicar a atualização."
    )

    try:
        bot.send_message(chat_id, message)
        _fundamentals_reminder_sent_month = month_key
        log.info("🔔 Lembrete mensal de fundamentos enviado (%s).", month_key)
    except Exception as exc:
        log.exception("Falha ao enviar lembrete mensal de fundamentos: %s", exc)


# ===========================================================================
# HEARTBEAT
# ===========================================================================

def heartbeat():
    """
    Gera logs periódicos para confirmar
    que o serviço continua ativo.
    """

    while True:

        try:

            log.info(
                "💓 B3 Sniper online - %s",
                datetime.now(
                    TZ_BRASILIA
                ).strftime(
                    "%d/%m/%Y %H:%M:%S"
                ),
            )

        except Exception as e:

            log.warning(
                "Erro no heartbeat: %s",
                e,
            )

        # 30 minutos
        time.sleep(1800)


# ===========================================================================
# MAIN
# ===========================================================================

def main() -> None:

    log.info(
        "🚀 Iniciando B3 Sniper..."
    )

    # -----------------------------------------------------------------------
    # CONFIGURAÇÃO
    # -----------------------------------------------------------------------

    cfg = load_config()

    token = get_bot_token(cfg)

    try:
        dados_referencias = referencias.carregar_referencias()
        quantidade_referencias = len(dados_referencias.get("ativos") or [])
    except Exception as exc:
        quantidade_referencias = 0
        log.exception("Referências de preço indisponíveis na inicialização: %s", exc)

    with _alert_lock:
        _alert_episode_keys.update(referencias.carregar_episodios())

    orders = load_ordens()

    # -----------------------------------------------------------------------
    # CHAT ID
    # -----------------------------------------------------------------------

    chat_id = (
        os.environ.get(
            "TELEGRAM_CHAT_ID",
            "",
        )
        .strip()
    )

    if not chat_id:

        chat_id = (
            (cfg.get("telegram") or {})
            .get("chat_id")
            or ""
        )

        chat_id = str(
            chat_id
        ).strip()

    if not chat_id:

        raise ValueError(
            "Defina TELEGRAM_CHAT_ID no ambiente "
            "ou telegram.chat_id no arquivo de configuração."
        )

    # -----------------------------------------------------------------------
    # TELEGRAM
    # -----------------------------------------------------------------------

    bot = telebot.TeleBot(
        token,
        parse_mode=None,
    )

    # -----------------------------------------------------------------------
    # INTERVALO
    # -----------------------------------------------------------------------

    schedule_config = (
        get_schedule_config(cfg)
    )

    try:

        interval = int(
            schedule_config.get(
                "interval_minutes",
                60,
            )
        )

    except (
        TypeError,
        ValueError,
    ):

        interval = 60

    if interval < 1:
        interval = 1

    # -----------------------------------------------------------------------
    # JOB PRINCIPAL
    # -----------------------------------------------------------------------

    def job():

        try:

            log.info(
                "🔎 Iniciando verificação de preços..."
            )

            run_price_check(
                bot,
                chat_id,
                cfg,
            )

            run_order_check(
                bot,
                chat_id,
                cfg,
                orders,
            )

            log.info(
                "✅ Verificação concluída."
            )

        except Exception as e:

            log.exception(
                "Erro no job agendado: %s",
                e,
            )

    # -----------------------------------------------------------------------
    # AGENDA MONITORAMENTO
    # -----------------------------------------------------------------------

    schedule.every(
        interval
    ).minutes.do(
        job
    )

    # -----------------------------------------------------------------------
    # RESUMO DIÁRIO
    # -----------------------------------------------------------------------

    eod_time = (
        schedule_config.get(
            "eod_summary_time"
        )
        or "21:00"
    )

    try:

        schedule.every().day.at(
            eod_time
        ).do(
            lambda: run_eod_summary(
                bot,
                chat_id,
                cfg,
                orders,
            )
        )

        log.info(
            "⏰ Resumo diário agendado para %s",
            eod_time,
        )

    except Exception as e:

        log.exception(
            "Erro ao agendar resumo diário (%s): %s",
            eod_time,
            e,
        )

    reminder_day = schedule_config.get(
        "fundamentals_update_reminder_day",
        1,
    )
    reminder_time = (
        schedule_config.get("fundamentals_update_reminder_time")
        or eod_time
    )

    try:
        schedule.every().day.at(reminder_time).do(
            lambda: run_fundamentals_update_reminder(
                bot,
                chat_id,
                cfg,
            )
        )
        log.info(
            "⏰ Lembrete mensal de fundamentos agendado para o dia %s às %s.",
            reminder_day,
            reminder_time,
        )
        # Recupera o lembrete se o serviço iniciou depois do horário nesse dia.
        run_fundamentals_update_reminder(bot, chat_id, cfg)
    except Exception as e:
        log.exception(
            "Erro ao agendar lembrete mensal de fundamentos (%s): %s",
            reminder_time,
            e,
        )

    # -----------------------------------------------------------------------
    # COMANDOS TELEGRAM
    # -----------------------------------------------------------------------

    @bot.message_handler(
        commands=["start", "help"]
    )
    def send_welcome(message):

        try:

            text = (
                "*🤖 B3 Sniper*\n\n"

                "*Comandos disponíveis:*\n\n"

                "📊 /status — preços atuais dos ativos\n"
                "🎯 /ordens — comparação com suas ordens\n"
                "❓ /help — esta mensagem\n\n"

                "_Fonte: Yahoo Finance "
                "\\(pode haver atraso conforme o ativo\\)\\._"
            )

            bot.reply_to(
                message,
                text,
                parse_mode="MarkdownV2",
            )

        except Exception as e:

            log.exception(
                "Erro em /help: %s",
                e,
            )

            bot.reply_to(
                message,
                "Erro ao formatar ajuda. "
                "Tente /status.",
            )

    # -----------------------------------------------------------------------

    @bot.message_handler(
        commands=["ordens"]
    )
    def cmd_ordens(message):

        try:

            current_orders = load_ordens()

            if not current_orders:

                bot.reply_to(
                    message,
                    (
                        "Nenhuma ordem encontrada em ordens.json."
                    ),
                )

                return

            response = (
                build_ordens_comparison_message(
                    current_orders,
                    use_close=False,
                )
            )

            bot.reply_to(
                message,
                response,
                parse_mode="MarkdownV2",
            )

        except Exception as e:

            log.exception(
                "Erro em /ordens: %s",
                e,
            )

            try:

                bot.reply_to(
                    message,
                    (
                        "Não foi possível montar "
                        "a comparação agora. "
                        "Tente novamente em instantes."
                    ),
                )

            except Exception:
                pass

    # -----------------------------------------------------------------------

    @bot.message_handler(
        commands=["status"]
    )
    def cmd_status(message):

        try:

            dados = referencias.carregar_referencias()
            mensagens = build_status_messages(dados)
            for indice, texto in enumerate(mensagens):
                if indice == 0:
                    bot.reply_to(message, texto, parse_mode="MarkdownV2")
                else:
                    bot.send_message(message.chat.id, texto, parse_mode="MarkdownV2")

        except Exception as e:

            log.exception(
                "Erro em /status: %s",
                e,
            )

            try:

                bot.reply_to(
                    message,
                    (
                        "Não foi possível montar "
                        "o status agora. "
                        "Tente novamente em instantes."
                    ),
                )

            except Exception:
                pass

    # -----------------------------------------------------------------------
    # SCHEDULER
    # -----------------------------------------------------------------------

    def scheduler_loop():

        log.info(
            "⏰ Scheduler iniciado."
        )

        while True:

            try:

                schedule.run_pending()

            except Exception as e:

                log.exception(
                    "Erro no scheduler: %s",
                    e,
                )

            time.sleep(10)

    threading.Thread(
        target=scheduler_loop,
        daemon=True,
        name="scheduler",
    ).start()

    # -----------------------------------------------------------------------
    # PRIMEIRA VERIFICAÇÃO
    # -----------------------------------------------------------------------

    def initial_check():

        time.sleep(5)

        log.info(
            "🚀 Executando primeira verificação..."
        )

        job()

    threading.Thread(
        target=initial_check,
        daemon=True,
        name="initial-check",
    ).start()

    # -----------------------------------------------------------------------
    # SERVIDOR WEB
    # -----------------------------------------------------------------------

    threading.Thread(
        target=run_web_server,
        daemon=True,
        name="web-server",
    ).start()

    # -----------------------------------------------------------------------
    # HEARTBEAT
    # -----------------------------------------------------------------------

    threading.Thread(
        target=heartbeat,
        daemon=True,
        name="heartbeat",
    ).start()

    # -----------------------------------------------------------------------
    # LOG INICIAL
    # -----------------------------------------------------------------------

    log.info(
        "🤖 Bot iniciado. "
        "Intervalo: %s min. "
        "Chat alertas: %s. "
        "Referências: %s. "
        "Ordens: %s. "
        "Resumo EOD: %s. "
        "Timezone: %s",
        interval,
        chat_id,
        quantidade_referencias,
        len(orders),
        eod_time,
        time.tzname,
    )

    # -----------------------------------------------------------------------
    # TELEGRAM POLLING
    # -----------------------------------------------------------------------

    log.info("📡 Iniciando Telegram polling...")
    run_telegram_polling(bot)


# ===========================================================================
# START
# ===========================================================================

if __name__ == "__main__":
    main()
