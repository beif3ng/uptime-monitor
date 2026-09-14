# AI assistance: parts of this file were written with Claude (Anthropic).
# Architecture, review and debugging are my own. See README.md, "AI use".

"""Telegram alerts.

One function, one transport. A failed alert is logged and swallowed: the
monitor's own state is already written by the time this is called, and losing
a message must not stop the checker from doing its job.
"""

import logging

import httpx

import config

log = logging.getLogger("notifier")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


async def send(text: str) -> None:
    """Post one message to the configured chat.

    Without a token or chat id the message only reaches the log. That is the
    deliberate behaviour for running the project without a bot: everything
    else works, and the alert is still visible to whoever is watching.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.info("Telegram not configured, alert not sent: %s", text)
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                API_URL.format(token=config.TELEGRAM_BOT_TOKEN),
                json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            )
        if response.status_code != 200:
            # Telegram explains a rejected message in the body, not the status.
            log.error("Telegram refused the alert: %s", response.text)
    except httpx.HTTPError as exc:
        log.error("Telegram unreachable: %s", exc)
