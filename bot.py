# bot.py - Monitor bot (website keyword check) + Flask web service + slash commands
import os
import io
import json
import sqlite3
import threading
import urllib.parse
import asyncio
from datetime import datetime, timedelta

import aiohttp
import discord
from discord.ext import commands, tasks
from flask import Flask, jsonify
from dotenv import load_dotenv

load_dotenv()

# ========== ENV ========== (fill these in Render or .env)
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_USER_IDS = os.getenv("OWNER_USER_IDS", "")   # comma-separated user IDs who receive DMs
GUILD_ID = os.getenv("GUILD_ID", "")               # set to your guild id to force guild sync (bot must be in it)
CHECK_URL = os.getenv("CHECK_URL")                 # website to fetch (no site changes required)
ONLINE_KEYWORD = os.getenv("ONLINE_KEYWORD", "Online")
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "5"))
REQUEST_TIMEOUT_S = int(os.getenv("REQUEST_TIMEOUT_S", "10"))
QUICKCHART_URL = os.getenv("QUICKCHART_URL", "https://quickchart.io/chart")
DB_PATH = os.getenv("DB_PATH", "monitor.db")
FLASK_PORT = int(os.getenv("PORT", "3000"))       # Render provides PORT

if not BOT_TOKEN or not OWNER_USER_IDS or not CHECK_URL:
    print("ERROR: BOT_TOKEN, OWNER_USER_IDS and CHECK_URL must be set")
    raise SystemExit(1)

OWNER_IDS = [int(x.strip()) for x in OWNER_USER_IDS.split(",") if x.strip()]

# ========== SQLite DB (settings, logs, downtimes) ==========
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    interval_min INTEGER DEFAULT ?,
    timeout_s INTEGER DEFAULT ?,
    response_keyword TEXT DEFAULT ?,
    channel_id TEXT DEFAULT ''
)""", (CHECK_INTERVAL_MIN, REQUEST_TIMEOUT_S, ONLINE_KEYWORD))
# Above uses parameterized defaults for portability; if table existed this has no effect

# create logs & downtimes
cur.execute("CREATE TABLE IF NOT EXISTS logs (ts INTEGER, up INTEGER)")
cur.execute("""CREATE TABLE IF NOT EXISTS downtimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts INTEGER,
    end_ts INTEGER
)""")
conn.commit()

# ensure single settings row
r = cur.execute("SELECT COUNT(*) FROM settings WHERE id=1").fetchone()
if r is None or r[0] == 0:
    cur.execute("INSERT INTO settings(id, interval_min, timeout_s, response_keyword) VALUES (1, ?, ?, ?)",
                (CHECK_INTERVAL_MIN, REQUEST_TIMEOUT_S, ONLINE_KEYWORD))
    conn.commit()

def db_get(sql, params=()):
    c = conn.cursor()
    c.execute(sql, params)
    return c.fetchone()

def db_all(sql, params=()):
    c = conn.cursor()
    c.execute(sql, params)
    return c.fetchall()

def db_run(sql, params=()):
    c = conn.cursor()
    c.execute(sql, params)
    conn.commit()
    return c

def get_settings():
    row = db_get("SELECT interval_min, timeout_s, response_keyword, channel_id FROM settings WHERE id=1")
    return {
        "interval_min": row[0],
        "timeout_s": row[1],
        "response_keyword": row[2],
        "channel_id": row[3]
    }

def update_setting(field, value):
    if field not in ("interval_min","timeout_s","response_keyword","channel_id"):
        raise ValueError("bad field")
    db_run(f"UPDATE settings SET {field} = ? WHERE id=1", (value,))

def insert_log(ts_ms, up):
    db_run("INSERT INTO logs(ts, up) VALUES (?, ?)", (ts_ms, up))

def start_downtime(start_ts):
    db_run("INSERT INTO downtimes(start_ts, end_ts) VALUES (?, NULL)", (start_ts,))

def end_last_downtime(end_ts):
    db_run("UPDATE downtimes SET end_ts = ? WHERE id = (SELECT id FROM downtimes ORDER BY id DESC LIMIT 1)", (end_ts,))

def get_last_downtime():
    return db_get("SELECT start_ts, end_ts FROM downtimes ORDER BY id DESC LIMIT 1")

def db_logs_since(ms_since):
    return db_all("SELECT ts, up FROM logs WHERE ts >= ? ORDER BY ts ASC", (ms_since,))

# ========== Flask app for Render port detection and simple API ==========
flask_app = Flask("monitor_web")

# expose a small JSON endpoint describing the observed status + last check
# OBSERVED_STATUS is updated by the monitor loop
OBSERVED_STATUS = {"online": False, "last_check_ts": None, "last_msg": None}

@flask_app.route("/")
def index():
    s = get_settings()
    return jsonify({
        "service": "maxy-monitor",
        "observed_online": OBSERVED_STATUS["online"],
        "last_check": datetime.utcfromtimestamp(OBSERVED_STATUS["last_check_ts"]/1000).isoformat()+"Z" if OBSERVED_STATUS["last_check_ts"] else None,
        "settings": s
    })

@flask_app.route("/_health")
def health():
    return "ok", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)

# ========== Discord bot ==========
intents = discord.Intents.default()
# set message_content to True only to silence the warning; not required for slash commands
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

observed_status = None     # "online" or "offline" or None initially
downtime_start = None

# ---------- HTTP helpers ----------
async def fetch_text(url, timeout_s):
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=timeout_s) as resp:
            txt = await resp.text()
            return resp.status, txt

async def notify_owners_dm(content: str, file_bytes: bytes = None, filename: str = "chart.png"):
    for uid in OWNER_IDS:
        try:
            user = await bot.fetch_user(uid)
            if file_bytes:
                bio = io.BytesIO(file_bytes)
                bio.seek(0)
                await user.send(content, file=discord.File(bio, filename=filename))
            else:
                await user.send(content)
        except discord.Forbidden:
            print(f"[WARN] Cannot DM {uid} - forbidden (user DMs disabled or blocked bot)")
        except Exception as e:
            print(f"[WARN] Failed to DM {uid}: {e}")

# ---------- uptime calculations ----------
def uptime_percent(hours: int):
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    since_ms = now_ms - (hours * 3600 * 1000)
    rows = db_logs_since(since_ms)
    if not rows:
        return 100.0
    total = len(rows)
    up = sum(r[1] for r in rows)
    return round((up / total) * 100, 2)

# ---------- QuickChart image builder ----------
async def build_quickchart_png(labels, values):
    cfg = {
        "type": "line",
        "data": {"labels": labels, "datasets":[{"label":"Uptime %","data":values,"fill":True,"borderColor":"#39d353","backgroundColor":"rgba(57,211,83,0.08)"}]},
        "options": {"scales": {"y": {"min":0,"max":100}}, "plugins":{"legend":{"display":False}}}
    }
    q = urllib.parse.quote_plus(json.dumps(cfg, separators=(",",":")))
    url = f"{QUICKCHART_URL}?c={q}&format=png&width=800&height=300"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"QuickChart returned {resp.status}")
            return await resp.read()

# ---------- core check - returns tuple (is_online:bool, message:str, ts_ms:int) ----------
async def run_check_once():
    global observed_status, downtime_start, OBSERVED_STATUS
    s = get_settings()
    keyword = (s["response_keyword"] or ONLINE_KEYWORD).strip()
    timeout = int(s["timeout_s"] or REQUEST_TIMEOUT_S)
    try:
        status_code, page_text = await fetch_text(CHECK_URL, timeout)
        found = keyword.lower() in page_text.lower()
    except Exception as e:
        print("Fetch error:", e)
        found = False

    ts_ms = int(datetime.utcnow().timestamp() * 1000)
    insert_log(ts_ms, 1 if found else 0)

    # update observed global for Flask endpoint
    OBSERVED_STATUS["online"] = bool(found)
    OBSERVED_STATUS["last_check_ts"] = ts_ms
    OBSERVED_STATUS["last_msg"] = None

    if found:
        # recovered or stays online
        if observed_status != "online":
            # was offline -> now recovered
            if downtime_start:
                end_ts = ts_ms
                end_last_downtime(end_ts)
                downtime_secs = (end_ts - downtime_start) // 1000
                downtime_start = None
                msg = f"✅ Maxy BACK ONLINE (downtime {downtime_secs}s)\n{CHECK_URL}"
            else:
                msg = f"✅ Maxy ONLINE\n{CHECK_URL}"
            # notify owners asynchronously
            asyncio.create_task(notify_owners_dm(msg))
            print("Owners notified: ONLINE")
        observed_status = "online"
        return True, "ONLINE", ts_ms
    else:
        # detected offline
        if observed_status != "offline":
            observed_status = "offline"
            downtime_start = ts_ms
            start_downtime(downtime_start)
            msg = f"🔴 Maxy OFFLINE (keyword not found)\n{CHECK_URL}"
            asyncio.create_task(notify_owners_dm(msg))
            print("Owners notified: OFFLINE")
        return False, "OFFLINE", ts_ms

# ---------- monitor worker (reads interval from settings) ----------
async def monitor_worker():
    await bot.wait_until_ready()
    while True:
        try:
            s = get_settings()
            interval_min = max(1, int(s["interval_min"]))
            await run_check_once()
        except Exception as e:
            print("Monitor worker error:", e)
        await asyncio.sleep(interval_min * 60)

# ---------- Discord UI: Settings (modals & buttons) ----------
from discord.ui import View, Button, Modal, TextInput

class EditModal(Modal):
    def __init__(self, field, label, placeholder, style=discord.TextStyle.short):
        super().__init__(title=f"Edit {label}")
        self.field = field
        self.input = TextInput(label=label, placeholder=placeholder, style=style)
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.input.value.strip()
        if self.field in ("interval_min","timeout_s"):
            try:
                valn = int(val)
                update_setting(self.field, valn)
            except:
                await interaction.response.send_message("Invalid number", ephemeral=True)
                return
        else:
            update_setting(self.field, val)
        await interaction.response.send_message(f"Saved {self.field} = {val}", ephemeral=True)

class SettingsView(View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=300)
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker_id or interaction.user.id in OWNER_IDS

    @discord.ui.button(label="Edit Interval (min)", style=discord.ButtonStyle.secondary)
    async def edit_interval(self, button: Button, interaction: discord.Interaction):
        await interaction.response.send_modal(EditModal("interval_min", "Interval (minutes)", "5"))

    @discord.ui.button(label="Edit Timeout (s)", style=discord.ButtonStyle.secondary)
    async def edit_timeout(self, button: Button, interaction: discord.Interaction):
        await interaction.response.send_modal(EditModal("timeout_s", "Timeout (seconds)", "10"))

    @discord.ui.button(label="Edit Keyword", style=discord.ButtonStyle.primary)
    async def edit_keyword(self, button: Button, interaction: discord.Interaction):
        await interaction.response.send_modal(EditModal("response_keyword", "Online keyword", ONLINE_KEYWORD))

    @discord.ui.button(label="Show current", style=discord.ButtonStyle.success)
    async def show_current(self, button: Button, interaction: discord.Interaction):
        s = get_settings()
        lines = [f"{k}: {v}" for k,v in s.items()]
        await interaction.response.send_message("Current settings:\n" + "\n".join(lines), ephemeral=True)

# ---------- Slash commands (/health, /status, /settings, /forcecheck) ----------
@bot.tree.command(name="health", description="Show Maxy health summary (chart + text).")
async def health(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        u24 = uptime_percent(24)
        u7 = uptime_percent(24*7)
        u30 = uptime_percent(24*30)
        last_inc = get_last_downtime()
        if last_inc:
            s_ts, e_ts = last_inc
            last_inc_str = f"{datetime.fromtimestamp(s_ts/1000).strftime('%c')}" + (f" → {datetime.fromtimestamp(e_ts/1000).strftime('%c')}" if e_ts else " (ongoing)")
        else:
            last_inc_str = "No incidents"
        # hourly buckets last 24 hours
        now = datetime.utcnow()
        labels = []
        values = []
        for i in range(23, -1, -1):
            end = now - timedelta(hours=i)
            labels.append(end.strftime("%H:%M"))
            start_ms = int((end - timedelta(hours=1)).timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            rows = db_all("SELECT up FROM logs WHERE ts >= ? AND ts < ?", (start_ms, end_ms))
            if not rows:
                values.append(100)
            else:
                total = len(rows); upcount = sum(r[0] for r in rows)
                values.append(round((upcount/total) * 100, 2))
        chart_png = await build_quickchart_png(labels, values)
        text = f"Maxy health\n24h: {u24}% • 7d: {u7}% • 30d: {u30}%\n{last_inc_str}"
        file = discord.File(io.BytesIO(chart_png), filename="health.png")
        embed = discord.Embed(title="Maxy Health", description=text)
        embed.set_image(url="attachment://health.png")
        await interaction.followup.send(embed=embed, file=file)
        # DM owners
        await notify_owners_dm(text, file_bytes=chart_png, filename="health.png")
    except Exception as e:
        print("health error:", e)
        await interaction.followup.send("Error generating health summary.")

@bot.tree.command(name="status", description="Show quick Maxy status.")
async def status(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        u24 = uptime_percent(24)
        u7 = uptime_percent(24*7)
        u30 = uptime_percent(24*30)
        last_inc = get_last_downtime()
        if last_inc:
            s_ts, e_ts = last_inc
            last_inc_str = f"{datetime.fromtimestamp(s_ts/1000).strftime('%c')}" + (f" → {datetime.fromtimestamp(e_ts/1000).strftime('%c')}" if e_ts else " (ongoing)")
        else:
            last_inc_str = "No incidents"
        last_row = db_all("SELECT ts, up FROM logs ORDER BY ts DESC LIMIT 1")
        if last_row:
            last_check = datetime.fromtimestamp(last_row[0][0]/1000).strftime('%c')
            last_up = "ONLINE" if last_row[0][1] == 1 else "OFFLINE"
        else:
            last_check = "N/A"; last_up = "N/A"
        color = discord.Color.green() if last_up == "ONLINE" else discord.Color.red()
        embed = discord.Embed(title="Maxy Quick Status", color=color)
        embed.add_field(name="Current", value=last_up, inline=True)
        embed.add_field(name="Last checked", value=last_check, inline=True)
        embed.add_field(name="24h", value=f"{u24}%", inline=True)
        embed.add_field(name="7d", value=f"{u7}%", inline=True)
        embed.add_field(name="30d", value=f"{u30}%", inline=True)
        embed.add_field(name="Last incident", value=last_inc_str, inline=False)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        print("status error:", e)
        await interaction.followup.send("Error fetching status.")

@bot.tree.command(name="settings", description="Open monitor settings (owners only).")
async def settings(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_IDS:
        await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
        return
    s = get_settings()
    embed = discord.Embed(title="Monitor Settings", description="Edit using the buttons.", color=discord.Color.blue())
    for k,v in s.items():
        embed.add_field(name=k, value=str(v), inline=False)
    view = SettingsView(invoker_id=interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="forcecheck", description="Force an immediate check (owners only).")
async def forcecheck(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_IDS:
        await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        is_online, msg, ts_ms = await run_check_once()
        last_check = datetime.fromtimestamp(ts_ms/1000).strftime('%c')
        color = discord.Color.green() if is_online else discord.Color.red()
        embed = discord.Embed(title="Force Check Result", color=color)
        embed.add_field(name="Status", value=("ONLINE" if is_online else "OFFLINE"), inline=True)
        embed.add_field(name="Checked at", value=last_check, inline=True)
        last_inc = get_last_downtime()
        if last_inc:
            s_ts, e_ts = last_inc
            last_inc_str = f"{datetime.fromtimestamp(s_ts/1000).strftime('%c')}" + (f" → {datetime.fromtimestamp(e_ts/1000).strftime('%c')}" if e_ts else " (ongoing)")
        else:
            last_inc_str = "No incidents"
        embed.add_field(name="Last incident", value=last_inc_str, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        print("/forcecheck error:", e)
        await interaction.followup.send("Error running check.", ephemeral=True)

# prefix fallbacks
@bot.command(name="health")
async def health_cmd(ctx):
    await ctx.send("Generating health summary... (may take a moment)")
    await health.callback(ctx)

@bot.command(name="status")
async def status_cmd(ctx):
    await ctx.invoke(status)

@bot.command(name="forcecheck")
async def forcecheck_cmd(ctx):
    if ctx.author.id not in OWNER_IDS:
        await ctx.send("You are not allowed to use this command.")
        return
    await ctx.send("Running force check...")
    is_online, msg, ts_ms = await run_check_once()
    last_check = datetime.fromtimestamp(ts_ms/1000).strftime('%c')
    color = 0x2ecc71 if is_online else 0xe74c3c
    embed = discord.Embed(title="Force Check Result", color=color)
    embed.add_field(name="Status", value=("ONLINE" if is_online else "OFFLINE"), inline=True)
    embed.add_field(name="Checked at", value=last_check, inline=True)
    last_inc = get_last_downtime()
    if last_inc:
        s_ts, e_ts = last_inc
        last_inc_str = f"{datetime.fromtimestamp(s_ts/1000).strftime('%c')}" + (f" → {datetime.fromtimestamp(e_ts/1000).strftime('%c')}" if e_ts else " (ongoing)")
    else:
        last_inc_str = "No incidents"
    embed.add_field(name="Last incident", value=last_inc_str, inline=False)
    await ctx.send(embed=embed)

# ========== on_ready: force guild sync if requested, start monitor ==========
@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} Owners: {OWNER_IDS}")
    try:
        if GUILD_ID:
            try:
                gid = int(GUILD_ID)
                await bot.tree.sync(guild=discord.Object(id=gid))
                print("Synced commands to guild", GUILD_ID)
            except Exception as e:
                print("Guild sync failed:", e, "falling back to global sync")
                await bot.tree.sync()
                print("Synced global commands")
        else:
            await bot.tree.sync()
            print("Synced global commands")
    except Exception as e:
        print("Slash sync failed:", e)
    # start monitor worker
    bot.loop.create_task(monitor_worker())

# ========== start Flask thread & run bot ==========
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(BOT_TOKEN)
