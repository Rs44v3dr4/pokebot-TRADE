import os
import re
import io
import asyncio
import aiohttp
import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.environ["DISCORD_TOKEN"]
PREFIX = "!"
ALLOWED_CHANNEL_ID = 1440355743999066233

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)

POKEAPI_BASE = "https://pokeapi.co/api/v2/pokemon/{name}"

CARD_W = 1000
BG_COLOR = (32, 34, 37)
BUSCO_COLOR = (130, 230, 150)
OFREZCO_COLOR = (240, 190, 90)
ICON_SIZE = 96
ICON_GAP = 16
COLS = 6


def normalize_name(raw: str):
    """Convierte 'Mimikyu Shiny' -> ('mimikyu', True)."""
    raw = raw.strip().lower()
    shiny = False
    if "shiny" in raw:
        shiny = True
        raw = raw.replace("shiny", "").strip()
    slug = re.sub(r"[^a-z0-9\-]+", "-", raw).strip("-")
    return slug, shiny


def parse_command(text: str):
    """
    Formato esperado (busco/ofrezco o abreviado b/o):
    !trade b: mew shiny, tsareena | o: garchomp shiny, mimikyu
    !trade busco: mew shiny, tsareena | ofrezco: garchomp shiny, mimikyu
    """
    text = text[len(PREFIX) + len("trade"):].strip()
    if "|" not in text:
        raise ValueError(
            "Formato: `!trade b: poke1, poke2 shiny | o: poke3, poke4` "
            "(usa `!trade help` para más detalles)"
        )
    left, right = text.split("|", 1)
    left = re.sub(r"^\s*(busco|b)\s*:?", "", left, flags=re.IGNORECASE).strip()
    right = re.sub(r"^\s*(ofrezco|of|o)\s*:?", "", right, flags=re.IGNORECASE).strip()

    busco = [normalize_name(p) for p in left.split(",") if p.strip()]
    ofrezco = [normalize_name(p) for p in right.split(",") if p.strip()]

    if not busco or not ofrezco:
        raise ValueError("Debes indicar al menos un Pokémon en cada lista.")
    return busco, ofrezco


async def resolve_default_variety(session: aiohttp.ClientSession, slug: str):
    """
    Algunos Pokémon con formas especiales (mimikyu, aegislash, zygarde, etc.)
    no existen como /pokemon/{slug} directamente. Se busca su especie y se
    obtiene el nombre real de la variedad por defecto.
    """
    species_url = f"https://pokeapi.co/api/v2/pokemon-species/{slug}"
    async with session.get(species_url) as resp:
        if resp.status != 200:
            return None
        species_data = await resp.json()

    for variety in species_data.get("varieties", []):
        if variety.get("is_default"):
            return variety["pokemon"]["name"]
    return None


async def fetch_sprite(session: aiohttp.ClientSession, slug: str, shiny: bool):
    async with session.get(POKEAPI_BASE.format(name=slug)) as resp:
        if resp.status == 404:
            resolved = await resolve_default_variety(session, slug)
            if resolved:
                async with session.get(POKEAPI_BASE.format(name=resolved)) as resp2:
                    if resp2.status != 200:
                        return None, slug
                    data = await resp2.json()
            else:
                return None, slug
        elif resp.status != 200:
            return None, slug
        else:
            data = await resp.json()

    sprite_url = (
        data["sprites"]["front_shiny"] if shiny else data["sprites"]["front_default"]
    )
    if not sprite_url:
        sprite_url = data["sprites"]["front_default"]
    if not sprite_url:
        return None, data.get("name", slug)

    async with session.get(sprite_url) as img_resp:
        if img_resp.status != 200:
            return None, data.get("name", slug)
        img_bytes = await img_resp.read()

    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    return img, data.get("name", slug)


def section_height(n_items):
    rows = max(1, (n_items + COLS - 1) // COLS)
    return 60 + rows * (ICON_SIZE + ICON_GAP)


def draw_section(card, draw, y, label, items, sprites, color, font_label, font_name):
    draw.text((30, y), label, font=font_label, fill=color)
    x, row_y = 30, y + 46
    for i, ((slug, shiny), (img, display_name)) in enumerate(zip(items, sprites)):
        if i > 0 and i % COLS == 0:
            row_y += ICON_SIZE + ICON_GAP
            x = 30
        if img is not None:
            img = img.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
            card.paste(img, (x, row_y), img)
        else:
            box = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (60, 60, 60, 255))
            card.paste(box, (x, row_y))
            draw.text((x + 6, row_y + ICON_SIZE // 2 - 8), "?", font=font_name, fill=(255, 255, 255))
        caption = display_name + (" (Shiny)" if shiny else "")
        draw.text((x, row_y + ICON_SIZE + 2), caption[:14], font=font_name, fill=(220, 220, 220))
        x += ICON_SIZE + ICON_GAP
    return row_y + ICON_SIZE + 30


async def build_trade_card(author_name, busco, ofrezco):
    async with aiohttp.ClientSession() as session:
        busco_sprites = await asyncio.gather(
            *[fetch_sprite(session, slug, shiny) for slug, shiny in busco]
        )
        ofrezco_sprites = await asyncio.gather(
            *[fetch_sprite(session, slug, shiny) for slug, shiny in ofrezco]
        )

    h_busco = section_height(len(busco))
    h_ofrezco = section_height(len(ofrezco))
    total_h = 90 + h_busco + h_ofrezco + 40

    card = Image.new("RGBA", (CARD_W, total_h), BG_COLOR + (255,))
    draw = ImageDraw.Draw(card)

    font_title = ImageFont.load_default(size=32)
    font_label = ImageFont.load_default(size=24)
    font_name = ImageFont.load_default(size=15)

    draw.text((30, 20), f"Trade de {author_name}", font=font_title, fill=(255, 255, 255))

    y = draw_section(card, draw, 80, "Busco:", busco, busco_sprites, BUSCO_COLOR, font_label, font_name)
    draw_section(card, draw, y, "Ofrezco:", ofrezco, ofrezco_sprites, OFREZCO_COLOR, font_label, font_name)

    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


@bot.event
async def on_ready():
    print(f"Conectado como {bot.user}")


HELP_TEXT = (
    "**Cómo usar `!trade`**\n"
    "```\n"
    "!trade b: poke1, poke2 shiny | o: poke3, poke4\n"
    "```\n"
    "• `b:` = lo que **buscas** (también sirve `busco:`)\n"
    "• `o:` = lo que **ofreces** (también sirve `of:` u `ofrezco:`)\n"
    "• Agrega la palabra `shiny` después del nombre si quieres esa versión\n"
    "• Separa varios Pokémon con comas\n\n"
    "**Formas regionales o especiales** (Galar, Alola, Paldea, etc.) deben "
    "escribirse con guion, tal como en Pokémon HOME/Bulbapedia:\n"
    "```\n"
    "!trade b: weezing-galar shiny | o: tauros-paldea-combat-breed, ponyta-galar\n"
    "```\n"
    "Formas como Mimikyu, Aegislash o Zygarde se detectan solas, no hace falta "
    "especificarlas."
)


@bot.command(name="tradehelp")
async def trade_help(ctx: commands.Context):
    if ctx.channel.id != ALLOWED_CHANNEL_ID:
        return
    await ctx.reply(HELP_TEXT)


@bot.command(name="trade")
async def trade(ctx: commands.Context):
    if ctx.channel.id != ALLOWED_CHANNEL_ID:
        return  # ignora silenciosamente comandos fuera del canal permitido

    if ctx.message.content.strip().lower() in (f"{PREFIX}trade", f"{PREFIX}trade help"):
        await ctx.reply(HELP_TEXT)
        return

    try:
        busco, ofrezco = parse_command(ctx.message.content)
    except ValueError as e:
        await ctx.reply(str(e))
        return

    async with ctx.typing():
        try:
            image_buf = await build_trade_card(ctx.author.display_name, busco, ofrezco)
        except Exception as e:
            await ctx.reply(f"No pude generar la imagen: {e}")
            return

    file = discord.File(image_buf, filename="trade.png")
    await ctx.send(file=file)


bot.run(TOKEN)
