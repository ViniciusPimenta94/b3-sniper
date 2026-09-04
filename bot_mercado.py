"""
B3 Sniper
Bot Telegram: alertas quando preço de mercado <= preço teto (oportunidade).

Usa:
- yfinance
- pyTelegramBotAPI
- schedule
- Flask
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import schedule
import telebot
import yfinance as yf
from flask import Flask


# ===========================================================================
# CONFIGURAÇÃO GERAL
# ===========================================================================

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
ORDENS_PATH = Path(__file__).resolve().parent / "ordens.json"

TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger(__name__)


# ===========================================================================
# SERVIDOR WEB - RENDER / UPTIMEROBOT
# ===========================================================================

app = Flask(__name__)


@app.before_request
def log_request():
    log.info(
        "🌐 Ping recebido - %s %s - %s",
        datetime.now(TZ_BRASILIA).strftime("%d/%m/%Y %H:%M:%S"),
        os.environ.get("RENDER_EXTERNAL_URL", ""),
        "Requisição HTTP",
    )


@app.route("/")
def home():
    return "🤖 B3 Sniper Online!"


@app.route("/health")
def health():
    return {
        "status": "online",
        "service": "B3 Sniper",
        "time": datetime.now(TZ_BRASILIA).isoformat(),
    }, 200


def run_web_server():
    """
    Inicia servidor HTTP necessário para o Render Web Service.
    Executa em thread separada para não bloquear o Telegram.
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
# CONFIG.JSON
# ===========================================================================


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"Arquivo de configuração não encontrado: {CONFIG_PATH}"
        )

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    # Garante que a seção telegram exista
    if "telegram" not in config:
        config["telegram"] = {}

    # Variáveis de ambiente têm prioridade no Render
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if telegram_token:
        config["telegram"]["bot_token"] = telegram_token

    if telegram_chat_id:
        config["telegram"]["chat_id"] = telegram_chat_id

    return config


def get_bot_token(cfg: dict) -> str:
    """
    Prioridade:
    1. TELEGRAM_BOT_TOKEN do ambiente
    2. telegram.bot_token do config.json
    """

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()

    if token:
        return token

    token = (cfg.get("telegram") or {}).get("bot_token") or ""
    token = str(token).strip()

    if not token:
        raise ValueError(
            "Token do bot ausente. "
            "Defina TELEGRAM_BOT_TOKEN "
            "ou telegram.bot_token em config.json"
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
        return self.nome_amigavel or display_ticker(self.ticker)


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

    if not ORDENS_PATH.is_file():
        log.info(
            "Arquivo ordens.json não encontrado; "
            "monitoramento de ordens desativado."
        )

        return []

    with open(ORDENS_PATH, encoding="utf-8") as f:
        data = json.load(f)

    raw = data.get("ordens") or []

    out: list[OrderConfig] = []

    for item in raw:

        if not isinstance(item, dict):
            continue

        ticker = str(item.get("ticker", "")).strip()

        if not ticker:
            continue

        try:
            preco_ordem = float(item.get("preco_ordem"))

        except (TypeError, ValueError):

            log.warning(
                "Ignorando ordem sem preco_ordem válido: %s",
                item,
            )

            continue

        nome = item.get("nome_amigavel")

        nome = str(nome).strip() if nome else None

        out.append(
            OrderConfig(
                ticker=ticker,
                preco_ordem=preco_ordem,
                nome_amigavel=nome,
            )
        )

    return out


def parse_assets(cfg: dict) -> list[AssetConfig]:

    raw = cfg.get("assets") or []

    out: list[AssetConfig] = []

    for item in raw:

        if not isinstance(item, dict):
            continue

        ticker = str(item.get("ticker", "")).strip()

        if not ticker:
            continue

        try:
            preco_teto = float(item.get("preco_teto"))

        except (TypeError, ValueError):

            log.warning(
                "Ignorando ativo sem preco_teto válido: %s",
                item,
            )

            continue

        nome = item.get("nome_amigavel")

        nome = str(nome).strip() if nome else None

        out.append(
            AssetConfig(
                ticker=ticker,
                preco_teto=preco_teto,
                nome_amigavel=nome,
            )
        )

    return out


# ===========================================================================
# YAHOO FINANCE
# ===========================================================================


def _price_from_ticker(ticker: yf.Ticker) -> float | None:
    """
    Obtém último preço conhecido.
    """

    try:

        fi = ticker.fast_info

        if fi is not None:

            for key in (
                "last_price",
                "regular_market_price",
                "previous_close",
            ):

                value = (
                    getattr(fi, key, None)
                    if not isinstance(fi, dict)
                    else fi.get(key)
                )

                if value is not None and float(value) > 0:
                    return float(value)

    except Exception as e:

        log.debug(
            "fast_info falhou para %s: %s",
            ticker.ticker,
            e,
        )

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

            return float(hist["Close"].iloc[-1])

    except Exception as e:

        log.debug(
            "history falhou para %s: %s",
            ticker.ticker,
            e,
        )

    return None


def resolve_yahoo_symbol(
    user_ticker: str,
) -> tuple[str, float | None]:

    """
    Valida no Yahoo Finance.

    Se o símbolo puro falhar,
    tenta automaticamente com .SA.
    """

    base = user_ticker.strip().upper()

    if not base:
        return base, None

    candidates = [base]

    if not base.endswith(".SA"):
        candidates.append(f"{base}.SA")

    last_symbol = candidates[-1]

    for symbol in candidates:

        last_symbol = symbol

        try:

            ticker = yf.Ticker(symbol)

            price = _price_from_ticker(ticker)

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


def get_current_price(symbol: str) -> float | None:

    try:

        ticker = yf.Ticker(symbol)

        return _price_from_ticker(ticker)

    except Exception as e:

        log.warning(
            "Falha ao obter preço de %s: %s",
            symbol,
            e,
        )

        return None


def get_daily_close(symbol: str) -> float | None:
    """
    Fechamento do último pregão disponível.
    """

    try:

        hist = yf.Ticker(symbol).history(
            period="5d",
            interval="1d",
        )

        if (
            hist is not None
            and not hist.empty
            and "Close" in hist.columns
        ):

            return float(hist["Close"].iloc[-1])

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
    Diferença percentual:

    (ordem / referência - 1) * 100
    """

    if preco_referencia <= 0:
        return 0.0

    return (
        preco_ordem / preco_referencia - 1.0
    ) * 100.0


def format_pct(value: float) -> str:

    result = f"{value:+.1f}".replace(".", ",")

    if value >= 0 and not result.startswith("+"):
        result = "+" + result

    return result + "%"


# ===========================================================================
# TELEGRAM / MARKDOWN
# ===========================================================================


_MD_V2_SPECIAL = frozenset(
    r"_*[]()~`>#+-=|{}.!"
)


def md_escape(text: str) -> str:

    return "".join(
        "\\" + char
        if char in _MD_V2_SPECIAL
        else char

        for char in str(text)
    )


def format_money(value: float) -> str:

    return (
        f"R$ {value:,.2f}"
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )


# ===========================================================================
# JANELA DE PREGÃO
# ===========================================================================


def is_trading_window(cfg: dict) -> bool:

    schedule_config = (
        (cfg.get("schedule") or {})
        .get("trading_hours")
        or {}
    )

    if not schedule_config.get(
        "enabled",
        True,
    ):
        return True

    timezone_name = (
        schedule_config.get("timezone")
        or "America/Sao_Paulo"
    )

    try:
        timezone = ZoneInfo(timezone_name)

    except Exception:
        timezone = TZ_BRASILIA

    now = datetime.now(timezone)

    if (
        schedule_config.get(
            "weekdays_only",
            True,
        )
        and now.weekday() >= 5
    ):
        return False

    start_s = (
        schedule_config.get("start")
        or "10:00"
    )

    end_s = (
        schedule_config.get("end")
        or "17:55"
    )

    start_hour, start_minute = map(
        int,
        start_s.split(":")[:2],
    )

    end_hour, end_minute = map(
        int,
        end_s.split(":")[:2],
    )

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


def get_schedule_tz(
    cfg: dict,
) -> ZoneInfo:

    schedule_config = (
        (cfg.get("schedule") or {})
        .get("trading_hours")
        or {}
    )

    timezone_name = (
        schedule_config.get("timezone")
        or "America/Sao_Paulo"
    )

    try:
        return ZoneInfo(timezone_name)

    except Exception:
        return TZ_BRASILIA


def is_weekday(cfg: dict) -> bool:

    schedule_config = (
        (cfg.get("schedule") or {})
        .get("trading_hours")
        or {}
    )

    if not schedule_config.get(
        "weekdays_only",
        True,
    ):
        return True

    return (
        datetime.now(
            get_schedule_tz(cfg)
        ).weekday()
        < 5
    )


# ===========================================================================
# MENSAGENS TELEGRAM
# ===========================================================================


def build_status_message(
    cfg: dict,
    assets: list[AssetConfig],
) -> str:

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

        symbol, _ = resolve_yahoo_symbol(
            asset.ticker
        )

        price = get_current_price(symbol)

        label = md_escape(asset.label)

        symbol_escaped = md_escape(symbol)

        if price is None:

            lines.append(
                f"*{label}* "
                f"\\(`{symbol_escaped}`\\)"
            )

            lines.append(
                "⚠️ Preço indisponível no momento\\."
            )

            lines.append("")

            continue

        teto = asset.preco_teto

        if price > teto:

            emoji = "📈"

            situacao = (
                "Acima do preço teto"
            )

        elif price == teto:

            emoji = "📉"

            situacao = (
                "Igual ao preço teto "
                "(oportunidade)"
            )

        else:

            emoji = "📉"

            situacao = (
                "Abaixo do preço teto "
                "(oportunidade)"
            )

        lines.append(
            f"{emoji} *{label}* "
            f"\\(`{symbol_escaped}`\\)"
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
            f"• _{md_escape(situacao)}_"
        )

        lines.append("")

    return "\n".join(lines).rstrip()


def build_ordens_comparison_message(
    orders: list[OrderConfig],
    *,
    use_close: bool = False,
) -> str:

    price_label = (
        "Fechamento"
        if use_close
        else "Preço atual"
    )

    title = (
        "*📊 Resumo do dia — comparação com nossas ordens*"
        if use_close
        else "*📊 Comparação com nossas ordens*"
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

        symbol, _ = resolve_yahoo_symbol(
            order.ticker
        )

        price = (
            get_daily_close(symbol)
            if use_close
            else get_current_price(symbol)
        )

        symbol_display = md_escape(
            display_ticker(symbol)
        )

        if price is None:

            lines.append(
                f"*{symbol_display}*"
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

        diff = calc_order_diff_pct(
            order.preco_ordem,
            price,
        )

        lines.append(
            f"*{symbol_display}*"
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
            f"*{md_escape(format_pct(diff))}*"
        )

        lines.append("")

    return "\n".join(lines).rstrip()


def build_order_alert_message(
    order: OrderConfig,
    symbol: str,
    price: float,
    preco_ordem: float,
) -> str:

    symbol_display = md_escape(
        display_ticker(symbol)
    )

    return (
        f"*🎯 Ordem atingida*\n\n"

        f"*{symbol_display}* atingiu o preço "
        f"da sua ordem de compra\\.\n"

        f"Preço atual "
        f"*{md_escape(format_money(price))}* "

        f"≤ sua ordem "
        f"*{md_escape(format_money(preco_ordem))}*\\.\n\n"

        f"_Verifique na corretora se a ordem "
        f"foi executada\\._"
    )


def build_alert_message(
    asset: AssetConfig,
    symbol: str,
    price: float,
    preco_teto: float,
) -> str:

    label = md_escape(
        asset.label
    )

    symbol_escaped = md_escape(
        symbol
    )

    return (
        f"*🚨 Alerta de oportunidade*\n\n"

        f"*📉* *{label}* "
        f"\\(`{symbol_escaped}`\\)\n"

        f"Preço atual "
        f"*{md_escape(format_money(price))}* "

        f"≤ preço teto "
        f"*{md_escape(format_money(preco_teto))}*\\.\n\n"

        f"_Considere sua própria análise "
        f"antes de operar\\._"
    )


# ===========================================================================
# CONTROLE DE ALERTAS
# ===========================================================================


_alert_episode_keys: set[str] = set()

_order_alert_episode_keys: set[str] = set()

_eod_summary_sent_date: str | None = None


def _alert_episode_key(
    symbol: str,
    preco_teto: float,
) -> str:

    return (
        f"{symbol}|{preco_teto}"
    )


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

    if not is_trading_window(cfg):

        log.info(
            "Fora da janela de pregão configurada; "
            "pulando verificação agendada."
        )

        return

    for asset in assets:

        try:

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

            if price is None:

                log.warning(
                    "Não foi possível obter preço "
                    "para %s (resolvido: %s)",
                    asset.ticker,
                    symbol,
                )

                continue

            teto = asset.preco_teto

            key = _alert_episode_key(
                symbol,
                teto,
            )

            if price <= teto:

                if key in _alert_episode_keys:
                    continue

                message = build_alert_message(
                    asset,
                    symbol,
                    price,
                    teto,
                )

                try:

                    bot.send_message(
                        chat_id,
                        message,
                        parse_mode="MarkdownV2",
                    )

                    _alert_episode_keys.add(key)

                    log.info(
                        "🚨 Alerta enviado: %s @ %s",
                        symbol,
                        price,
                    )

                except Exception as e:

                    log.exception(
                        "Falha ao enviar alerta Telegram: %s",
                        e,
                    )

            else:

                _alert_episode_keys.discard(
                    key
                )

        except Exception as e:

            log.exception(
                "Erro ao processar ativo %s: %s",
                asset.ticker,
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

    if not orders:
        return

    if not is_trading_window(cfg):

        log.info(
            "Fora da janela de pregão; "
            "pulando verificação de ordens."
        )

        return

    for order in orders:

        try:

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
                    "Não foi possível obter preço "
                    "para ordem %s (resolvido: %s)",
                    order.ticker,
                    symbol,
                )

                continue

            limite = order.preco_ordem

            key = _order_alert_episode_key(
                symbol,
                limite,
            )

            if price <= limite:

                if key in _order_alert_episode_keys:
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
                        "🎯 Alerta de ordem enviado: %s @ %s",
                        symbol,
                        price,
                    )

                except Exception as e:

                    log.exception(
                        "Falha ao enviar alerta de ordem: %s",
                        e,
                    )

            else:

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

    global _eod_summary_sent_date

    if not orders:
        return

    if not is_weekday(cfg):
        return

    timezone = get_schedule_tz(cfg)

    today = datetime.now(
        timezone
    ).strftime(
        "%Y-%m-%d"
    )

    if _eod_summary_sent_date == today:
        return

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


# ===========================================================================
# HEARTBEAT
# ===========================================================================


def heartbeat():

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
    # Configuração
    # -----------------------------------------------------------------------

    cfg = load_config()

    token = get_bot_token(cfg)

    assets = parse_assets(cfg)

    orders = load_ordens()

    # -----------------------------------------------------------------------
    # Chat ID
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
            "ou telegram.chat_id em config.json "
            "(ID do chat para enviar alertas agendados)."
        )

    # -----------------------------------------------------------------------
    # Telegram
    # -----------------------------------------------------------------------

    bot = telebot.TeleBot(
        token,
        parse_mode=None,
    )

    # -----------------------------------------------------------------------
    # Intervalo
    # -----------------------------------------------------------------------

    schedule_config = (
        cfg.get("schedule")
        or {}
    )

    interval = int(
        schedule_config.get(
            "interval_minutes",
            60,
        )
    )

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

    # Agenda monitoramento
    schedule.every(
        interval
    ).minutes.do(
        job
    )

    # -----------------------------------------------------------------------
    # Resumo diário
    # -----------------------------------------------------------------------

    eod_time = (
        schedule_config.get(
            "eod_summary_time"
        )
        or "18:00"
    )

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

                "*Comandos disponíveis:*\n"

                "• /status — preços atuais de todos os ativos\n"
                "• /ordens — comparação com suas ordens na corretora\n"
                "• /help — esta mensagem\n\n"

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
                "Erro ao formatar ajuda. Tente /status.",
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
                        "Nenhuma ordem em ordens.json. "
                        "Edite o arquivo e reinicie o bot."
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
                        "Não foi possível montar a comparação agora. "
                        "Tente de novo em instantes."
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
                        "Não foi possível montar o status agora. "
                        "Tente de novo em instantes."
                    ),
                )

            except Exception:
                pass

    # -----------------------------------------------------------------------
    # SCHEDULER
    # -----------------------------------------------------------------------

    def scheduler_loop():

        while True:

            try:

                schedule.run_pending()

            except Exception as e:

                log.exception(
                    "Erro no scheduler: %s",
                    e,
                )

            time.sleep(15)

    threading.Thread(
        target=scheduler_loop,
        daemon=True,
        name="scheduler",
    ).start()

    # -----------------------------------------------------------------------
    # PRIMEIRA VERIFICAÇÃO
    # -----------------------------------------------------------------------

    threading.Thread(
        target=lambda: (
            time.sleep(5),
            job(),
        ),
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
        "Resumo EOD: %s",
        interval,
        chat_id,
        len(assets),
        len(orders),
        eod_time,
    )

    # -----------------------------------------------------------------------
    # TELEGRAM POLLING
    # -----------------------------------------------------------------------

    try:

        log.info(
            "📡 Iniciando Telegram polling..."
        )

        bot.infinity_polling(
            skip_pending=True,
            restart_on_change=False,
        )

    except Exception as e:

        log.exception(
            "❌ Polling encerrado com erro: %s",
            e,
        )

        raise


# ===========================================================================
# START
# ===========================================================================


if __name__ == "__main__":
    main()
