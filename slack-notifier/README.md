# slack-notifier

A minimal Slack Bot client for posting messages to channels or DMs. No project-specific dependencies.

## Install

From the package directory:

```bash
pip install -e .
```

Or copy the `slack_notifier` folder into your project.

## Setup

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps)
2. Add Bot Token Scopes: `chat:write`, `im:write`
3. Install the app to your workspace
4. Get your Bot Token from **OAuth & Permissions**
5. Get your User ID: Slack profile → More → Copy member ID (e.g. `U0AHDLC6D9P`)

## Usage

### Post to a channel (with ping)

```python
from slack_notifier import SlackBotClient

client = SlackBotClient(
    bot_token="xoxb-your-token",
    dm_user_id="U0xxxxx",  # user to ping
    channel="#general",
)
client.post("*Status update*\nEverything is running.")
```

### Post to DM

```python
client = SlackBotClient(
    bot_token="xoxb-your-token",
    dm_user_id="U0xxxxx",
)
client.post("Direct message to you.")
```

### Optional ping override

```python
# Ping a different user than dm_user_id
client.post("Message", ping_user_id="U0OTHER")
```

## Environment variables

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_USER_ID=U0xxxxx
SLACK_CHANNEL=#general  # optional, omit for DM
```

```python
import os
from slack_notifier import SlackBotClient

client = SlackBotClient(
    bot_token=os.environ["SLACK_BOT_TOKEN"],
    dm_user_id=os.environ["SLACK_USER_ID"],
    channel=os.environ.get("SLACK_CHANNEL"),
)
client.post("Hello from my script!")
```
