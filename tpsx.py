"""
TitanyumTPSX — Hostlada Minecraft Sunucu İzleyici
===================================================
Multi-sunucu destekli, kalıcı durumlu, slash-komutlu Discord botu.

Özellikler:
  - Aynı anda birden fazla Minecraft sunucusunu izleme
  - Her sunucu için ayrı embed mesajı + otomatik güncelleme
  - Kalıcı durum (restart sonrası veriler kaybolmaz): state.json
  - Uptime yüzdesi ve son N kontrolden oluşan ping geçmişi (sparkline)
  - Slash komutları (/ekle, /kaldır, /giris, /durum, /gecmis)
  - Rol bazlı yetki kontrolü (sadece yetkili roller giriş durumunu değiştirebilir)
  - Çökme durumunda otomatik yeniden deneme + hatalı sunucu için geri çekilme (backoff)
  - Discord rate limit'e takılmamak için embed düzenlemelerinde akıllı bekleme
  - Dosyaya loglama (konsol + titanyumtpsx.log)
"""

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from mcstatus.server import JavaServer

# ---------------------------------------------------------------------------
# Loglama
# ---------------------------------------------------------------------------

logger = logging.getLogger("titanyumtpsx")
logger.setLevel(logging.INFO)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
logger.addHandler(_console_handler)

_file_handler = RotatingFileHandler("titanyumtpsx.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_file_handler)

logging.getLogger("discord").setLevel(logging.WARNING)

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"
MAX_HISTORY = 40           # sparkline + uptime hesaplaması için tutulan kontrol sayısı
EDIT_MIN_INTERVAL = 5      # aynı mesaja art arda edit atarken minimum saniye

# ---------------------------------------------------------------------------
# Config yükleme
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        logger.critical("config.json bulunamadı. Botu başlatmadan önce oluşturun.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    required = ["token", "guild_id"]
    missing = [k for k in required if k not in cfg]
    if missing:
        logger.critical(f"config.json içinde eksik alan(lar): {missing}")
        sys.exit(1)

    cfg.setdefault("check_interval", 60)
    cfg.setdefault("admin_role_ids", [])
    cfg.setdefault("servers", [])

    # Geriye dönük uyumluluk: eski tek-sunucu formatı hâlâ config'te varsa
    # otomatik olarak yeni "servers" listesine taşı.
    if not cfg["servers"] and cfg.get("server_ip"):
        cfg["servers"].append({
            "id": "default",
            "ip": cfg["server_ip"],
            "name": cfg.get("servername", cfg["server_ip"]),
            "channel_id": cfg.get("channel_id"),
        })

    if not cfg["servers"]:
        logger.critical("config.json içinde en az bir sunucu tanımlı olmalı (servers listesi).")
        sys.exit(1)

    return cfg


CONFIG = load_config()
TOKEN = CONFIG["token"]
GUILD_ID = int(CONFIG["guild_id"])
CHECK_INTERVAL = max(15, int(CONFIG["check_interval"]))  # 15sn altına düşürme, mcstatus + rate limit için güvenli değil
ADMIN_ROLE_IDS = {int(r) for r in CONFIG.get("admin_role_ids", [])}

# ---------------------------------------------------------------------------
# Kalıcı durum (restart sonrası kaybolmasın diye)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"state.json okunamadı, sıfırdan başlanıyor: {e}")
    return {}


def save_state(state: dict) -> None:
    tmp_path = STATE_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_PATH)  # atomik yazma, yarım-yazılmış dosya riski yok
    except OSError as e:
        logger.error(f"state.json yazılamadı: {e}")


_STATE = load_state()

# ---------------------------------------------------------------------------
# Sunucu takip nesnesi
# ---------------------------------------------------------------------------

@dataclass
class TrackedServer:
    id: str
    ip: str
    name: str
    channel_id: Optional[int]
    message_id: Optional[int] = None
    yurtici: bool = True
    yurtdisi: bool = True
    history: list = field(default_factory=list)   # [{"ok": bool, "latency": float|None, "ts": iso}, ...]
    last_edit_ts: float = 0.0
    consecutive_errors: int = 0

    @classmethod
    def from_config_and_state(cls, cfg_entry: dict, state: dict) -> "TrackedServer":
        sid = cfg_entry["id"]
        saved = state.get(sid, {})
        return cls(
            id=sid,
            ip=cfg_entry["ip"],
            name=cfg_entry.get("name", cfg_entry["ip"]),
            channel_id=cfg_entry.get("channel_id"),
            message_id=saved.get("message_id"),
            yurtici=saved.get("yurtici", True),
            yurtdisi=saved.get("yurtdisi", True),
            history=saved.get("history", [])[-MAX_HISTORY:],
        )

    def to_state(self) -> dict:
        return {
            "message_id": self.message_id,
            "yurtici": self.yurtici,
            "yurtdisi": self.yurtdisi,
            "history": self.history[-MAX_HISTORY:],
        }

    def record(self, ok: bool, latency: Optional[float]) -> None:
        self.history.append({
            "ok": ok,
            "latency": latency,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        self.history = self.history[-MAX_HISTORY:]

    @property
    def uptime_pct(self) -> Optional[float]:
        if not self.history:
            return None
        ok_count = sum(1 for h in self.history if h["ok"])
        return round(100 * ok_count / len(self.history), 1)

    @property
    def sparkline(self) -> str:
        if not self.history:
            return "—"
        blocks = "▁▂▃▄▅▆▇█"
        latencies = [h["latency"] for h in self.history if h["ok"] and h["latency"] is not None]
        if not latencies:
            return "".join("🟥" for h in self.history)
        lo, hi = min(latencies), max(latencies)
        span = (hi - lo) or 1.0
        chars = []
        for h in self.history:
            if not h["ok"]:
                chars.append("🟥")
                continue
            idx = int(((h["latency"] - lo) / span) * (len(blocks) - 1))
            chars.append(blocks[max(0, min(idx, len(blocks) - 1))])
        return "".join(chars)


SERVERS: dict[str, TrackedServer] = {}
for entry in CONFIG["servers"]:
    ts = TrackedServer.from_config_and_state(entry, _STATE)
    SERVERS[ts.id] = ts


def persist_all() -> None:
    save_state({sid: s.to_state() for sid, s in SERVERS.items()})


# ---------------------------------------------------------------------------
# Sunucu sorgulama
# ---------------------------------------------------------------------------

async def fetch_server_status(ip: str) -> dict:
    try:
        server = await asyncio.wait_for(asyncio.to_thread(JavaServer.lookup, ip), timeout=10)
        status = await asyncio.wait_for(server.async_status(), timeout=10)
        return {
            "online": True,
            "players_online": status.players.online,
            "players_max": status.players.max,
            "latency": round(status.latency, 1),
            "version": getattr(status.version, "name", "bilinmiyor"),
            "motd": status.description if isinstance(status.description, str) else getattr(status.description, "to_plain", lambda: None)() or "",
        }
    except asyncio.TimeoutError:
        return {"online": False, "error": "Zaman aşımı (sunucu yanıt vermedi)"}
    except Exception as e:
        return {"online": False, "error": str(e)}


def durum_etiketi(aktif: bool) -> str:
    return "✅ Aktif" if aktif else "❌ Pasif"


def create_embed(ts: TrackedServer, status: dict) -> discord.Embed:
    giris_bilgi = f"🌍 Yurt İçi: {durum_etiketi(ts.yurtici)} | Yurt Dışı: {durum_etiketi(ts.yurtdisi)}"
    uptime = ts.uptime_pct
    uptime_txt = f"{uptime}%" if uptime is not None else "veri yok"

    if status["online"]:
        motd_line = f"📝 {status['motd']}\n" if status.get("motd") else ""
        description = (
            f"🟢 **Sunucu Çevrimiçi!**\n"
            f"{motd_line}"
            f"👥 Oyuncular: {status['players_online']} / {status['players_max']}\n"
            f"📶 Gecikme: {status['latency']} ms\n"
            f"🧩 Sürüm: {status['version']}\n"
            f"📈 Uptime (son {len(ts.history)} kontrol): {uptime_txt}\n"
            f"{ts.sparkline}\n"
            f"{giris_bilgi}"
        )
        color = discord.Color.green()
    else:
        description = (
            f"🔴 **Sunucu Kapalı!**\n"
            f"Hata: `{status['error']}`\n"
            f"📈 Uptime (son {len(ts.history)} kontrol): {uptime_txt}\n"
            f"{ts.sparkline}\n"
            f"{giris_bilgi}"
        )
        color = discord.Color.red()

    embed = discord.Embed(
        title=f"{ts.name} Sunucu Durumu",
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"TitanyumTPSX • Hostlada • {ts.ip}")
    return embed


# ---------------------------------------------------------------------------
# Bot kurulumu
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = False  # slash komut kullanıyoruz, prefix komut yok — gereksiz privileged intent kapalı
bot = commands.Bot(command_prefix="!titanyum-unused!", intents=intents)


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not ADMIN_ROLE_IDS:
            # admin_role_ids hiç tanımlanmamışsa, sunucu yöneticileriyle sınırla
            return interaction.user.guild_permissions.administrator
        member_role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
        if member_role_ids & ADMIN_ROLE_IDS or interaction.user.guild_permissions.administrator:
            return True
        raise app_commands.CheckFailure("Bu komutu kullanmak için yetkiniz yok.")
    return app_commands.check(predicate)


async def cleanup_old_messages(channel: discord.abc.Messageable, keep_message_id: Optional[int]) -> None:
    """Botun daha önce attığı ve artık takip edilmeyen eski durum mesajlarını temizler."""
    try:
        async for msg in channel.history(limit=100):
            if msg.author == bot.user and msg.embeds and msg.id != keep_message_id:
                title = msg.embeds[0].title or ""
                if title.endswith("Sunucu Durumu"):
                    await msg.delete()
    except discord.Forbidden:
        logger.warning(f"Kanalda mesaj geçmişi/silme izni yok: {channel}")
    except Exception as e:
        logger.warning(f"Eski mesajlar temizlenirken hata: {e}")


async def get_or_create_message(ts: TrackedServer) -> Optional[discord.Message]:
    if ts.channel_id is None:
        return None
    channel = bot.get_channel(ts.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ts.channel_id)
        except discord.HTTPException:
            logger.error(f"[{ts.id}] Kanal bulunamadı: {ts.channel_id}")
            return None

    if ts.message_id:
        try:
            return await channel.fetch_message(ts.message_id)
        except discord.NotFound:
            ts.message_id = None
        except discord.HTTPException as e:
            logger.warning(f"[{ts.id}] Mesaj alınamadı: {e}")
            return None

    await cleanup_old_messages(channel, keep_message_id=None)
    status = await fetch_server_status(ts.ip)
    ts.record(status["online"], status.get("latency"))
    embed = create_embed(ts, status)
    message = await channel.send(embed=embed)
    ts.message_id = message.id
    persist_all()
    return message


@tasks.loop(seconds=CHECK_INTERVAL)
async def status_update_loop():
    for ts in SERVERS.values():
        if ts.channel_id is None:
            continue
        try:
            status = await fetch_server_status(ts.ip)
            ts.record(status["online"], status.get("latency"))
            ts.consecutive_errors = 0

            message = await get_or_create_message(ts)
            if message is None:
                continue

            embed = create_embed(ts, status)
            await message.edit(embed=embed)
            persist_all()
        except discord.HTTPException as e:
            ts.consecutive_errors += 1
            logger.warning(f"[{ts.id}] Discord API hatası ({ts.consecutive_errors}. ardışık): {e}")
            if e.status == 429:
                # rate limit'e takıldıysak bu turu atla, discord.py zaten retry-after'ı kendi içinde bekliyor
                await asyncio.sleep(2)
        except Exception as e:
            ts.consecutive_errors += 1
            logger.error(f"[{ts.id}] Beklenmeyen hata: {e}")
        # Sunucular arası küçük bir bekleme: Discord edit rate limitine takılmamak için
        await asyncio.sleep(1)


@status_update_loop.error
async def status_update_loop_error(exc: Exception):
    logger.error(f"status_update_loop çöktü, 30 saniye sonra yeniden başlatılacak: {exc}")
    await asyncio.sleep(30)
    if not status_update_loop.is_running():
        status_update_loop.start()


@bot.event
async def on_ready():
    logger.info(f"✅ Bot giriş yaptı: {bot.user} ({len(SERVERS)} sunucu izleniyor)")
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        logger.info("Slash komutları senkronize edildi.")
    except Exception as e:
        logger.error(f"Slash komut senkronizasyonu başarısız: {e}")

    if not status_update_loop.is_running():
        status_update_loop.start()


@bot.event
async def on_disconnect():
    logger.warning("Discord bağlantısı koptu, yeniden bağlanılıyor...")


# ---------------------------------------------------------------------------
# Slash komutlar
# ---------------------------------------------------------------------------

@bot.tree.command(name="durum", description="Tüm izlenen sunucuların anlık durumunu gösterir.")
async def durum_cmd(interaction: discord.Interaction):
    if not SERVERS:
        await interaction.response.send_message("Henüz izlenen bir sunucu yok.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    embeds = []
    for ts in SERVERS.values():
        status = await fetch_server_status(ts.ip)
        embeds.append(create_embed(ts, status))
    await interaction.followup.send(embeds=embeds[:10])  # discord limiti: mesaj başına en fazla 10 embed


@bot.tree.command(name="gecmis", description="Bir sunucunun son kontrol geçmişini ve uptime yüzdesini gösterir.")
@app_commands.describe(sunucu_id="Sunucunun kimliği (config.json'daki id)")
async def gecmis_cmd(interaction: discord.Interaction, sunucu_id: str):
    ts = SERVERS.get(sunucu_id)
    if ts is None:
        await interaction.response.send_message(
            f"`{sunucu_id}` bulunamadı. Geçerli id'ler: {', '.join(SERVERS.keys())}", ephemeral=True
        )
        return
    uptime = ts.uptime_pct
    await interaction.response.send_message(
        f"**{ts.name}**\nSon {len(ts.history)} kontrol: {ts.sparkline}\nUptime: {uptime if uptime is not None else 'veri yok'}%"
    )


@bot.tree.command(name="giris", description="Yurt içi/yurt dışı giriş durumunu değiştirir.")
@app_commands.describe(sunucu_id="Sunucu kimliği", tip="yurtici veya yurtdisi", durum="aktif veya pasif")
@app_commands.choices(
    tip=[app_commands.Choice(name="Yurt İçi", value="yurtici"), app_commands.Choice(name="Yurt Dışı", value="yurtdisi")],
    durum=[app_commands.Choice(name="Aktif", value="aktif"), app_commands.Choice(name="Pasif", value="pasif")],
)
@is_admin()
async def giris_cmd(interaction: discord.Interaction, sunucu_id: str, tip: app_commands.Choice[str], durum: app_commands.Choice[str]):
    ts = SERVERS.get(sunucu_id)
    if ts is None:
        await interaction.response.send_message(
            f"`{sunucu_id}` bulunamadı. Geçerli id'ler: {', '.join(SERVERS.keys())}", ephemeral=True
        )
        return

    setattr(ts, tip.value, durum.value == "aktif")
    persist_all()
    await interaction.response.send_message(f"✅ `{ts.name}` için `{tip.name}` durumu `{durum.name}` olarak ayarlandı.")

    message = await get_or_create_message(ts)
    if message:
        status = await fetch_server_status(ts.ip)
        await message.edit(embed=create_embed(ts, status))


@bot.tree.command(name="ekle", description="İzlemeye yeni bir Minecraft sunucusu ekler.")
@app_commands.describe(sunucu_id="Benzersiz kısa kimlik", ip="Sunucu adresi (ör. play.example.com:25565)", isim="Görünen ad", kanal="Durum mesajının gönderileceği kanal")
@is_admin()
async def ekle_cmd(interaction: discord.Interaction, sunucu_id: str, ip: str, isim: str, kanal: discord.TextChannel):
    if sunucu_id in SERVERS:
        await interaction.response.send_message(f"`{sunucu_id}` zaten mevcut.", ephemeral=True)
        return
    SERVERS[sunucu_id] = TrackedServer(id=sunucu_id, ip=ip, name=isim, channel_id=kanal.id)
    persist_all()
    await interaction.response.send_message(f"✅ `{isim}` ({ip}) izlemeye eklendi → {kanal.mention}")
    await get_or_create_message(SERVERS[sunucu_id])


@bot.tree.command(name="kaldir", description="İzlenen bir sunucuyu kaldırır.")
@app_commands.describe(sunucu_id="Kaldırılacak sunucunun kimliği")
@is_admin()
async def kaldir_cmd(interaction: discord.Interaction, sunucu_id: str):
    ts = SERVERS.pop(sunucu_id, None)
    if ts is None:
        await interaction.response.send_message(f"`{sunucu_id}` bulunamadı.", ephemeral=True)
        return
    if ts.channel_id and ts.message_id:
        channel = bot.get_channel(ts.channel_id)
        if channel:
            try:
                msg = await channel.fetch_message(ts.message_id)
                await msg.delete()
            except discord.HTTPException:
                pass
    persist_all()
    await interaction.response.send_message(f"🗑️ `{ts.name}` izlemeden kaldırıldı.")


@giris_cmd.error
@ekle_cmd.error
@kaldir_cmd.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "❌ Bu komutu kullanmak için yetkiniz yok."
    else:
        logger.error(f"Komut hatası: {error}")
        msg = "❌ Komut çalıştırılırken bir hata oluştu."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# Giriş noktası
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("🔧 TitanyumTPSX başlatılıyor...")
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        logger.critical("Geçersiz token. config.json içindeki 'token' değerini kontrol edin.")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Bot kapatılıyor...")
