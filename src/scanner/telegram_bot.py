"""Telegram bot for whale alerts with copy trading."""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config.settings import Settings
from src.scanner.models import WhaleAlert

logger = logging.getLogger(__name__)


class TelegramAlertBot:
    """Telegram bot for sending whale alerts and handling copy trades."""

    def __init__(self, settings: Settings):
        """Initialize Telegram bot.

        Args:
            settings: Application settings with Telegram token and chat ID
        """
        self.settings = settings
        self.token = getattr(settings, "telegram_bot_token", "")
        self.chat_id = getattr(settings, "telegram_chat_id", "")
        self.max_copy_size = getattr(settings, "whale_copy_max_usd", 10.0)

        self.app: Optional[Application] = None
        self._pending_alerts: Dict[str, WhaleAlert] = {}
        self._copy_callback = None  # Callback function for copy trades

        if not self.token:
            logger.warning("Telegram bot token not configured")

    async def initialize(self) -> bool:
        """Initialize the Telegram bot application."""
        if not self.token:
            return False

        try:
            self.app = Application.builder().token(self.token).build()

            # Add handlers
            self.app.add_handler(CommandHandler("start", self._handle_start))
            self.app.add_handler(CommandHandler("status", self._handle_status))
            self.app.add_handler(CallbackQueryHandler(self._handle_callback))

            await self.app.initialize()
            logger.info("Telegram bot initialized")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Telegram bot: {e}")
            return False

    def set_copy_callback(self, callback) -> None:
        """Set the callback function for executing copy trades.

        Args:
            callback: Async function that takes (alert, size_usd) and returns success bool
        """
        self._copy_callback = callback

    async def send_alert(self, alert: WhaleAlert) -> bool:
        """Send a whale alert to Telegram with copy buttons.

        Args:
            alert: The whale alert to send

        Returns:
            True if sent successfully
        """
        if not self.app or not self.chat_id:
            logger.warning("Telegram not configured, skipping alert")
            return False

        # Store alert for callback reference
        alert_id = alert.id
        self._pending_alerts[alert_id] = alert

        # Format message
        message = self._format_alert_message(alert)

        # Create inline keyboard with Copy/Skip buttons
        keyboard = [
            [
                InlineKeyboardButton(
                    f"✅ Copy ${self.max_copy_size:.0f}",
                    callback_data=f"copy:{alert_id}",
                ),
                InlineKeyboardButton("❌ Skip", callback_data=f"skip:{alert_id}"),
            ],
            [
                InlineKeyboardButton(
                    "🔗 View Market",
                    url=f"https://polymarket.com/event/{alert.trade.market_slug}",
                ),
                InlineKeyboardButton(
                    "👤 View Wallet",
                    url=f"https://polymarket.com/@{alert.trade.wallet_address}",
                ),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            logger.info(f"Sent Telegram alert for {alert.trade.wallet_address[:10]}...")
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")
            return False

    def _format_alert_message(self, alert: WhaleAlert) -> str:
        """Format alert for Telegram message."""
        types_str = " + ".join([t.value.replace("_", " ").title() for t in alert.alert_types])
        wallet_short = f"{alert.trade.wallet_address[:6]}...{alert.trade.wallet_address[-4:]}"

        # Confidence emoji
        if alert.confidence >= 0.8:
            conf_emoji = "🔥🔥🔥"
        elif alert.confidence >= 0.6:
            conf_emoji = "🔥🔥"
        else:
            conf_emoji = "🔥"

        message = f"""
{conf_emoji} <b>WHALE ALERT</b> {conf_emoji}

<b>Wallet:</b> <code>{wallet_short}</code>
<b>Type:</b> {types_str}
<b>Confidence:</b> {alert.confidence:.0%}

<b>Market:</b> {alert.trade.market_question[:60]}{'...' if len(alert.trade.market_question) > 60 else ''}

<b>Action:</b> {alert.trade.side} {alert.trade.outcome} @ ${alert.trade.price:.2f}
<b>Size:</b> <b>${alert.trade.value_usd:,.0f}</b>

📊 <b>Wallet Stats:</b>
• Age: {alert.wallet.wallet_age_days} days
• Prior trades: {alert.wallet.total_trades}
• Total volume: ${alert.wallet.total_volume_usd:,.0f}

⏰ {datetime.now().strftime('%H:%M:%S')}
"""
        return message.strip()

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        await update.message.reply_text(
            "🐋 <b>Whale Scanner Bot</b>\n\n"
            "I'll send you alerts when whales make big moves on Polymarket.\n\n"
            "Commands:\n"
            "/status - Check bot status\n\n"
            f"Your Chat ID: <code>{update.effective_chat.id}</code>",
            parse_mode="HTML",
        )

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command."""
        pending_count = len(self._pending_alerts)
        await update.message.reply_text(
            f"🐋 <b>Whale Scanner Status</b>\n\n"
            f"• Pending alerts: {pending_count}\n"
            f"• Max copy size: ${self.max_copy_size:.0f}\n"
            f"• Bot active: ✅",
            parse_mode="HTML",
        )

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle button callbacks (Copy/Skip)."""
        query = update.callback_query
        await query.answer()

        data = query.data
        action, alert_id = data.split(":", 1)

        alert = self._pending_alerts.get(alert_id)
        if not alert:
            await query.edit_message_text(
                text=query.message.text + "\n\n⚠️ Alert expired",
                parse_mode="HTML",
            )
            return

        if action == "copy":
            # Execute copy trade
            if self._copy_callback:
                try:
                    success = await self._copy_callback(alert, self.max_copy_size)
                    if success:
                        result_text = f"\n\n✅ <b>COPIED!</b> ${self.max_copy_size:.0f} on {alert.trade.outcome}"
                    else:
                        result_text = "\n\n❌ Copy failed - check logs"
                except Exception as e:
                    logger.error(f"Copy trade error: {e}")
                    result_text = f"\n\n❌ Error: {str(e)[:50]}"
            else:
                result_text = "\n\n⚠️ Copy trading not configured"

            await query.edit_message_text(
                text=query.message.text + result_text,
                parse_mode="HTML",
            )

        elif action == "skip":
            await query.edit_message_text(
                text=query.message.text + "\n\n⏭️ Skipped",
                parse_mode="HTML",
            )

        # Remove from pending
        self._pending_alerts.pop(alert_id, None)

    async def start_polling(self) -> None:
        """Start the bot polling for updates."""
        if self.app:
            await self.app.start()
            await self.app.updater.start_polling()

    async def stop(self) -> None:
        """Stop the bot."""
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
