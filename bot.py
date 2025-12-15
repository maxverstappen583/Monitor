# bot.py — updated: force guild sync + /forcecheck command
import os
import io
import json
import asyncio
import sqlite3
import urllib.parse
import threading
from datetime import datetime, timedelta

import aiohttp
import discord
from discord.ext import tasks, commands
from flask import Flask, jsonify
from dotenv import load_dotenv

load_dotenv()

# ========== ENV / CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_USER_IDS = os.getenv("OWNER_USER_IDS", "")   # comma separated
STATUS_PAGE_URL = os.getenv("STATUS_PAGE_URL")
ONLINE_KEYWORD = os.getenv("ONLINE_KEYWORD", "Online")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
TIMEOUT_SECONDS = int(os.getenv("TIMEOUT_SECONDS", "10"))
QUICKCHART_URL = os.getenv("QUICKCHART_URL", "https://quickchart.io/chart")
GUILD_ID = os.getenv("GUILD_ID", "")   # <--- set this to force guild sync (bot must be in this guild)
DB_PATH = os.getenv("DB_PATH", "monitor.db")
FLASK_PORT = int(os.getenv("PORT", "3000"))  # Render sets PORT env

if not BOT_TOKEN or not OWNER_USER_IDS or not STATUS_PAGE_URL:
    print("ERROR: please set BOT_TOKEN, OWNER_USER_IDS, STATUS_PAGE_URL")
    raise SystemExit(1)

OWNER_IDS = [int(x.strip()) for x in OWNER_USER_IDS.split(",") if x.strip()]

# ========== SQLite helpers ==========
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id=1),
    channel_id TEXT DEFAULT '',
    interval_min INTEGER DEFAULT 5,
    timeout_s INTEGER DEFAULT 10,
    response_keyword TEXT DEFAULT 'Online',
    auto_ping_url TEXT DEFAULT NULL,
    auto_ping_interval_s INTEGER DEFAULT 300
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS logs ( ts INTEGER, up INTEGER )""")
cur.execute("""CREATE TABLE IF NOT EXISTS downtimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts INTEGER,
    end_ts INTEGER
)""")
conn.commit()

def dbGet(sql, params=()):
    c = conn.cursor()
    c.execute(sql, params)
    return c.fetchone()

def dbAll(sql, params=()):
    c = conn.cursor()
    c.execute(sql, params)
    return c.fetchall()

def dbRun(sql, params=()):
    c = conn.cursor()
    c.execute(sql, params)
    conn.commit()
    return c

if not dbGet("SELECT 1 FROM settings WHERE id=1"):
    dbRun("INSERT INTO settings(id) VALUES (1)")

def get_settings():
    row = dbGet("SELECT channel_id, interval_min, timeout_s, response_keyword, auto_ping_url, auto_ping_interval_s FROM settings WHERE id=1")
    return {
        "channel_id": row[0] or "",
        "interval_min": row[1] or CHECK_INTERVAL_MINUTES,
        "timeout_s": row[2] or TIMEOUT_SECONDS,
        "response_keyword": row[3] or ONLINE_KEYWORD,
        "auto_ping_url": row[4],
        "auto_ping_interval_s": row[5] or 300
    }

def update_setting(field, value):
    if field not in ("channel_id","interval_min","timeout_s","response_keyword","auto_ping_url","auto_ping_interval_s"):
        raise ValueError("bad field")
    dbRun(f"UPDATE settings SET {field}=? WHERE id=1", (value,))

def insert_log(ts_ms, up):
    dbRun("INSERT INTO logs(ts, up) VALUES (?, ?)", (ts_ms, up))

def start_downtime(start_ts):
    dbRun("INSERT INTO downtimes(start_ts, end_ts) VALUES (?, NULL)", (start_ts,))

def end_last_downtime(end_ts):
    dbRun("UPDATE downtimes SET end_ts=? WHERE id=(SELECT id FROM downtimes ORDER BY id DESC LIMIT 1)", (end_ts,))

def get_last_downtime():
    return dbGet("SELECT start_ts, end_ts FROM downtimes ORDER BY id DESC LIMIT 1")

def dbAll_sync(query, params=()):
    c = conn.cursor()
    c.execute(query, params)
    return c.fetchall()

# ========== Flask (small web service so Render sees a port) ==========
app = Flask("monitor_web")

@app.route("/")
def index():
    s = get_settings()
    last = dbGet("SELECT up, ts FROM logs ORDER BY ts DESC LIMIT 1")
    status = "unknown"
    last_checked = None
    if last:
        status = "online" if last[0] == 1 else "offline"
        last_checked = datetime.utcfromtimestamp(last[1]/1000).isoformat() + "Z"
    return jsonify({
        "status": status,
        "last_checked": last_checked,
        "settings": s
    })

@app.route("/_health")
def health():
    return "ok"

def run_flask():
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)

# ========== Discord bot ==========
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

observed_status = None
downtime_start = None

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
            print(f"Forbidden to DM {uid} (they may have DMs disabled or blocked the bot)")
        except Exception as e:
            print(f"Failed to DM {uid}: {e}")

def compute_uptime_percent(hours: int):
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    since_ms = now_ms - (hours * 3600 * 1000)
    rows = dbAll("SELECT up FROM logs WHERE ts >= ? ORDER BY ts ASC", (since_ms,))
    if not rows:
        return 100.00
    total = len(rows)
    up = sum(r[0] for r in rows)
    return round((up/total)*100, 2)

async def build_quickchart_png(labels, values):
    cfg = {"type":"line","data":{"labels":labels,"datasets":[{"label":"Uptime %","data":values,"fill":True,"borderColor":"#39d353","backgroundColor":"rgba(57,211,83,0.08)"}]},"options":{"scales":{"y":{"min":0,"max":100}},"plugins":{"legend":{"display":False}}}}
    q = urllib.parse.quote_plus(json.dumps(cfg, separators=(",",":")))
    url = QUICKCHART_URL + "?c=" + q + "&format=png&width=800&height=300"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"QuickChart error {resp.status}")
            return await resp.read()

# ========== run_check_once returns result ==========
async def run_check_once():
    global observed_status, downtime_start
    s = get_settings()
    keyword = s["response_keyword"] or ONLINE_KEYWORD
    timeout = s["timeout_s"] or TIMEOUT_SECONDS
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(STATUS_PAGE_URL, timeout=timeout) as resp:
                text = await resp.text()
                is_online = keyword.lower() in text.lower()
    except Exception as e:
        print("Fetch error:", e)
        is_online = False

    ts_ms = int(datetime.utcnow().timestamp() * 1000)
    insert_log(ts_ms, 1 if is_online else 0)

    if is_online:
        msg = "Online"
        if observed_status != "online":
            # recovered
            if downtime_start:
                end_ts = int(datetime.utcnow().timestamp() * 1000)
                end_last_downtime(end_ts)
                downtime_secs = (end_ts - downtime_start) // 1000
                downtime_start = None
                notify_msg = f"✅ **Maxy is BACK ONLINE**\nDowntime: {downtime_secs}s\n{STATUS_PAGE_URL}"
            else:
                notify_msg = f"✅ **Maxy is ONLINE**\n{STATUS_PAGE_URL}"
            # notify owners
            asyncio.create_task(notify_owners_dm(notify_msg))
            print("Owners notified: ONLINE")
        observed_status = "online"
    else:
        msg = "Offline"
        if observed_status != "offline":
            observed_status = "offline"
            downtime_start = int(datetime.utcnow().timestamp() * 1000)
            start_downtime(downtime_start)
            notify_msg = f"🔴 **Maxy is OFFLINE**\n{STATUS_PAGE_URL}\n(Keyword `{keyword}` not found or fetch error)"
            asyncio.create_task(notify_owners_dm(notify_msg))
            print("Owners notified: OFFLINE")

    return is_online, msg, ts_ms

# monitor worker
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

# ========== CLI/UI: settings view (same as before) ==========
from discord.ui import View, Button, Modal, TextInput

class EditModal(Modal):
    def __init__(self, field_name: str, label: str, placeholder: str, style=discord.TextStyle.short):
        super().__init__(title=f"Edit {label}")
        self.field_name = field_name
        self.input = TextInput(label=label, placeholder=placeholder, style=style)
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.input.value.strip()
        if self.field_name in ("interval_min","timeout_s","auto_ping_interval_s"):
            try:
                valn = int(val)
                update_setting(self.field_name, valn)
            except:
                await interaction.response.send_message("Invalid number", ephemeral=True)
                return
        else:
            update_setting(self.field_name, val if val != "" else None)
        await interaction.response.send_message(f"Saved {self.field_name} = {val}", ephemeral=True)

class SettingsView(View):
    def __init__(self, inviter_id: int):
        super().__init__(timeout=300)
        self.inviter_id = inviter_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.inviter_id or interaction.user.id in OWNER_IDS

    @discord.ui.button(label="Edit Channel ID", style=discord.ButtonStyle.primary)
    async def edit_channel(self, button: Button, interaction: discord.Interaction):
        modal = EditModal("channel_id", "Channel ID", "123456789012345678")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Interval (min)", style=discord.ButtonStyle.secondary)
    async def edit_interval(self, button: Button, interaction: discord.Interaction):
        modal = EditModal("interval_min", "Interval (minutes)", "5")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Timeout (s)", style=discord.ButtonStyle.secondary)
    async def edit_timeout(self, button: Button, interaction: discord.Interaction):
        modal = EditModal("timeout_s", "Timeout (seconds)", "10")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Keyword", style=discord.ButtonStyle.secondary)
    async def edit_keyword(self, button: Button, interaction: discord.Interaction):
        modal = EditModal("response_keyword", "Online keyword", "Online")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Auto Ping URL", style=discord.ButtonStyle.secondary)
    async def edit_autoping(self, button: Button, interaction: discord.Interaction):
        modal = EditModal("auto_ping_url", "Auto ping URL (blank to disable)", "https://example.com/keepalive")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Show current", style=discord.ButtonStyle.success)
    async def show_current(self, button: Button, interaction: discord.Interaction):
        s = get_settings()
        lines = [f"{k}: {v}" for k,v in s.items()]
        await interaction.response.send_message("Current settings:\n" + "\n".join(lines), ephemeral=True)

# ========== slash commands: /health, /status, /settings, /forcecheck ==========
@bot.tree.command(name="health", description="Show Maxy health (chart + text).")
async def health(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        u24 = compute_uptime_percent(24)
        u7 = compute_uptime_percent(24*7)
        u30 = compute_uptime_percent(24*30)
        last_inc = get_last_downtime()
        if last_inc:
            s_ts, e_ts = last_inc
            last_inc_str = f"{datetime.fromtimestamp(s_ts/1000).strftime('%c')}" + (f" → {datetime.fromtimestamp(e_ts/1000).strftime('%c')}" if e_ts else " (ongoing)")
        else:
            last_inc_str = "No incidents"
        now = datetime.utcnow()
        labels, values = [], []
        for i in range(23, -1, -1):
            bucket_end = now - timedelta(hours=i)
            labels.append(bucket_end.strftime("%H:%M"))
            start_ms = int((bucket_end - timedelta(hours=1)).timestamp() * 1000)
            end_ms = int(bucket_end.timestamp() * 1000)
            rows = dbAll("SELECT up FROM logs WHERE ts >= ? AND ts < ?", (start_ms, end_ms))
            if not rows:
                values.append(100)
            else:
                total = len(rows); upcount = sum(r[0] for r in rows)
                values.append(round((upcount/total) * 100, 2))
        chart_png = await build_quickchart_png(labels, values)
        text = f"Maxy health summary\n24h: {u24}% • 7d: {u7}% • 30d: {u30}%\n{last_inc_str}"
        file = discord.File(io.BytesIO(chart_png), filename="health.png")
        embed = discord.Embed(title="Maxy Health", description=text)
        embed.set_image(url="attachment://health.png")
        await interaction.followup.send(embed=embed, file=file)
        await notify_owners_dm(text, file_bytes=chart_png, filename="health.png")
    except Exception as e:
        print("health error:", e)
        await interaction.followup.send("Error generating health summary")

@bot.tree.command(name="status", description="Show Maxy quick status (uptime %, last incident).")
async def status(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        u24 = compute_uptime_percent(24)
        u7 = compute_uptime_percent(24*7)
        u30 = compute_uptime_percent(24*30)
        last_inc = get_last_downtime()
        if last_inc:
            s_ts, e_ts = last_inc
            last_inc_str = f"{datetime.fromtimestamp(s_ts/1000).strftime('%c')}" + (f" → {datetime.fromtimestamp(e_ts/1000).strftime('%c')}" if e_ts else " (ongoing)")
        else:
            last_inc_str = "No incidents"
        last_row = dbAll("SELECT ts, up FROM logs ORDER BY ts DESC LIMIT 1")
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
        print("/status error:", e)
        await interaction.followup.send("Error fetching status")

@bot.tree.command(name="settings", description="Open interactive settings UI (owners only).")
async def settings(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_IDS:
        await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
        return
    s = get_settings()
    embed = discord.Embed(title="Monitor Settings", description="Edit settings using the buttons below.", color=discord.Color.blue())
    for k,v in s.items():
        embed.add_field(name=k, value=str(v), inline=False)
    view = SettingsView(inviter_id=interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# ========== NEW: /forcecheck (owner-only) ==========
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
        # show last incident summary
        last_inc = get_last_downtime()
        if last_inc:
            s_ts, e_ts = last_inc
            if e_ts:
                last_inc_str = f"{datetime.fromtimestamp(s_ts/1000).strftime('%c')} → {datetime.fromtimestamp(e_ts/1000).strftime('%c')}"
            else:
                last_inc_str = f"Ongoing since {datetime.fromtimestamp(s_ts/1000).strftime('%c')}"
        else:
            last_inc_str = "No incidents"
        embed.add_field(name="Last incident", value=last_inc_str, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        print("/forcecheck error:", e)
        await interaction.followup.send("Error running check.", ephemeral=True)

# prefix fallback commands
@bot.command(name="forcecheck")
async def forcecheck_cmd(ctx):
    if ctx.author.id not in OWNER_IDS:
        await ctx.send("You are not allowed to use this command.")
        return
    await ctx.send("Running force check...")
    try:
        is_online, msg, ts_ms = await run_check_once()
        last_check = datetime.fromtimestamp(ts_ms/1000).strftime('%c')
        color = 0x2ecc71 if is_online else 0xe74c3c
        embed = discord.Embed(title="Force Check Result", color=color)
        embed.add_field(name="Status", value=("ONLINE" if is_online else "OFFLINE"), inline=True)
        embed.add_field(name="Checked at", value=last_check, inline=True)
        last_inc = get_last_downtime()
        if last_inc:
            s_ts, e_ts = last_inc
            if e_ts:
                last_inc_str = f"{datetime.fromtimestamp(s_ts/1000).strftime('%c')} → {datetime.fromtimestamp(e_ts/1000).strftime('%c')}"
            else:
                last_inc_str = f"Ongoing since {datetime.fromtimestamp(s_ts/1000).strftime('%c')}"
        else:
            last_inc_str = "No incidents"
        embed.add_field(name="Last incident", value=last_inc_str, inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        print("!forcecheck error:", e)
        await ctx.send("Error running check.")

# ========== bot events: on_ready with forced guild sync ==========
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
                print("Guild sync failed:", e, "— falling back to global sync")
                await bot.tree.sync()
                print("Synced global commands")
        else:
            await bot.tree.sync()
            print("Synced global commands")
    except Exception as e:
        print("Slash sync final fallback failed:", e)

    # start the monitor worker
    bot.loop.create_task(monitor_worker())

# ========== start everything ==========
if __name__ == "__main__":
    # run flask in background thread for Render port detection
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(BOT_TOKEN)
