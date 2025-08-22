# (truncated message header for brevity)
import asyncio
import os
import re
import json
from dataclasses import dataclass
from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

import yt_dlp
from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials
from dotenv import load_dotenv

from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp
from pathlib import Path

# Load .env if present
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

INTENTS = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "skip_download": True,
    "source_address": "0.0.0.0",
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

SPOTIFY_URL_RE = re.compile(r"(https?://open\.spotify\.com/(track|album|playlist)/[A-Za-z0-9]+)")

# === Smoke Reminder Storage / Paths ===
BASE_DIR = Path(os.path.dirname(__file__))
SMOKE_STORE = str(BASE_DIR / "smoke_reminders.json")
DEFAULT_TZ = "UTC"
REMINDER_BEEP = str(BASE_DIR / "assets" / "reminder_beep.wav")
ASSETS_DIR = BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)

ALLOWED_AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".opus", ".m4a", ".webm"}

def guild_sound_path(gid: int) -> Path:
    return ASSETS_DIR / f"smoke_custom_{gid}.dat"  # container; FFmpeg will sniff

# {guild_id: {"channel_id": int, "times": ["HH:MM", ...], "tz": "Area/City", "message": str,
#             "last_fired": { "YYYYMMDDHHMM": True }, "interval_minutes": int|None, "interval_last_ts": int|None,
#             "sound": bool, "sound_path": str|None }}
smoke_cfg: dict[int, dict] = {}

def load_smoke():
    global smoke_cfg
    try:
        with open(SMOKE_STORE, "r") as f:
            smoke_cfg = json.load(f)
            smoke_cfg = {int(k): v for k, v in smoke_cfg.items()}
    except Exception:
        smoke_cfg = {}

def save_smoke():
    try:
        with open(SMOKE_STORE, "w") as f:
            json.dump(smoke_cfg, f)
    except Exception:
        pass

def parse_timestrings(times_str: str):
    out = []
    for part in times_str.split(","):
        t = part.strip()
        if not t: 
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if not m:
            continue
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh < 24 and 0 <= mm < 60:
            out.append(f"{hh:02d}:{mm:02d}")
    return out

def now_in_tz(tz_name: str):
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz)

async def play_beep_in_voice(guild: discord.Guild):
    """Play the guild's custom sound (or default beep) if voice is connected and idle."""
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return False
    if vc.is_playing() or vc.is_paused():
        return False  # don't interrupt music

    # choose sound file
    custom_path = smoke_cfg.get(guild.id, {}).get("sound_path")
    sound_file = custom_path if (custom_path and os.path.exists(custom_path)) else REMINDER_BEEP
    if not sound_file or not os.path.exists(sound_file):
        return False

    try:
        source = discord.FFmpegPCMAudio(sound_file, **FFMPEG_OPTS)
        vc.play(source)
        # wait up to ~2s for short clip to finish
        for _ in range(40):
            if not vc.is_playing():
                break
            await asyncio.sleep(0.05)
        return True
    except Exception:
        return False

@tasks.loop(seconds=30)
async def smoke_tick():
    for gid, cfg in list(smoke_cfg.items()):
        try:
            channel_id = cfg.get("channel_id")
            times = cfg.get("times") or []
            tzname = cfg.get("tz") or DEFAULT_TZ
            msg = cfg.get("message") or "🚬 Time to smoke!"
            last_fired = cfg.setdefault("last_fired", {})
            interval_minutes = cfg.get("interval_minutes")
            interval_last_ts = cfg.get("interval_last_ts")
            sound_on = cfg.get("sound", True)

            if not channel_id:
                continue
            ch = bot.get_channel(int(channel_id))
            if ch is None:
                continue

            now = now_in_tz(tzname)
            key_min = now.strftime("%Y%m%d%H%M")
            current_hhmm = now.strftime("%H:%M")

            fired = False

            # Mode A: specific daily times
            if times:
                if current_hhmm in times and key_min not in last_fired:
                    # Send text
                    await ch.send(msg)
                    # Try sound (only if in voice & idle)
                    guild = bot.get_guild(gid)
                    if sound_on and guild:
                        await play_beep_in_voice(guild)
                    last_fired[key_min] = True
                    fired = True

            # Mode B: interval minutes (only while bot is in voice chat)
            elif isinstance(interval_minutes, int) and interval_minutes > 0:
                guild = bot.get_guild(gid)
                voice_ok = guild and guild.voice_client and guild.voice_client.is_connected()
                if voice_ok:
                    now_ts = int(now.timestamp())
                    due = False
                    if not interval_last_ts:
                        due = True
                    else:
                        due = (now_ts - int(interval_last_ts)) >= (interval_minutes * 60)
                    if due:
                        # Prefer sound when idle; otherwise post text
                        did_sound = False
                        if sound_on and guild:
                            did_sound = await play_beep_in_voice(guild)
                        if not did_sound:
                            await ch.send(msg)
                        cfg["interval_last_ts"] = int(now_ts)
                        fired = True

            if fired:
                if len(last_fired) > 5000:
                    for k in sorted(last_fired.keys())[:-2000]:
                        last_fired.pop(k, None)
                save_smoke()
        except Exception:
            continue

@smoke_tick.before_loop
async def before_smoke_tick():
    await bot.wait_until_ready()

load_smoke()

# ===== Music player implementation =====
@dataclass
class Track:
    title: str
    url: str  # direct audio URL for FFmpeg
    webpage_url: Optional[str] = None
    requested_by: Optional[str] = None

class GuildPlayer:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.play_task: Optional[asyncio.Task] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.current: Optional[Track] = None
        self.stop_signal = asyncio.Event()

    async def ensure_player_task(self, guild: discord.Guild):
        if self.play_task is None or self.play_task.done():
            self.play_task = asyncio.create_task(self.player_loop(guild))

    async def player_loop(self, guild: discord.Guild):
        while not self.stop_signal.is_set():
            self.current = await self.queue.get()
            if self.voice is None or not self.voice.is_connected():
                self.current = None
                continue
            source = discord.FFmpegPCMAudio(self.current.url, **FFMPEG_OPTS)
            def after_play(err):
                guild._state.loop.call_soon_threadsafe(self.queue.task_done)
            self.voice.play(source, after=after_play)
            while self.voice.is_playing() or self.voice.is_paused():
                await asyncio.sleep(0.5)
            self.current = None

    def skip(self):
        if self.voice and self.voice.is_playing():
            self.voice.stop()

    def stop(self):
        self.stop_signal.set()
        if self.voice and self.voice.is_connected():
            try:
                asyncio.create_task(self.voice.disconnect(force=True))
            except Exception:
                pass

players: dict[int, GuildPlayer] = {}

def get_player(guild: discord.Guild) -> GuildPlayer:
    gp = players.get(guild.id)
    if not gp:
        gp = GuildPlayer(guild.id)
        players[guild.id] = gp
    return gp

def make_spotify_client() -> Optional[Spotify]:
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        auth = SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET
        )
        return Spotify(client_credentials_manager=auth)
    return None

sp_client = make_spotify_client()

async def youtube_search_first(query: str) -> Optional[Track]:
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
            if info is None:
                return None
            if "entries" in info and info["entries"]:
                info = info["entries"][0]
            stream_url = info.get("url") or info.get("webpage_url")
            if not stream_url:
                return None
            title = info.get("title") or query
            return Track(title=title, url=stream_url, webpage_url=info.get("webpage_url"))
        except Exception:
            return None

def parse_spotify(url: str) -> List[str]:
    if not sp_client:
        return []
    try:
        if "/track/" in url:
            tid = url.split("/track/")[1].split("?")[0]
            t = sp_client.track(tid)
            artists = ", ".join(a["name"] for a in t["artists"])
            return [f"{artists} - {t['name']} audio"]
        if "/album/" in url:
            aid = url.split("/album/")[1].split("?")[0]
            album = sp_client.album(aid)
            out = []
            for t in album["tracks"]["items"]:
                artists = ", ".join(a["name"] for a in t["artists"])
                out.append(f"{artists} - {t['name']} audio")
            return out
        if "/playlist/" in url:
            pid = url.split("/playlist/")[1].split("?")[0]
            pl = sp_client.playlist(pid)
            out = []
            for it in pl["tracks"]["items"]:
                t = it["track"]
                if not t:
                    continue
                artists = ", ".join(a["name"] for a in t["artists"])
                out.append(f"{artists} - {t['name']} audio")
            return out
    except Exception:
        return []
    return []

async def enqueue_from_input(guild: discord.Guild, query: str, requested_by: str) -> int:
    gp = get_player(guild)
    if not SPOTIFY_URL_RE.search(query):
        q = query if query.startswith("http") else f"ytsearch1:{query}"
        track = await youtube_search_first(q)
        if track:
            track.requested_by = requested_by
            await gp.queue.put(track)
            return 1
        return 0

    queries = parse_spotify(query)
    if not queries:
        queries = [query]

    added = 0
    for q in queries:
        tr = await youtube_search_first(f"ytsearch1:{q}")
        if tr:
            tr.requested_by = requested_by
            await gp.queue.put(tr)
            added += 1
    return added

# === Smoke Reminder Commands ===
smoke = app_commands.Group(name="smoke", description="Dooberhut smoke reminder controls")

@smoke.command(name="start35", description="Join your voice and start 35-min reminders with sound (voice-gated).")
@app_commands.describe(message="Optional custom message", channel="Channel to post text reminder in (defaults to current)")
async def smoke_start35(inter: discord.Interaction, message: str | None = None, channel: discord.TextChannel | None = None):
    await inter.response.defer(thinking=True)
    if not inter.guild or not isinstance(inter.user, discord.Member):
        return await inter.followup.send("Server-only command.")
    # Must be in a voice channel
    vs = inter.user.voice
    if not vs or not vs.channel:
        return await inter.followup.send("Join a voice channel first so I can connect.")
    # Connect/move to user's voice channel
    gp = get_player(inter.guild)
    if gp.voice and gp.voice.is_connected():
        await gp.voice.move_to(vs.channel)
    else:
        gp.voice = await vs.channel.connect(self_deaf=True)
        await gp.ensure_player_task(inter.guild)

    # Configure reminders: 35 minutes, sound on, this text channel (or selected channel)
    cfg = smoke_cfg.setdefault(inter.guild_id, {})
    ch = channel or inter.channel
    cfg["channel_id"] = ch.id
    cfg["interval_minutes"] = 35
    cfg["interval_last_ts"] = None  # start counting now
    if message:
        cfg["message"] = message
    cfg["times"] = []
    cfg.setdefault("tz", DEFAULT_TZ)
    cfg["sound"] = True
    save_smoke()

    await inter.followup.send(f"✅ Joined **{vs.channel.name}** and set smoke reminder every **35 minutes** (sound **on**) posting in {ch.mention}.")

@smoke.command(name="every", description="Post a smoke reminder every N minutes (only while in voice).")
@app_commands.describe(minutes="Interval in minutes (e.g., 35)", message="Optional custom message", channel="Channel to post in")
async def smoke_every(inter: discord.Interaction, minutes: int, message: str | None = None, channel: discord.TextChannel | None = None):
    await inter.response.defer(ephemeral=True)
    if not inter.guild:
        return await inter.followup.send("Server-only command.")
    if minutes < 1 or minutes > 1440:
        return await inter.followup.send("Choose 1–1440 minutes.")
    cfg = smoke_cfg.setdefault(inter.guild_id, {})
    ch = channel or inter.channel
    cfg["channel_id"] = ch.id
    cfg["interval_minutes"] = int(minutes)
    cfg["interval_last_ts"] = None  # start counting when first seen in voice
    if message:
        cfg["message"] = message
    cfg["times"] = []  # disable fixed-time mode
    cfg.setdefault("tz", DEFAULT_TZ)
    cfg.setdefault("sound", True)
    save_smoke()
    await inter.followup.send(f"✅ Interval reminders set: every **{minutes}** minutes **while in voice** in {ch.mention}.")

@smoke.command(name="sound", description="Turn the sound reminder on or off.")
@app_commands.describe(toggle="Choose 'on' or 'off'")
async def smoke_sound(inter: discord.Interaction, toggle: str):
    await inter.response.defer(ephemeral=True)
    t = toggle.lower().strip()
    if t not in ("on", "off"):
        return await inter.followup.send("Use `on` or `off`.")
    cfg = smoke_cfg.setdefault(inter.guild_id, {})
    cfg["sound"] = (t == "on")
    save_smoke()
    await inter.followup.send(f"🔊 Sound reminder: **{t}**.")

@smoke.command(name="soundset", description="Upload a custom sound file to play for reminders.")
@app_commands.describe(file="Audio file (.wav, .mp3, .ogg, .opus, .m4a, .webm)")
async def smoke_soundset(inter: discord.Interaction, file: discord.Attachment):
    await inter.response.defer(ephemeral=True, thinking=True)
    if not inter.guild:
        return await inter.followup.send("Server-only command.")
    # Validate extension
    ext = Path(file.filename).suffix.lower()
    if ext not in {".wav", ".mp3", ".ogg", ".opus", ".m4a", ".webm"}:
        return await inter.followup.send("Unsupported file type. Allowed: .wav, .mp3, .ogg, .opus, .m4a, .webm")
    # Download to guild-specific path
    out_path = (ASSETS_DIR / f"smoke_custom_{inter.guild_id}.dat")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(file.url) as resp:
                if resp.status != 200:
                    return await inter.followup.send("Could not download the file.")
                data = await resp.read()
        out_path.write_bytes(data)
    except Exception:
        return await inter.followup.send("Failed to save the file.")
    # Save into config
    cfg = smoke_cfg.setdefault(inter.guild_id, {})
    cfg["sound_path"] = str(out_path)
    cfg.setdefault("sound", True)
    save_smoke()
    return await inter.followup.send(f"🔊 Custom sound set: `{file.filename}`")

@smoke.command(name="soundurl", description="Use a direct URL for the reminder sound.")
@app_commands.describe(url="Direct link to audio (.wav/.mp3/.ogg/.opus/.m4a/.webm)")
async def smoke_soundurl(inter: discord.Interaction, url: str):
    await inter.response.defer(ephemeral=True, thinking=True)
    if not inter.guild:
        return await inter.followup.send("Server-only command.")
    # Fetch and store under guild path (let FFmpeg probe format)
    out_path = (ASSETS_DIR / f"smoke_custom_{inter.guild_id}.dat")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return await inter.followup.send("Could not download the URL (non-200).")
                # Simple size guard (~15 MB)
                size = int(resp.headers.get("Content-Length") or 0)
                if size and size > 15 * 1024 * 1024:
                    return await inter.followup.send("File too large (>15MB).")
                data = await resp.read()
        out_path.write_bytes(data)
    except Exception:
        return await inter.followup.send("Failed to fetch or save from URL.")
    cfg = smoke_cfg.setdefault(inter.guild_id, {})
    cfg["sound_path"] = str(out_path)
    cfg.setdefault("sound", True)
    save_smoke()
    return await inter.followup.send("🔊 Custom sound set from URL.")

@smoke.command(name="soundreset", description="Revert to the default beep sound.")
async def smoke_soundreset(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    cfg = smoke_cfg.setdefault(inter.guild_id, {})
    # Remove file if present
    p = cfg.get("sound_path")
    if p and os.path.exists(p):
        try:
            os.remove(p)
        except Exception:
            pass
    cfg["sound_path"] = None
    save_smoke()
    return await inter.followup.send("🔔 Reverted to default beep.")

@smoke.command(name="set", description="Set daily smoke times for this server.")
@app_commands.describe(times="Times like '4:20' or '09:30' (comma-separated)",
                       tz="IANA timezone like 'America/Los_Angeles' (default UTC)",
                       message="Custom reminder message",
                       channel="Channel to post in (defaults to current)")
async def smoke_set(inter: discord.Interaction, times: str, tz: str | None = None, message: str | None = None, channel: discord.TextChannel | None = None):
    await inter.response.defer(ephemeral=True, thinking=True)
    if not inter.guild:
        return await inter.followup.send("Server-only command.")
    parsed = parse_timestrings(times)
    if not parsed:
        return await inter.followup.send("Give one or more times like `4:20` or `09:30`, comma-separated.")
    tzname = tz or smoke_cfg.get(inter.guild_id, {}).get("tz") or DEFAULT_TZ
    ch = channel or inter.channel
    smoke_cfg[inter.guild_id] = {
        "channel_id": ch.id,
        "times": parsed,
        "tz": tzname,
        "message": message or smoke_cfg.get(inter.guild_id, {}).get("message") or "🚬 Time to smoke!",
        "last_fired": smoke_cfg.get(inter.guild_id, {}).get("last_fired", {}),
        "interval_minutes": None,
        "interval_last_ts": None,
        "sound": smoke_cfg.get(inter.guild_id, {}).get("sound", True),
        "sound_path": smoke_cfg.get(inter.guild_id, {}).get("sound_path", None),
    }
    save_smoke()
    await inter.followup.send(f"✅ Daily reminders set for {', '.join(parsed)} ({tzname}) in {ch.mention}.")

@smoke.command(name="message", description="Set a custom reminder message.")
@app_commands.describe(message="Text to post at reminder time")
async def smoke_message(inter: discord.Interaction, message: str):
    await inter.response.defer(ephemeral=True)
    if not inter.guild:
        return await inter.followup.send("Server-only command.")
    cfg = smoke_cfg.setdefault(inter.guild_id, {})
    cfg["message"] = message
    cfg.setdefault("times", [])
    cfg.setdefault("channel_id", inter.channel.id)
    cfg.setdefault("tz", DEFAULT_TZ)
    save_smoke()
    await inter.followup.send(f"📝 Message set to: {message}")

@smoke.command(name="tz", description="Set the timezone for smoke reminders.")
@app_commands.describe(tz="IANA timezone like 'America/New_York'")
async def smoke_tz(inter: discord.Interaction, tz: str):
    await inter.response.defer(ephemeral=True)
    if not inter.guild:
        return await inter.followup.send("Server-only command.")
    cfg = smoke_cfg.setdefault(inter.guild_id, {})
    cfg["tz"] = tz
    cfg.setdefault("times", [])
    cfg.setdefault("channel_id", inter.channel.id)
    cfg.setdefault("message", "🚬 Time to smoke!")
    save_smoke()
    await inter.followup.send(f"⏰ Timezone set to {tz}.")

@smoke.command(name="list", description="Show current smoke reminder settings.")
async def smoke_list(inter: discord.Interaction):
    cfg = smoke_cfg.get(inter.guild_id)
    if not cfg or (not cfg.get("times") and not cfg.get("interval_minutes")):
        return await inter.response.send_message("No smoke reminders set.", ephemeral=True)
    ch_id = cfg.get("channel_id")
    guild = inter.guild
    ch = guild.get_channel(ch_id) if guild and ch_id else None
    channel_name = ch.mention if ch else (f"<#{ch_id}>" if ch_id else "(not set)")
    mode = f"interval {cfg.get('interval_minutes')} min (voice-gated)" if cfg.get("interval_minutes") else f"times: {', '.join(cfg.get('times', []))}"
    await inter.response.send_message(
        f"Mode: {mode}\nTZ: {cfg.get('tz', DEFAULT_TZ)}\nChannel: {channel_name}\nMessage: {cfg.get('message', '🚬 Time to smoke!')}\nSound: {'on' if cfg.get('sound', True) else 'off'}" ,
        ephemeral=True
    )

@smoke.command(name="off", description="Turn off smoke reminders for this server (both modes).")
async def smoke_off(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    cfg = smoke_cfg.setdefault(inter.guild_id, {})
    cfg["times"] = []
    cfg["interval_minutes"] = None
    cfg["interval_last_ts"] = None
    save_smoke()
    await inter.followup.send("🛑 Smoke reminders turned off.")

@smoke.command(name="test", description="Send a test smoke reminder now.")
async def smoke_test(inter: discord.Interaction):
    await inter.response.defer()
    guild = inter.guild
    did_sound = False
    if guild:
        did_sound = await play_beep_in_voice(guild)
    if not did_sound:
        await inter.followup.send("🚬 Time to smoke! (test)")

# Register the group
try:
    tree.add_command(smoke)
except Exception:
    pass

# ===== Music commands =====
@dataclass
class Track:
    title: str
    url: str  # direct audio URL for FFmpeg
    webpage_url: Optional[str] = None
    requested_by: Optional[str] = None

class GuildPlayer:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.play_task: Optional[asyncio.Task] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.current: Optional[Track] = None
        self.stop_signal = asyncio.Event()

    async def ensure_player_task(self, guild: discord.Guild):
        if self.play_task is None or self.play_task.done():
            self.play_task = asyncio.create_task(self.player_loop(guild))

    async def player_loop(self, guild: discord.Guild):
        while not self.stop_signal.is_set():
            self.current = await self.queue.get()
            if self.voice is None or not self.voice.is_connected():
                self.current = None
                continue
            source = discord.FFmpegPCMAudio(self.current.url, **FFMPEG_OPTS)
            def after_play(err):
                guild._state.loop.call_soon_threadsafe(self.queue.task_done)
            self.voice.play(source, after=after_play)
            while self.voice.is_playing() or self.voice.is_paused():
                await asyncio.sleep(0.5)
            self.current = None

    def skip(self):
        if self.voice and self.voice.is_playing():
            self.voice.stop()

    def stop(self):
        self.stop_signal.set()
        if self.voice and self.voice.is_connected():
            try:
                asyncio.create_task(self.voice.disconnect(force=True))
            except Exception:
                pass

players: dict[int, GuildPlayer] = {}

def get_player(guild: discord.Guild) -> GuildPlayer:
    gp = players.get(guild.id)
    if not gp:
        gp = GuildPlayer(guild.id)
        players[guild.id] = gp
    return gp

def make_spotify_client() -> Optional[Spotify]:
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        auth = SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET
        )
        return Spotify(client_credentials_manager=auth)
    return None

sp_client = make_spotify_client()

async def youtube_search_first(query: str) -> Optional[Track]:
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
            if info is None:
                return None
            if "entries" in info and info["entries"]:
                info = info["entries"][0]
            stream_url = info.get("url") or info.get("webpage_url")
            if not stream_url:
                return None
            title = info.get("title") or query
            return Track(title=title, url=stream_url, webpage_url=info.get("webpage_url"))
        except Exception:
            return None

def parse_spotify(url: str) -> List[str]:
    if not sp_client:
        return []
    try:
        if "/track/" in url:
            tid = url.split("/track/")[1].split("?")[0]
            t = sp_client.track(tid)
            artists = ", ".join(a["name"] for a in t["artists"])
            return [f"{artists} - {t['name']} audio"]
        if "/album/" in url:
            aid = url.split("/album/")[1].split("?")[0]
            album = sp_client.album(aid)
            out = []
            for t in album["tracks"]["items"]:
                artists = ", ".join(a["name"] for a in t["artists"])
                out.append(f"{artists} - {t['name']} audio")
            return out
        if "/playlist/" in url:
            pid = url.split("/playlist/")[1].split("?")[0]
            pl = sp_client.playlist(pid)
            out = []
            for it in pl["tracks"]["items"]:
                t = it["track"]
                if not t:
                    continue
                artists = ", ".join(a["name"] for a in t["artists"])
                out.append(f"{artists} - {t['name']} audio")
            return out
    except Exception:
        return []
    return []

async def enqueue_from_input(guild: discord.Guild, query: str, requested_by: str) -> int:
    gp = get_player(guild)
    if not SPOTIFY_URL_RE.search(query):
        q = query if query.startswith("http") else f"ytsearch1:{query}"
        track = await youtube_search_first(q)
        if track:
            track.requested_by = requested_by
            await gp.queue.put(track)
            return 1
        return 0

    queries = parse_spotify(query)
    if not queries:
        queries = [query]

    added = 0
    for q in queries:
        tr = await youtube_search_first(f"ytsearch1:{q}")
        if tr:
            tr.requested_by = requested_by
            await gp.queue.put(tr)
            added += 1
    return added

@tree.command(name="join", description="Have Dooberhut Bot join your current voice channel.")
async def join(inter: discord.Interaction):
    if not inter.user or not isinstance(inter.user, discord.Member):
        return await inter.response.send_message("Dooberhut Bot couldn't find your voice channel.", ephemeral=True)
    voice_state = inter.user.voice
    if not voice_state or not voice_state.channel:
        return await inter.response.send_message("Join a voice channel first.", ephemeral=True)
    channel = voice_state.channel

    gp = get_player(inter.guild)
    if gp.voice and gp.voice.is_connected():
        await gp.voice.move_to(channel)
    else:
        gp.voice = await channel.connect(self_deaf=True)
        await gp.ensure_player_task(inter.guild)

    await inter.response.send_message(f"🤖 Dooberhut Bot joined **{channel.name}**.")

@tree.command(name="play", description="Play a song by name, YouTube link, or Spotify link.")
@app_commands.describe(query="Song name, YouTube URL, or Spotify track/album/playlist URL")
async def play(inter: discord.Interaction, query: str):
    await inter.response.defer(thinking=True)
    if not inter.guild:
        return await inter.followup.send("This command only works in servers.")
    gp = get_player(inter.guild)
    if not gp.voice or not gp.voice.is_connected():
        if isinstance(inter.user, discord.Member) and inter.user.voice and inter.user.voice.channel:
            gp.voice = await inter.user.voice.channel.connect(self_deaf=True)
            await gp.ensure_player_task(inter.guild)
        else:
            return await inter.followup.send("Dooberhut Bot isn't in a voice channel. Use `/join` first.")
    added = await enqueue_from_input(inter.guild, query, requested_by=inter.user.display_name)
    if added == 0:
        return await inter.followup.send("Couldn't find anything to play.")
    await gp.ensure_player_task(inter.guild)
    await inter.followup.send(f"Queued **{added}** track(s). Use `/queue` to view.")

@tree.command(name="queue", description="Show upcoming songs.")
async def queue_cmd(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Server-only command.")
    gp = get_player(inter.guild)
    items = list(gp.queue._queue)
    lines = []
    if gp.current:
        lines.append(f"**Now:** {gp.current.title} *(requested by {gp.current.requested_by})*")
    if not items:
        if not lines:
            return await inter.response.send_message("Queue is empty.")
    else:
        for i, t in enumerate(items[:10], start=1):
            lines.append(f"{i}. {t.title} *(requested by {t.requested_by})*")
        if len(items) > 10:
            lines.append(f"...and {len(items) - 10} more")
    await inter.response.send_message("\n".join(lines))

@tree.command(name="skip", description="Skip the current song.")
async def skip_cmd(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Server-only command.")
    gp = get_player(inter.guild)
    gp.skip()
    await inter.response.send_message("⏭️ Skipped.")

@tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop_cmd(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Server-only command.")
    gp = get_player(inter.guild)
    while not gp.queue.empty():
        try:
            gp.queue.get_nowait()
            gp.queue.task_done()
        except Exception:
            break
    gp.skip()
    await inter.response.send_message("⏹️ Stopped and cleared queue.")

@tree.command(name="leave", description="Disconnect Dooberhut Bot from voice.")
async def leave_cmd(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Server-only command.")
    gp = get_player(inter.guild)
    gp.stop()
    await inter.response.send_message("👋 Dooberhut Bot left the voice channel.")

@bot.event
async def on_ready():
    try:
        await tree.sync()
    except Exception as e:
        print("Slash sync failed:", e)
    await bot.change_presence(activity=discord.Game(name="music in Dooberhut 🎶"))
    smoke_tick.start()
    print(f"✅ Dooberhut Bot is online as {bot.user} (ID: {bot.user.id})")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment or .env file.")
    bot.run(DISCORD_TOKEN)
