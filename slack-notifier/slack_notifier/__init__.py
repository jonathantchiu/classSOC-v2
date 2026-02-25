"""Standalone Slack Bot client for posting messages to channels or DMs."""

import logging
import os
from typing import Optional

import requests

__version__ = "0.1.0"

logger = logging.getLogger(__name__)

# Env var names for from_env()
ENV_BOT_TOKEN = "SLACK_BOT_TOKEN"
ENV_USER_ID = "SLACK_USER_ID"
ENV_CHANNEL = "SLACK_CHANNEL"


class SlackBotClient:
    """
    Post messages to Slack via Bot API (channel or DM).

    Usage:
        # Post to a channel and ping a user
        client = SlackBotClient(
            bot_token="xoxb-...",
            dm_user_id="U0xxxxx",  # for ping
            channel="#general",
        )
        client.post("Hello world!")

        # Post to DM
        client = SlackBotClient(
            bot_token="xoxb-...",
            dm_user_id="U0xxxxx",
        )
        client.post("Direct message")
    """

    def __init__(
        self,
        bot_token: str,
        dm_user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ):
        """
        Args:
            bot_token: Slack Bot User OAuth Token (xoxb-...)
            dm_user_id: Slack User ID (U0xxxxx). Required for DM mode; used for ping in channel mode.
            channel: Channel name (e.g. #general). If set, posts to channel; else posts to DM.
        """
        self.bot_token = bot_token
        self.dm_user_id = dm_user_id
        self.channel = channel
        self._dm_channel_id: Optional[str] = None

    @classmethod
    def from_env(
        cls,
        bot_token: Optional[str] = None,
        dm_user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> "SlackBotClient":
        """
        Create client from environment variables.

        Reads SLACK_BOT_TOKEN, SLACK_USER_ID, SLACK_CHANNEL (optional).
        Pass explicit args to override env vars.
        """
        return cls(
            bot_token=bot_token or os.environ[ENV_BOT_TOKEN],
            dm_user_id=dm_user_id or os.environ.get(ENV_USER_ID),
            channel=channel or os.environ.get(ENV_CHANNEL),
        )

    def post(
        self,
        text: str,
        ping_user_id: Optional[str] = None,
    ) -> bool:
        """
        Send a message to Slack.

        Args:
            text: Message text (supports Slack markdown)
            ping_user_id: If set, prepends <@user_id> to ping the user. Defaults to dm_user_id when channel is set.

        Returns:
            True if sent successfully, False otherwise.
        """
        ping = ping_user_id or (self.dm_user_id if self.channel else None)
        if ping:
            text = f"<@{ping}> " + text

        try:
            channel_id = self._get_channel()
            if not channel_id:
                return False

            resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {self.bot_token}",
                    "Content-Type": "application/json",
                },
                json={"channel": channel_id, "text": text},
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error("Slack chat.postMessage failed: %s", data.get("error", "unknown"))
                return False
            return True
        except Exception as e:
            logger.error("Slack post failed: %s", e)
            return False

    def _get_channel(self) -> Optional[str]:
        """Return channel ID or name for posting."""
        if self.channel:
            return self.channel
        return self._get_dm_channel()

    def _get_dm_channel(self) -> Optional[str]:
        """Open or return cached DM channel ID."""
        if not self.dm_user_id:
            logger.error("dm_user_id required for DM mode")
            return None

        if self._dm_channel_id:
            return self._dm_channel_id

        try:
            resp = requests.post(
                "https://slack.com/api/conversations.open",
                headers={
                    "Authorization": f"Bearer {self.bot_token}",
                    "Content-Type": "application/json",
                },
                json={"users": [self.dm_user_id]},
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error("Slack conversations.open failed: %s", data.get("error", "unknown"))
                return None

            self._dm_channel_id = data["channel"]["id"]
            return self._dm_channel_id
        except Exception as e:
            logger.error("Slack conversations.open failed: %s", e)
            return None
