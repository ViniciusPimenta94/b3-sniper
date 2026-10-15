"""
B3 Sniper
Bot Telegram para monitoramento de oportunidades e ordens na B3.

Funcionalidades:
- Monitoramento de preço teto dos ativos
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
import math
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

    # Mescla os alertas padrão do arquivo versionado na configuração local,
    # preservando todos os valores preenchidos localmente.
    if config_file != CONFIG_PATH and CONFIG_PATH.is_file():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            defaults = json.load(f).get("opportunity_alerts", {})
        configured = config.get("opportunity_alerts") or {}
        merged = {**defaults, **configured}
        for category in ("venda", "compra", "analise"):
            merged.setdefault(category, defaults.get(category, []))
        config["opportunity_alerts"] = merged

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
    Carrega os ativos configurados.
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


def parse_opportunity_rules(cfg: dict) -> list[dict]:
    """Carrega regras configuradas para os três tipos de oportunidade."""
    section = cfg.get("opportunity_alerts") or {}
    rules: list[dict] = []

    categories = ("venda", "compra", "analise")

    try:
        margin_value = float(section.get("margem_proximidade_pct", 0))
        margin = max(0.0, margin_value) / 100 if math.isfinite(margin_value) else 0.0
    except (TypeError, ValueError):
        margin = 0.0
        log.warning("Margem de proximidade inválida; usando 0%%.")

    for category in categories:
        entries = section.get(category) or []
        if not isinstance(entries, list):
            log.warning("A lista de oportunidade '%s' deve ser uma lista.", category)
            continue

        for item in entries:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker", "")).strip()
            if not ticker:
                log.warning("Regra de oportunidade sem ticker ignorada: %s", item)
                continue

            rule = {"category": category, "ticker": ticker,
                    "label": str(item.get("nome_amigavel") or ticker).strip(),
                    "margin": margin, "configured": item}
            try:
                if category == "venda":
                    yield_pct = float(item.get("dividend_yield_minimo_pct", 6))
                    if yield_pct <= 0 or not math.isfinite(yield_pct):
                        raise ValueError("yield mínimo deve ser positivo")
                    rule["yield_pct"] = yield_pct
                elif category == "compra":
                    # LPA e VPA são preenchidos manualmente em opportunity_alerts.
                    pass
                else:
                    low, high = float(item["preco_minimo"]), float(item["preco_maximo"])
                    if low < 0 or high < low:
                        raise ValueError("Faixa de preço inválida")
                    rule["low"], rule["high"] = low, high
                    rule["basis"] = "Faixa de preço configurada"
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Regra de oportunidade inválida (%s): %s", ticker, exc)
                continue
            rules.append(rule)

    return rules


def _valid_positive_number(value) -> float | None:
    try:
        number = float(value)
        if number > 0 and math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return None


# ===========================================================================
# YAHOO FINANCE
# ===========================================================================

def _price_from_ticker(
    ticker: yf.Ticker,
) -> float | None:
    """
    Obtém o último preço conhecido.
    """

    # -----------------------------------------------------------------------
    # FAST INFO
    # -----------------------------------------------------------------------

    try:

        fi = ticker.fast_info

        if fi is not None:

            for key in (
                "last_price",
                "regular_market_price",
                "previous_close",
            ):

                try:

                    value = (
                        getattr(
                            fi,
                            key,
                            None,
                        )
                        if not isinstance(
                            fi,
                            dict,
                        )
                        else fi.get(key)
                    )

                    if (
                        value is not None
                        and float(value) > 0
                    ):

                        return float(value)

                except Exception:
                    continue

    except Exception as e:

        log.debug(
            "fast_info falhou para %s: %s",
            getattr(
                ticker,
                "ticker",
                "desconhecido",
            ),
            e,
        )

    # -----------------------------------------------------------------------
    # HISTORY
    # -----------------------------------------------------------------------

    try:

        hist = ticker.history(
            period="5d",
            interval="1d",
        )

        if (
            hist is not None
            and not hist.empty
            and "Close" in hist.columns
        ):

            value = hist["Close"].iloc[-1]

            if value is not None:

                value = float(value)

                if value > 0:
                    return value

    except Exception as e:

        log.debug(
            "history falhou para %s: %s",
            getattr(
                ticker,
                "ticker",
                "desconhecido",
            ),
            e,
        )

    return None


def resolve_yahoo_symbol(
    user_ticker: str,
) -> tuple[str, float | None]:
    """
    Valida o ticker no Yahoo Finance.

    Se o ticker puro falhar, tenta automaticamente
    adicionar .SA.

    Exemplos:

    CMIG4 -> CMIG4 -> CMIG4.SA
    XPLG11 -> XPLG11 -> XPLG11.SA
    """

    base = user_ticker.strip().upper()

    if not base:
        return base, None

    candidates = [base]

    if not base.endswith(".SA"):
        candidates.append(
            f"{base}.SA"
        )

    last_symbol = candidates[-1]

    for symbol in candidates:

        last_symbol = symbol

        try:

            ticker = yf.Ticker(symbol)

            price = _price_from_ticker(
                ticker
            )

            if price is not None:
                return symbol, price

        except Exception as e:

            log.debug(
                "Erro ao consultar %s: %s",
                symbol,
                e,
            )

            continue

    return last_symbol, None


def get_current_price(
    symbol: str,
) -> float | None:
    """
    Obtém o preço atual ou último preço disponível.
    """

    try:

        ticker = yf.Ticker(symbol)

        return _price_from_ticker(
            ticker
        )

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

def build_status_message(
    cfg: dict,
    assets: list[AssetConfig],
) -> str:
    """
    Monta mensagem do comando /status.
    """

    lines = [
        "*📊 Status dos ativos*",
        "",
        (
            f"_Atualizado: "
            f"{md_escape(datetime.now(TZ_BRASILIA).strftime('%d/%m/%Y %H:%M'))}_"
        ),
        "",
    ]

    for asset in assets:

        symbol, price_try = (
            resolve_yahoo_symbol(
                asset.ticker
            )
        )

        price = (
            price_try
            if price_try is not None
            else get_current_price(symbol)
        )

        label = md_escape(
            asset.label
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
                "⚠️ Preço indisponível no momento\\."
            )

            lines.append("")

            continue

        teto = asset.preco_teto

        # -------------------------------------------------------------------
        # SITUAÇÃO
        # -------------------------------------------------------------------

        if price > teto:

            emoji = "📈"

            situacao = (
                "Acima do preço teto"
            )

        elif price == teto:

            emoji = "🎯"

            situacao = (
                "No preço teto"
            )

        else:

            emoji = "🚨"

            situacao = (
                "Abaixo do preço teto"
            )

        # -------------------------------------------------------------------
        # DIFERENÇA
        # -------------------------------------------------------------------

        diff_pct = (
            (price / teto - 1)
            * 100
        ) if teto > 0 else 0

        lines.append(
            f"{emoji} *{label}* "
            f"\\({symbol_display}\\)"
        )

        lines.append(
            f"• Atual: "
            f"*{md_escape(format_money(price))}*"
        )

        lines.append(
            f"• Teto: "
            f"*{md_escape(format_money(teto))}*"
        )

        lines.append(
            f"• Diferença: "
            f"*{md_escape(format_pct(diff_pct))}*"
        )

        lines.append(
            f"• _{md_escape(situacao)}_"
        )

        lines.append("")

    return "\n".join(
        lines
    ).rstrip()


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

        symbol, _ = (
            resolve_yahoo_symbol(
                order.ticker
            )
        )

        price = (
            get_daily_close(symbol)
            if use_close
            else get_current_price(symbol)
        )

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


def build_opportunity_message(rule: dict, symbol: str, price: float) -> str:
    """Monta um alerta específico de venda, compra ou análise."""
    labels = {
        "venda": ("📈 Alerta de oportunidade de venda", "Preço teto", "O preço está próximo ou acima do preço teto."),
        "compra": ("📉 Alerta de oportunidade de compra", "Preço justo", "O preço está próximo ou abaixo do preço justo."),
        "analise": ("🔎 Alerta de oportunidade de análise", "Faixa configurada", "O preço atual está dentro da faixa definida."),
    }
    title, reference_label, description = labels[rule["category"]]
    label = md_escape(rule["label"])
    symbol_display = md_escape(display_ticker(symbol))
    lines = [
        f"*{title}*", "", f"*{label}* \\({symbol_display}\\)", "",
        f"Preço atual: *{md_escape(format_money(price))}*",
    ]
    if rule["category"] == "analise":
        lines.append(
            f"{reference_label}: *{md_escape(format_money(rule['low']))} a "
            f"{md_escape(format_money(rule['high']))}*"
        )
    else:
        lines.append(
            f"{reference_label}: *{md_escape(format_money(rule['target']))}*"
        )
        if rule["category"] == "venda":
            lines.append(
                f"DPA 12M: *{md_escape(format_money(rule['dpa_12m']))}* | "
                f"Yield mínimo: *{rule['yield_pct']:g}%*"
            )
        else:
            lines.append(
                f"LPA: *{md_escape(format_money(rule['lpa']))}* | "
                f"VPA: *{md_escape(format_money(rule['vpa']))}*"
            )
    lines.extend(["", md_escape(description), f"_{md_escape(rule['basis'])}_"])
    return "\n".join(lines)


def build_missing_fundamentals_message(rule: dict, symbol: str, price: float, missing: list[str]) -> str:
    """Informa que os dados faltantes impediram avaliar a oportunidade."""
    category = "venda" if rule["category"] == "venda" else "compra"
    return (
        f"⚠️ Alerta de oportunidade de {category} não calculado\n\n"
        f"Ativo: {rule['label']} ({display_ticker(symbol)})\n"
        f"Preço atual: {format_money(price)}\n"
        f"Dados ausentes ou inválidos: {', '.join(missing)}\n\n"
        "O limite não foi calculado e nenhum sinal de compra/venda foi emitido."
    )


# ===========================================================================
# CONTROLE DE ALERTAS
# ===========================================================================

_alert_episode_keys: set[str] = set()

_order_alert_episode_keys: set[str] = set()

_eod_summary_sent_date: str | None = None
_fundamentals_reminder_sent_month: str | None = None


def _missing_fundamentals_key(category: str, symbol: str) -> str:
    return f"dados_pendentes|{category}|{symbol}"


def _clear_missing_fundamentals_alert(category: str, symbol: str) -> None:
    _alert_episode_keys.discard(_missing_fundamentals_key(category, symbol))


def _send_missing_fundamentals_alert(
    bot: telebot.TeleBot,
    chat_id: str,
    rule: dict,
    symbol: str,
    price: float,
    missing: list[str],
) -> None:
    key = _missing_fundamentals_key(rule["category"], symbol)
    if key in _alert_episode_keys:
        return
    try:
        bot.send_message(
            chat_id,
            build_missing_fundamentals_message(rule, symbol, price, missing),
        )
        _alert_episode_keys.add(key)
        log.warning("Aviso enviado; fundamentos indisponíveis para %s: %s", symbol, missing)
    except Exception as exc:
        log.exception("Falha ao avisar fundamentos ausentes de %s: %s", symbol, exc)


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
    assets: list[AssetConfig],
) -> None:
    """
    Verifica os alertas de oportunidade de venda, compra e análise.
    A lista `assets` permanece no parâmetro por compatibilidade com a chamada
    atual; os alertas são definidos exclusivamente em opportunity_alerts.
    """

    rules = parse_opportunity_rules(cfg)
    if not rules:
        log.info("Nenhuma regra de alerta de oportunidade configurada.")
        return

    if not is_trading_window(cfg):

        log.info(
            "⏸️ Fora da janela de pregão; "
            "pulando verificação de ativos."
        )

        return

    for rule in rules:

        try:

            # ---------------------------------------------------------------
            # PREÇO
            # ---------------------------------------------------------------

            symbol, price_try = (
                resolve_yahoo_symbol(
                    rule["ticker"]
                )
            )

            price = (
                price_try
                if price_try is not None
                else get_current_price(symbol)
            )

            if price is None:

                log.warning(
                    "Não foi possível obter preço para %s "
                    "(resolvido: %s)",
                    rule["ticker"],
                    symbol,
                )

                continue

            category = rule["category"]
            if category == "venda":
                configured_dpa = rule["configured"].get("dpa_12m")
                dpa = _valid_positive_number(configured_dpa)
                missing = [] if dpa is not None else ["DPA 12M (campo dpa_12m na configuração)"]
                if missing:
                    _send_missing_fundamentals_alert(bot, chat_id, rule, symbol, price, missing)
                    continue
                _clear_missing_fundamentals_alert(category, symbol)
                rule["dpa_12m"] = dpa
                target = dpa / (rule["yield_pct"] / 100)
                rule["target"] = target
                rule["basis"] = f"Preço teto = DPA 12M / yield mínimo de {rule['yield_pct']:g}%"
                active = price >= target * (1 - rule["margin"])
                reference_key = f"{target:.8f}"
            elif category == "compra":
                configured = rule["configured"]
                lpa = _valid_positive_number(configured.get("lpa"))
                vpa = _valid_positive_number(configured.get("vpa"))
                missing = []
                if lpa is None:
                    missing.append("LPA (campo lpa na configuração)")
                if vpa is None:
                    missing.append("VPA (campo vpa na configuração)")
                if missing:
                    _send_missing_fundamentals_alert(bot, chat_id, rule, symbol, price, missing)
                    continue
                _clear_missing_fundamentals_alert(category, symbol)
                rule["lpa"], rule["vpa"] = lpa, vpa
                target = (22.5 * lpa * vpa) ** 0.5
                rule["target"] = target
                rule["basis"] = "Preço justo = √(22,5 × LPA × VPA)"
                active = price <= target * (1 + rule["margin"])
                reference_key = f"{target:.8f}"
            else:
                active = rule["low"] <= price <= rule["high"]
                reference_key = f"{rule['low']:.8f}:{rule['high']:.8f}"

            key = f"{category}|{symbol}|{reference_key}"
            if active:
                if key in _alert_episode_keys:
                    log.info("ℹ️ Alerta %s já enviado: %s", category, symbol)
                    continue
                try:
                    bot.send_message(
                        chat_id,
                        build_opportunity_message(rule, symbol, price),
                        parse_mode="MarkdownV2",
                    )
                    _alert_episode_keys.add(key)
                    log.info("🚨 Alerta de %s enviado: %s @ %.2f", category, symbol, price)
                except Exception as e:
                    log.exception("Falha ao enviar alerta Telegram: %s", e)
            else:
                _alert_episode_keys.discard(key)

        except Exception as e:

            log.exception(
                "Erro ao processar regra %s: %s",
                rule.get("ticker", "?"),
                e,
            )


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

            price = (
                price_try
                if price_try is not None
                else get_current_price(symbol)
            )

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
    """Lembra mensalmente de atualizar DPA, LPA e VPA na configuração."""
    global _fundamentals_reminder_sent_month

    schedule_config = get_schedule_config(cfg)
    opportunity_config = cfg.get("opportunity_alerts") or {}
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

    sale_tickers = ", ".join(
        str(item.get("ticker", ""))
        for item in opportunity_config.get("venda", [])
        if isinstance(item, dict)
    ) or "nenhum ativo cadastrado"
    buy_tickers = ", ".join(
        str(item.get("ticker", ""))
        for item in opportunity_config.get("compra", [])
        if isinstance(item, dict)
    ) or "nenhum ativo cadastrado"

    message = (
        "🔔 Lembrete mensal — atualizar fundamentos do B3 Sniper\n\n"
        f"Atualize a seção opportunity_alerts da configuração:\n"
        f"• Venda: preencher dpa_12m para {sale_tickers}.\n"
        f"• Compra: preencher lpa e vpa para {buy_tickers}.\n\n"
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

    assets = parse_assets(cfg)

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
                assets,
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

            response = build_status_message(
                cfg,
                assets,
            )

            bot.reply_to(
                message,
                response,
                parse_mode="MarkdownV2",
            )

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
        "Ativos: %s. "
        "Ordens: %s. "
        "Resumo EOD: %s. "
        "Timezone: %s",
        interval,
        chat_id,
        len(assets),
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
