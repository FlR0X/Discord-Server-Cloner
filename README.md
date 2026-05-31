# Discord-Server-Cloner
Selfhosted server backup & restore. Saves roles, emojis, channels, permissions, messages, icon, and more.

## Requirements
- Python 3.8+
- Discord bot with **all intents** enabled and **Administrator** permission

## Setup
```bash
pip install -r requirements.txt
export DISCORD_TOKEN="your-bot-token"
python backup.py
```

## Configuration (in the .py file itself)
SLOT_COUNT = 3 – number of backup slots (edit in script)

MAX_WORKERS = 5 – concurrent save/restore tasks

HTTP Type Proxys support: add proxys.txt (one proxy per line, format http://user:pass@host:port or http://host:port)

## Commands
**/save <slot> [options]**
Save server to a slot (1–3). Options (all default True):

include_roles

include_emojis

include_channels

include_messages

include_icon

include_server_settings

**/load <slot> [options]**
Deletes all current roles/emojis/channels then restores from slot. Same options.

**/slots**
Shows saved backups (server name, ID, timestamp, size) as an embed.

## Notes
Slots are global; backups can be loaded onto any server the bot manages.

Roles above the bot, emoji limits (boost level), and rate limits may cause partial failures.

Messages are restored as embeds (max 10 per post). Large histories may be slow.

Test on a secondary server first.
