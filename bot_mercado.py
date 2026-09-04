"""
Bot Telegram: alertas quando preço de mercado <= preço teto (oportunidade).
Usa yfinance, pyTelegramBotAPI e schedule.
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

# ---------------------------------------------------------------------------
# Configuração (edite config.json — não hardcode ativos no fluxo principal)
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
ORDENS_PATH = Path(__file__).resolve().parent / "ordens.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"Arquivo de configuração não encontrado: {CONFIG_PATH}"
        )
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_bot_token(cfg: dict) -> str:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if token:
        return token
    token = (cfg.get("telegram") or {}).get("bot_token") or ""
    token = str(token).strip()
    if not token:
        raise ValueError(
            "Token do bot ausente. Defina TELEGRAM_BOT_TOKEN ou telegram.bot_token em config.json"
        )
    return token


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


def display_ticker(ticker: str) -> str:
    """Exibe CMIG4 em vez de CMIG4.SA (como na corretora)."""
    base = ticker.strip().upper()
    if base.endswith(".SA"):
        return base[:-3]
    return base


def load_ordens() -> list[OrderConfig]:
    if not ORDENS_PATH.is_file():
        log.info("Arquivo ordens.json não encontrado; monitoramento de ordens desativado.")
        return []
    with open(ORDENS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("ordens") or []
    out: list[OrderConfig] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        t = str(item.get("ticker", "")).strip()
        if not t:
            continue
        try:
            po = float(item.get("preco_ordem"))
        except (TypeError, ValueError):
            log.warning("Ignorando ordem sem preco_ordem válido: %s", item)
            continue
        nome = item.get("nome_amigavel")
        nome = str(nome).strip() if nome else None
        out.append(OrderConfig(ticker=t, preco_ordem=po, nome_amigavel=nome))
    return out


def parse_assets(cfg: dict) -> list[AssetConfig]:
    raw = cfg.get("assets") or []
    out: list[AssetConfig] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        t = str(item.get("ticker", "")).strip()
        if not t:
            continue
        try:
            pt = float(item.get("preco_teto"))
        except (TypeError, ValueError):
            log.warning("Ignorando ativo sem preco_teto válido: %s", item)
            continue
        nome = item.get("nome_amigavel")
        nome = str(nome).strip() if nome else None
        out.append(AssetConfig(ticker=t, preco_teto=pt, nome_amigavel=nome))
    return out


# ---------------------------------------------------------------------------
# Yahoo Finance: preço e resolução .SA
# ---------------------------------------------------------------------------


def _price_from_ticker(ticker: yf.Ticker) -> float | None:
    """Obtém último preço conhecido; retorna None se indisponível."""
    try:
        fi = ticker.fast_info
        if fi is not None:
            for key in ("last_price", "regular_market_price", "previous_close"):
                v = getattr(fi, key, None) if not isinstance(fi, dict) else fi.get(key)
                if v is not None and float(v) > 0:
                    return float(v)
    except Exception as e:
        log.debug("fast_info falhou para %s: %s", ticker.ticker, e)

    try:
        hist = ticker.history(period="5d", interval="1d")
        if hist is not None and not hist.empty and "Close" in hist.columns:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.debug("history falhou para %s: %s", ticker.ticker, e)
    return None


def resolve_yahoo_symbol(user_ticker: str) -> tuple[str, float | None]:
    """
    Valida no Yahoo Finance. Se o símbolo puro falhar, tenta com sufixo .SA.
    Retorna (símbolo_yahoo_usado, preço ou None).
    """
    base = user_ticker.strip().upper()
    if not base:
        return base, None

    candidates = [base]
    if not base.endswith(".SA"):
        candidates.append(f"{base}.SA")

    last_symbol = candidates[-1]
    for sym in candidates:
        last_symbol = sym
        try:
            t = yf.Ticker(sym)
            price = _price_from_ticker(t)
            if price is not None:
                return sym, price
        except Exception as e:
            log.debug("Erro ao consultar %s: %s", sym, e)
            continue
    return last_symbol, None


def get_current_price(symbol: str) -> float | None:
    try:
        t = yf.Ticker(symbol)
        return _price_from_ticker(t)
    except Exception as e:
        log.warning("Falha ao obter preço de %s: %s", symbol, e)
        return None


def get_daily_close(symbol: str) -> float | None:
    """Fechamento do último pregão disponível no Yahoo Finance."""
    try:
        hist = yf.Ticker(symbol).history(period="5d", interval="1d")
        if hist is not None and not hist.empty and "Close" in hist.columns:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.debug("Fechamento diário indisponível para %s: %s", symbol, e)
    return None


def calc_order_diff_pct(preco_ordem: float, preco_referencia: float) -> float:
    """Diferença % entre ordem e preço de referência: (ordem/ref - 1) * 100."""
    if preco_referencia <= 0:
        return 0.0
    return (preco_ordem / preco_referencia - 1.0) * 100.0


def format_pct(value: float) -> str:
    s = f"{value:+.1f}".replace(".", ",")
    if value >= 0 and not s.startswith("+"):
        s = "+" + s
    return s + "%"


# ---------------------------------------------------------------------------
# Telegram (Markdown legado) + emojis
# ---------------------------------------------------------------------------

# Caracteres que exigem escape em MarkdownV2 (Telegram)
_MD_V2_SPECIAL = frozenset(r"_*[]()~`>#+-=|{}.!")


def md_escape(text: str) -> str:
    return "".join(("\\" + c if c in _MD_V2_SPECIAL else c) for c in str(text))


def format_money(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def is_trading_window(cfg: dict) -> bool:
    sch = (cfg.get("schedule") or {}).get("trading_hours") or {}
    if not sch.get("enabled", True):
        return True

    tz_name = sch.get("timezone") or "America/Sao_Paulo"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Sao_Paulo")

    now = datetime.now(tz)
    if sch.get("weekdays_only", True) and now.weekday() >= 5:
        return False

    start_s = sch.get("start") or "10:00"
    end_s = sch.get("end") or "17:55"
    sh, sm = map(int, start_s.split(":")[:2])
    eh, em = map(int, end_s.split(":")[:2])
    minutes_now = now.hour * 60 + now.minute
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    return start_m <= minutes_now <= end_m


def build_status_message(cfg: dict, assets: list[AssetConfig]) -> str:
    lines = [
        "*📊 Status dos ativos*",
        "",
        f"_Atualizado: {md_escape(datetime.now().strftime('%d/%m/%Y %H:%M'))}_",
        "",
    ]
    for a in assets:
        sym, _ = resolve_yahoo_symbol(a.ticker)
        price = get_current_price(sym)
        label = md_escape(a.label)
        sym_e = md_escape(sym)

        if price is None:
            lines.append(f"*{label}* \\(`{sym_e}`\\)")
            lines.append("⚠️ Preço indisponível no momento\\.")
            lines.append("")
            continue

        teto = a.preco_teto
        if price > teto:
            emoji = "📈"
            situ = "Acima do preço teto"
        elif price == teto:
            emoji = "📉"
            situ = "Igual ao preço teto (oportunidade)"
        else:
            emoji = "📉"
            situ = "Abaixo do preço teto (oportunidade)"

        lines.append(f"{emoji} *{label}* \\(`{sym_e}`\\)")
        lines.append(f"• Atual: *{md_escape(format_money(price))}*")
        lines.append(f"• Teto: *{md_escape(format_money(teto))}*")
        lines.append(f"• _{md_escape(situ)}_")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_ordens_comparison_message(
    orders: list[OrderConfig], *, use_close: bool = False
) -> str:
    price_label = "Fechamento" if use_close else "Preço atual"
    title = (
        "*📊 Resumo do dia — comparação com nossas ordens*"
        if use_close
        else "*📊 Comparação com nossas ordens*"
    )
    lines = [
        title,
        "",
        f"_{md_escape(datetime.now().strftime('%d/%m/%Y %H:%M'))}_",
        "",
    ]
    for order in orders:
        sym, _ = resolve_yahoo_symbol(order.ticker)
        price = get_daily_close(sym) if use_close else get_current_price(sym)
        sym_display = md_escape(display_ticker(sym))

        if price is None:
            lines.append(f"*{sym_display}*")
            lines.append(f"• {md_escape(price_label)}: indisponível")
            lines.append(f"• Nossa ordem: *{md_escape(format_money(order.preco_ordem))}*")
            lines.append("")
            continue

        diff = calc_order_diff_pct(order.preco_ordem, price)
        lines.append(f"*{sym_display}*")
        lines.append(f"• {md_escape(price_label)}: *{md_escape(format_money(price))}*")
        lines.append(f"• Nossa ordem: *{md_escape(format_money(order.preco_ordem))}*")
        lines.append(f"• Diferença: *{md_escape(format_pct(diff))}*")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_order_alert_message(
    order: OrderConfig, symbol: str, price: float, preco_ordem: float
) -> str:
    sym_display = md_escape(display_ticker(symbol))
    return (
        f"*🎯 Ordem atingida*\n\n"
        f"*{sym_display}* atingiu o preço da sua ordem de compra\\.\n"
        f"Preço atual *{md_escape(format_money(price))}* "
        f"≤ sua ordem *{md_escape(format_money(preco_ordem))}*\\.\n\n"
        f"_Verifique na corretora se a ordem foi executada\\._"
    )


def build_alert_message(
    asset: AssetConfig, symbol: str, price: float, preco_teto: float
) -> str:
    label = md_escape(asset.label)
    sym_e = md_escape(symbol)
    return (
        f"*🚨 Alerta de oportunidade*\n\n"
        f"*📉* *{label}* \\(`{sym_e}`\\)\n"
        f"Preço atual *{md_escape(format_money(price))}* "
        f"≤ preço teto *{md_escape(format_money(preco_teto))}*\\.\n\n"
        f"_Considere sua própria análise antes de operar\\._"
    )


# ---------------------------------------------------------------------------
# Núcleo de monitoramento
# ---------------------------------------------------------------------------

# Evita reenviar o mesmo alerta enquanto o preço permanecer ≤ teto;
# zera quando o preço voltar acima do teto.
_alert_episode_keys: set[str] = set()
_order_alert_episode_keys: set[str] = set()
_eod_summary_sent_date: str | None = None


def _alert_episode_key(symbol: str, preco_teto: float) -> str:
    return f"{symbol}|{preco_teto}"


def _order_alert_episode_key(symbol: str, preco_ordem: float) -> str:
    return f"ordem|{symbol}|{preco_ordem}"


def get_schedule_tz(cfg: dict) -> ZoneInfo:
    sch = (cfg.get("schedule") or {}).get("trading_hours") or {}
    tz_name = sch.get("timezone") or "America/Sao_Paulo"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("America/Sao_Paulo")


def is_weekday(cfg: dict) -> bool:
    sch = (cfg.get("schedule") or {}).get("trading_hours") or {}
    if not sch.get("weekdays_only", True):
        return True
    return datetime.now(get_schedule_tz(cfg)).weekday() < 5


def run_price_check(bot: telebot.TeleBot, chat_id: str, cfg: dict, assets: list[AssetConfig]) -> None:
    if not is_trading_window(cfg):
        log.info("Fora da janela de pregão configurada; pulando verificação agendada.")
        return

    for asset in assets:
        try:
            sym, price_try = resolve_yahoo_symbol(asset.ticker)
            price = price_try if price_try is not None else get_current_price(sym)

            if price is None:
                log.warning(
                    "Não foi possível obter preço para %s (resolvido: %s)",
                    asset.ticker,
                    sym,
                )
                continue

            teto = asset.preco_teto
            key = _alert_episode_key(sym, teto)
            if price <= teto:
                if key in _alert_episode_keys:
                    continue
                msg = build_alert_message(asset, sym, price, teto)
                try:
                    bot.send_message(chat_id, msg, parse_mode="MarkdownV2")
                    _alert_episode_keys.add(key)
                    log.info("Alerta enviado: %s @ %s", sym, price)
                except Exception as e:
                    log.exception("Falha ao enviar alerta Telegram: %s", e)
            else:
                _alert_episode_keys.discard(key)

        except Exception as e:
            log.exception("Erro ao processar ativo %s: %s", asset.ticker, e)


def run_order_check(
    bot: telebot.TeleBot, chat_id: str, cfg: dict, orders: list[OrderConfig]
) -> None:
    if not orders:
        return
    if not is_trading_window(cfg):
        log.info("Fora da janela de pregão; pulando verificação de ordens.")
        return

    for order in orders:
        try:
            sym, price_try = resolve_yahoo_symbol(order.ticker)
            price = price_try if price_try is not None else get_current_price(sym)

            if price is None:
                log.warning(
                    "Não foi possível obter preço para ordem %s (resolvido: %s)",
                    order.ticker,
                    sym,
                )
                continue

            limite = order.preco_ordem
            key = _order_alert_episode_key(sym, limite)
            if price <= limite:
                if key in _order_alert_episode_keys:
                    continue
                msg = build_order_alert_message(order, sym, price, limite)
                try:
                    bot.send_message(chat_id, msg, parse_mode="MarkdownV2")
                    _order_alert_episode_keys.add(key)
                    log.info("Alerta de ordem enviado: %s @ %s", sym, price)
                except Exception as e:
                    log.exception("Falha ao enviar alerta de ordem: %s", e)
            else:
                _order_alert_episode_keys.discard(key)

        except Exception as e:
            log.exception("Erro ao processar ordem %s: %s", order.ticker, e)


def run_eod_summary(
    bot: telebot.TeleBot, chat_id: str, cfg: dict, orders: list[OrderConfig]
) -> None:
    global _eod_summary_sent_date
    if not orders:
        return
    if not is_weekday(cfg):
        return

    tz = get_schedule_tz(cfg)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    if _eod_summary_sent_date == today:
        return

    msg = build_ordens_comparison_message(orders, use_close=True)
    try:
        bot.send_message(chat_id, msg, parse_mode="MarkdownV2")
        _eod_summary_sent_date = today
        log.info("Resumo diário de ordens enviado (%s)", today)
    except Exception as e:
        log.exception("Falha ao enviar resumo diário de ordens: %s", e)


def main() -> None:
    cfg = load_config()
    token = get_bot_token(cfg)
    assets = parse_assets(cfg)
    orders = load_ordens()

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        chat_id = (cfg.get("telegram") or {}).get("chat_id") or ""
        chat_id = str(chat_id).strip()
    if not chat_id:
        raise ValueError(
            "Defina TELEGRAM_CHAT_ID no ambiente ou telegram.chat_id em config.json "
            "(ID do chat para enviar alertas agendados)."
        )

    bot = telebot.TeleBot(token, parse_mode=None)

    sch_cfg = cfg.get("schedule") or {}
    interval = int(sch_cfg.get("interval_minutes", 60))

    def job():
        try:
            run_price_check(bot, chat_id, cfg, assets)
            run_order_check(bot, chat_id, cfg, orders)
        except Exception as e:
            log.exception("Erro no job agendado: %s", e)

    schedule.every(interval).minutes.do(job)

    eod_time = (cfg.get("schedule") or {}).get("eod_summary_time") or "18:00"
    schedule.every().day.at(eod_time).do(
        lambda: run_eod_summary(bot, chat_id, cfg, orders)
    )

    @bot.message_handler(commands=["start", "help"])
    def send_welcome(message):
        try:
            text = (
                "*Monitor de preços*\n\n"
                "Comandos:\n"
                "• /status — preços atuais de todos os ativos\n"
                "• /ordens — comparação com suas ordens na corretora\n"
                "• /help — esta mensagem\n\n"
                "_Fonte: Yahoo Finance \\(pode haver atraso conforme o ativo\\)\\._"
            )
            bot.reply_to(message, text, parse_mode="MarkdownV2")
        except Exception as e:
            log.exception("Erro em /help: %s", e)
            bot.reply_to(message, "Erro ao formatar ajuda. Tente /status.")

    @bot.message_handler(commands=["ordens"])
    def cmd_ordens(message):
        try:
            current_orders = load_ordens()
            if not current_orders:
                bot.reply_to(
                    message,
                    "Nenhuma ordem em ordens.json. Edite o arquivo e reinicie o bot.",
                )
                return
            msg = build_ordens_comparison_message(current_orders, use_close=False)
            bot.reply_to(message, msg, parse_mode="MarkdownV2")
        except Exception as e:
            log.exception("Erro em /ordens: %s", e)
            try:
                bot.reply_to(
                    message,
                    "Não foi possível montar a comparação agora. Tente de novo em instantes.",
                )
            except Exception:
                pass

    @bot.message_handler(commands=["status"])
    def cmd_status(message):
        try:
            msg = build_status_message(cfg, assets)
            bot.reply_to(message, msg, parse_mode="MarkdownV2")
        except Exception as e:
            log.exception("Erro em /status: %s", e)
            try:
                bot.reply_to(
                    message,
                    "Não foi possível montar o status agora. Tente de novo em instantes.",
                )
            except Exception:
                pass

    def scheduler_loop():
        while True:
            try:
                schedule.run_pending()
            except Exception as e:
                log.exception("Erro no scheduler: %s", e)
            time.sleep(15)

    threading.Thread(target=scheduler_loop, daemon=True).start()

    # Primeira verificação após subir (não bloqueia o polling)
    threading.Thread(target=lambda: (time.sleep(5), job()), daemon=True).start()

    log.info(
        "Bot iniciado. Intervalo: %s min. Chat alertas: %s. Ordens: %s. Resumo EOD: %s",
        interval,
        chat_id,
        len(orders),
        eod_time,
    )
    try:
        bot.infinity_polling(skip_pending=True, restart_on_change=False)
    except Exception as e:
        log.exception("Polling encerrado com erro: %s", e)
        raise


if __name__ == "__main__":
    main()
