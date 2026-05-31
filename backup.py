import asyncio
import base64
import json
import os
import random
import time
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from colorama import Fore, Style, init as colorama_init
from datetime import datetime

colorama_init(autoreset=True)

TOKEN = os.getenv("DISCORD_TOKEN") or "YOUR_BOT_TOKEN"
SAVE_DIR = "saves"
SLOT_COUNT = 3
MAX_WORKERS = 5
PROXY_COOLDOWN = 60

def log_info(msg):
    print(f"{Fore.GREEN}[+]{Style.RESET_ALL} {msg}")

def log_warning(msg):
    print(f"{Fore.YELLOW}[+]{Style.RESET_ALL} {msg}")

def log_error(msg):
    print(f"{Fore.RED}[+]{Style.RESET_ALL} {msg}")

def log_success(msg):
    print(f"{Fore.MAGENTA}[+]{Style.RESET_ALL} {msg}")

class ProxyPool:
    def __init__(self, proxy_file="proxys.txt", cooldown=PROXY_COOLDOWN):
        self.proxies = []
        self.cooldown_until = {}
        self.current = None
        self.load(proxy_file)
        self.cooldown = cooldown

    def load(self, path):
        if not os.path.exists(path):
            open(path, "a").close()
            return
        with open(path, "r") as f:
            lines = [line.strip() for line in f if line.strip()]
        self.proxies = lines
        if self.proxies:
            log_success(f"loaded {len(self.proxies)} proxies")

    def get_proxy(self):
        now = time.time()
        available = [p for p in self.proxies if self.cooldown_until.get(p, 0) <= now]
        if not available:
            log_warning("no proxies available, waiting for cooldown")
            return None
        return random.choice(available)

    def mark_rate_limited(self, proxy):
        self.cooldown_until[proxy] = time.time() + self.cooldown
        log_warning(f"proxy {proxy} rate limited, cooldown {self.cooldown}s")

    def rotate(self):
        new_proxy = self.get_proxy()
        if new_proxy is None:
            return None
        self.current = new_proxy
        return new_proxy

proxy_pool = ProxyPool()

intents = discord.Intents.all()

class BackupBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http_session = None

    async def setup_hook(self):
        proxy_pool.rotate()
        await self.rebuild_http_session()
        await bot.tree.sync()

    async def rebuild_http_session(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        connector = aiohttp.TCPConnector()
        proxy = proxy_pool.current
        if proxy:
            self.http_session = aiohttp.ClientSession(connector=connector)
            self.http._HTTPClient__session = self.http_session
            log_info(f"using proxy: {proxy}")
        else:
            self.http_session = aiohttp.ClientSession(connector=connector)
            self.http._HTTPClient__session = self.http_session
            log_info("no proxy in use")

    async def handle_rate_limit(self, error=None):
        if proxy_pool.current:
            proxy_pool.mark_rate_limited(proxy_pool.current)
            new_proxy = proxy_pool.rotate()
            if new_proxy:
                await self.rebuild_http_session()
                return True
        return False

    async def close(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

bot = BackupBot(command_prefix="!", intents=intents)

os.makedirs(SAVE_DIR, exist_ok=True)

def get_slot_path(slot):
    return os.path.join(SAVE_DIR, f"slot_{slot}.json")

def save_slot(slot, data):
    os.makedirs(SAVE_DIR, exist_ok=True)
    with open(get_slot_path(slot), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_slot(slot):
    path = get_slot_path(slot)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_slot_metadata(slot):
    data = load_slot(slot)
    if data is None:
        return None
    return {
        "server_name": data.get("server_name"),
        "server_id": data.get("server_id"),
        "backup_timestamp": data.get("backup_timestamp"),
        "size_bytes": data.get("size_bytes")
    }

async def serialize_guild(guild, include_roles=True, include_emojis=True,
                          include_channels=True, include_messages=True,
                          include_icon=True, include_server_settings=True):
    data = {}
    if include_server_settings:
        data["name"] = guild.name
        data["description"] = guild.description
    if include_icon:
        icon_bytes = None
        if guild.icon:
            icon_bytes = await guild.icon.read()
        data["icon_base64"] = base64.b64encode(icon_bytes).decode("utf-8") if icon_bytes else None
    if include_roles:
        roles = []
        for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
            if role.is_default():
                continue
            roles.append({
                "name": role.name,
                "color": role.color.value,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
                "permissions": role.permissions.value,
                "position": role.position,
            })
        data["roles"] = roles
    if include_emojis:
        emojis = []
        for emoji in guild.emojis:
            try:
                emoji_bytes = await emoji.read()
                emojis.append({
                    "name": emoji.name,
                    "animated": emoji.animated,
                    "image_base64": base64.b64encode(emoji_bytes).decode("utf-8")
                })
                log_success(f'saved emoji "{emoji.name}" (id: {emoji.id})')
            except Exception as e:
                log_error(f'failed to save emoji "{emoji.name}": {e}')
        data["emojis"] = emojis
    if include_channels:
        channels = []
        sem = asyncio.Semaphore(MAX_WORKERS)
        async def save_channel(channel):
            overwrites = []
            for target, overwrite in channel.overwrites.items():
                overwrites.append({
                    "id": target.id,
                    "type": "role" if isinstance(target, discord.Role) else "member",
                    "allow": overwrite.pair()[0].value,
                    "deny": overwrite.pair()[1].value,
                })
            channel_data = {
                "id": channel.id,
                "name": channel.name,
                "type": channel.type.value,
                "position": channel.position,
                "category_id": channel.category_id,
                "overwrites": overwrites,
            }
            log_info(f'saving channel #{channel.name} (id: {channel.id})')
            if include_messages and isinstance(channel, discord.TextChannel):
                messages = []
                async for msg in channel.history(limit=None):
                    messages.append(await serialize_message(msg))
                messages.reverse()
                channel_data["messages"] = messages
            return channel_data

        async def save_with_sem(channel):
            async with sem:
                return await save_channel(channel)

        tasks = [save_with_sem(ch) for ch in sorted(guild.channels, key=lambda c: c.position)]
        channel_results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in channel_results:
            if isinstance(result, Exception):
                log_error(f"error saving channel: {result}")
            else:
                channels.append(result)
        data["channels"] = channels
    return data

async def serialize_message(message):
    attachments = []
    for att in message.attachments:
        attachments.append({
            "url": att.url,
            "proxy_url": att.proxy_url,
            "filename": att.filename,
            "content_type": att.content_type,
        })
    embeds = [embed.to_dict() for embed in message.embeds]
    return {
        "author_name": message.author.display_name,
        "author_avatar_url": str(message.author.display_avatar.url),
        "content": message.content,
        "created_at_iso": message.created_at.isoformat(),
        "attachments": attachments,
        "embeds": embeds,
    }

async def restore_guild(guild, data, include_roles=True, include_emojis=True,
                        include_channels=True, include_messages=True,
                        include_icon=True, include_server_settings=True):
    if include_server_settings:
        try:
            await guild.edit(name=data.get("name", guild.name),
                             description=data.get("description"))
            log_success("restored server name and description")
        except Exception as e:
            log_error(f"failed to restore name/description: {e}")
    if include_icon and data.get("icon_base64"):
        icon_bytes = base64.b64decode(data["icon_base64"])
        try:
            await guild.edit(icon=icon_bytes)
            log_success("restored server icon")
        except Exception as e:
            log_error(f"failed to restore icon: {e}")
    if include_roles and "roles" in data:
        for role in guild.roles:
            if role.is_default():
                continue
            try:
                await role.delete()
                log_success(f'deleted role "{role.name}"')
            except Exception as e:
                log_error(f'failed to delete role "{role.name}": {e}')
        created_roles = []
        for role_data in sorted(data["roles"], key=lambda r: r["position"]):
            perms = discord.Permissions(role_data["permissions"])
            colour = discord.Colour(role_data["color"])
            try:
                new_role = await guild.create_role(
                    name=role_data["name"],
                    permissions=perms,
                    colour=colour,
                    hoist=role_data["hoist"],
                    mentionable=role_data["mentionable"]
                )
                created_roles.append((new_role, role_data["position"]))
                log_success(f'restored role "{new_role.name}"')
            except Exception as e:
                log_error(f'failed to restore role "{role_data["name"]}": {e}')
        for role, target_pos in created_roles:
            try:
                await role.edit(position=target_pos)
            except:
                pass
    if include_emojis and "emojis" in data:
        for emoji in guild.emojis:
            try:
                await emoji.delete()
                log_success(f'deleted emoji "{emoji.name}"')
            except Exception as e:
                log_error(f'failed to delete emoji "{emoji.name}": {e}')
        for emoji_data in data["emojis"]:
            try:
                img_bytes = base64.b64decode(emoji_data["image_base64"])
                await guild.create_custom_emoji(name=emoji_data["name"], image=img_bytes)
                log_success(f'restored emoji "{emoji_data["name"]}"')
            except Exception as e:
                log_error(f'failed to restore emoji "{emoji_data["name"]}": {e}')
    if include_channels and "channels" in data:
        for channel in guild.channels:
            try:
                await channel.delete()
                log_success(f"deleted channel #{channel.name}")
            except Exception as e:
                log_error(f"failed to delete channel #{channel.name}: {e}")
        id_map = {}
        categories = [c for c in data["channels"] if c["type"] == 4]
        for cat_data in sorted(categories, key=lambda c: c["position"]):
            try:
                new_cat = await guild.create_category(name=cat_data["name"],
                                                      position=cat_data["position"])
                id_map[cat_data["id"]] = new_cat.id
                log_success(f'restored category "{cat_data["name"]}"')
                for ow in cat_data["overwrites"]:
                    target = guild.get_role(ow["id"]) if ow["type"] == "role" else guild.get_member(ow["id"])
                    if target:
                        perms = discord.PermissionOverwrite.from_pair(
                            discord.Permissions(ow["allow"]),
                            discord.Permissions(ow["deny"])
                        )
                        await new_cat.set_permissions(target, overwrite=perms)
            except Exception as e:
                log_error(f'failed to restore category "{cat_data["name"]}": {e}')
        other_channels = [c for c in data["channels"] if c["type"] != 4]
        for ch_data in sorted(other_channels, key=lambda c: c["position"]):
            ch_type = discord.ChannelType(ch_data["type"])
            try:
                parent_id = id_map.get(ch_data["category_id"])
                parent = guild.get_channel(parent_id) if parent_id else None
                if ch_type in (discord.ChannelType.text, discord.ChannelType.news):
                    new_ch = await guild.create_text_channel(name=ch_data["name"],
                                                             category=parent,
                                                             position=ch_data["position"])
                    id_map[ch_data["id"]] = new_ch.id
                    log_success(f'restored text channel #{ch_data["name"]}')
                elif ch_type == discord.ChannelType.voice:
                    new_ch = await guild.create_voice_channel(name=ch_data["name"],
                                                              category=parent,
                                                              position=ch_data["position"])
                    id_map[ch_data["id"]] = new_ch.id
                    log_success(f'restored voice channel {ch_data["name"]}')
                else:
                    continue
                for ow in ch_data["overwrites"]:
                    target = guild.get_role(ow["id"]) if ow["type"] == "role" else guild.get_member(ow["id"])
                    if target:
                        perms = discord.PermissionOverwrite.from_pair(
                            discord.Permissions(ow["allow"]),
                            discord.Permissions(ow["deny"])
                        )
                        await new_ch.set_permissions(target, overwrite=perms)
            except Exception as e:
                log_error(f'failed to restore channel "{ch_data["name"]}": {e}')

        all_messages = {}
        if "messages" in data and isinstance(data["messages"], dict):
            all_messages.update(data["messages"])
        for ch_data in data["channels"]:
            if "messages" in ch_data and isinstance(ch_data["messages"], list):
                all_messages[str(ch_data["id"])] = ch_data["messages"]

        if include_messages and all_messages:
            log_info(f"restoring messages for {len(all_messages)} channels")
            sem = asyncio.Semaphore(MAX_WORKERS)
            async def restore_channel_messages(channel_id_str, msgs):
                new_channel = guild.get_channel(id_map.get(int(channel_id_str)))
                if new_channel and isinstance(new_channel, discord.TextChannel):
                    log_info(f"restoring messages to #{new_channel.name}")
                    for msg_data in msgs:
                        async with sem:
                            await send_restored_message(new_channel, msg_data)
                    log_success(f"finished messages in #{new_channel.name}")
                else:
                    log_warning(f"channel {channel_id_str} not found for messages")

            tasks = [restore_channel_messages(cid, msgs) for cid, msgs in all_messages.items()]
            await asyncio.gather(*tasks)
        elif include_messages:
            log_warning("no messages to restore")
    else:
        if include_messages:
            log_warning("channels not included, skipping messages")

async def send_restored_message(channel, msg_data, retry=3):
    content = msg_data["content"]
    embeds_original = msg_data.get("embeds", [])
    attachments = msg_data.get("attachments", [])
    author_name = msg_data["author_name"]
    avatar_url = msg_data["author_avatar_url"]
    timestamp = datetime.fromisoformat(msg_data["created_at_iso"])

    has_content = bool(content.strip()) if content else False
    has_original_embeds = bool(embeds_original)
    has_attachments = bool(attachments)
    if not has_content and not has_original_embeds and not has_attachments:
        return

    embeds_to_send = []

    if has_original_embeds:
        for embed_dict in embeds_original:
            embed = discord.Embed.from_dict(embed_dict)
            if embed.title:
                embed.title += f" @{author_name}"
            else:
                embed.title = f" @{author_name}"
            embeds_to_send.append(embed)
    elif has_content:
        embed = discord.Embed(
            title=f"Message by {author_name}",
            description=content,
            timestamp=timestamp
        )
        embed.set_thumbnail(url=avatar_url)
        time_str = timestamp.strftime("%H:%M:%S")
        date_str = timestamp.strftime("%d/%m/%Y")
        embed.set_footer(text=f"Sent by @{author_name} at {time_str} on {date_str}")
        embeds_to_send.append(embed)

    first_attachment = True
    for att in attachments:
        if att.get("content_type", "").startswith("image/"):
            img_embed = discord.Embed()
            img_embed.set_image(url=att["proxy_url"])
            if not has_original_embeds and not has_content and first_attachment:
                img_embed.title = f"Message by {author_name}"
                img_embed.set_thumbnail(url=avatar_url)
                time_str = timestamp.strftime("%H:%M:%S")
                date_str = timestamp.strftime("%d/%m/%Y")
                img_embed.set_footer(text=f"Sent by @{author_name} at {time_str} on {date_str}")
                first_attachment = False
            embeds_to_send.append(img_embed)
        else:
            file_embed = discord.Embed(
                title="Attachment",
                description=f"[{att['filename']}]({att['url']})"
            )
            if not has_original_embeds and not has_content and first_attachment:
                file_embed.title = f"Message by {author_name} (file)"
                file_embed.set_thumbnail(url=avatar_url)
                time_str = timestamp.strftime("%H:%M:%S")
                date_str = timestamp.strftime("%d/%m/%Y")
                file_embed.set_footer(text=f"Sent by @{author_name} at {time_str} on {date_str}")
                first_attachment = False
            embeds_to_send.append(file_embed)

    for i in range(0, len(embeds_to_send), 10):
        chunk = embeds_to_send[i:i+10]
        for attempt in range(retry):
            try:
                await channel.send(embeds=chunk)
                break
            except discord.HTTPException as e:
                if e.status == 429:
                    log_warning("rate limited, rotating proxy...")
                    rotated = await bot.handle_rate_limit(e)
                    if rotated:
                        continue
                    else:
                        log_error("proxy rotation failed, retrying in 1s")
                        await asyncio.sleep(1)
                else:
                    log_error(f"failed to send message (status {e.status}): {e}")
                    raise

@bot.event
async def on_ready():
    log_success(f"logged in as {bot.user}")

def require_admin():
    async def predicate(interaction: discord.Interaction):
        if not interaction.guild:
            raise app_commands.NoPrivateMessage()
        if not interaction.user.guild_permissions.administrator:
            raise app_commands.MissingPermissions(["administrator"])
        if not interaction.guild.me.guild_permissions.administrator:
            await interaction.response.send_message(
                "I need the Administrator permission.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)

@bot.tree.command(name="save", description="Save server state to a slot")
@app_commands.describe(
    slot="Slot number (1-3)",
    include_roles="Save roles and permissions",
    include_emojis="Save emojis",
    include_channels="Save channels and permissions",
    include_messages="Save message history",
    include_icon="Save server icon",
    include_server_settings="Save name and description"
)
@require_admin()
async def save_command(
    interaction: discord.Interaction,
    slot: int,
    include_roles: bool = True,
    include_emojis: bool = True,
    include_channels: bool = True,
    include_messages: bool = True,
    include_icon: bool = True,
    include_server_settings: bool = True
):
    if slot not in range(1, SLOT_COUNT + 1):
        await interaction.response.send_message(
            f"Slot must be 1-{SLOT_COUNT}.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    log_info(f"saving guild {guild.name} (id: {guild.id}) to slot {slot}")
    guild_data = await serialize_guild(
        guild,
        include_roles=include_roles,
        include_emojis=include_emojis,
        include_channels=include_channels,
        include_messages=include_messages,
        include_icon=include_icon,
        include_server_settings=include_server_settings
    )
    raw_json = json.dumps(guild_data, indent=2)
    size = len(raw_json.encode("utf-8"))
    wrapper = {
        "server_name": guild.name,
        "server_id": guild.id,
        "backup_timestamp": time.time(),
        "size_bytes": size,
        "guild_data": guild_data
    }
    save_slot(slot, wrapper)
    log_success(f"saved server to slot {slot} ({size} bytes)")
    await interaction.followup.send(
        f"Server saved to slot {slot} ({size} bytes).", ephemeral=True
    )

@bot.tree.command(name="load", description="Restore server from a slot")
@app_commands.describe(
    slot="Slot number (1-3)",
    include_roles="Restore roles and permissions",
    include_emojis="Restore emojis",
    include_channels="Restore channels and permissions",
    include_messages="Restore message history",
    include_icon="Restore server icon",
    include_server_settings="Restore name and description"
)
@require_admin()
async def load_command(
    interaction: discord.Interaction,
    slot: int,
    include_roles: bool = True,
    include_emojis: bool = True,
    include_channels: bool = True,
    include_messages: bool = True,
    include_icon: bool = True,
    include_server_settings: bool = True
):
    if slot not in range(1, SLOT_COUNT + 1):
        await interaction.response.send_message(
            f"Slot must be 1-{SLOT_COUNT}.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    wrapper = load_slot(slot)
    if wrapper is None:
        await interaction.followup.send(
            f"No save found in slot {slot}.", ephemeral=True
        )
        return
    guild_data = wrapper["guild_data"]
    guild = interaction.guild
    log_info(f"restoring slot {slot} to guild {guild.name} (id: {guild.id})")
    await restore_guild(
        guild,
        guild_data,
        include_roles=include_roles,
        include_emojis=include_emojis,
        include_channels=include_channels,
        include_messages=include_messages,
        include_icon=include_icon,
        include_server_settings=include_server_settings
    )
    log_success(f"restored server from slot {slot}")
    try:
        await interaction.followup.send(
            f"Server restored from slot {slot}.", ephemeral=True
        )
    except discord.NotFound:
        pass

@bot.tree.command(name="slots", description="Show information about saved slots")
@require_admin()
async def slots_command(interaction: discord.Interaction):
    embed = discord.Embed(title="Backup Slots", color=0x5865F2)
    found_any = False
    for s in range(1, SLOT_COUNT + 1):
        meta = get_slot_metadata(s)
        if meta:
            found_any = True
            ts = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(meta["backup_timestamp"])
            )
            val = (
                f"Server: {meta['server_name']}\n"
                f"ID: {meta['server_id']}\n"
                f"Backup: {ts}\n"
                f"Size: {meta['size_bytes']} bytes"
            )
        else:
            val = "Empty"
        embed.add_field(name=f"Slot {s}", value=val, inline=False)
    if not found_any:
        embed.description = "No backups stored yet."
    await interaction.response.send_message(embed=embed, ephemeral=True)

@save_command.error
@load_command.error
@slots_command.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "You need Administrator permission to use this command.", ephemeral=True
            )
        else:
            try:
                await interaction.followup.send(
                    "You need Administrator permission to use this command.", ephemeral=True
                )
            except:
                pass
    else:
        log_error(f"command error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("An error occurred.", ephemeral=True)
        else:
            try:
                await interaction.followup.send("An error occurred.", ephemeral=True)
            except:
                pass

bot.run(TOKEN)
