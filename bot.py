import os
import re
import io
import asyncio
import aiohttp
import discord
from typing import Optional
from discord.ext import commands
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.environ["DISCORD_TOKEN"]
PREFIX = ";"
COMMAND_NAME = "cambio"
ALLOWED_CHANNEL_ID = 1440355743999066233

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)

POKEMON_NAMES = []  # se llena al conectar, usado para el autocomplete de /cambio

POKEAPI_BASE = "https://pokeapi.co/api/v2/pokemon/{name}"

CARD_W = 1000
BG_COLOR = (32, 34, 37)
BUSCO_COLOR = (130, 230, 150)
OFREZCO_COLOR = (240, 190, 90)
ICON_SIZE = 96
ICON_GAP = 40
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
    text = text[len(PREFIX) + len(COMMAND_NAME):].strip()
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


def fit_text(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…" if text else "…"


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
        fitted = fit_text(draw, caption, font_name, ICON_SIZE + ICON_GAP - 6)
        draw.text((x, row_y + ICON_SIZE + 2), fitted, font=font_name, fill=(220, 220, 220))
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

    global POKEMON_NAMES
    if not POKEMON_NAMES:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://pokeapi.co/api/v2/pokemon?limit=20000") as resp:
                    data = await resp.json()
                    POKEMON_NAMES = sorted(r["name"] for r in data["results"])
            print(f"Cargados {len(POKEMON_NAMES)} nombres de Pokémon para autocomplete")
        except Exception as e:
            print(f"No se pudo cargar la lista de nombres: {e}")

    try:
        channel = bot.get_channel(ALLOWED_CHANNEL_ID) or await bot.fetch_channel(ALLOWED_CHANNEL_ID)
        guild = channel.guild
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Sincronizados {len(synced)} comandos slash en {guild.name}")
    except Exception as e:
        print(f"No se pudieron sincronizar los comandos slash: {e}")


HELP_TEXT = (
    "**Cómo usar `;cambio`**\n"
    "```\n"
    ";cambio b: poke1, poke2 shiny | o: poke3, poke4\n"
    "```\n"
    "• `b:` = lo que **buscas** (también sirve `busco:`)\n"
    "• `o:` = lo que **ofreces** (también sirve `of:` u `ofrezco:`)\n"
    "• Agrega la palabra `shiny` después del nombre si quieres esa versión\n"
    "• Separa varios Pokémon con comas\n\n"
    "**Formas regionales o especiales** (Galar, Alola, Paldea, etc.) deben "
    "escribirse con guion, tal como en Pokémon HOME/Bulbapedia:\n"
    "```\n"
    ";cambio b: weezing-galar shiny | o: tauros-paldea-combat-breed, ponyta-galar\n"
    "```\n"
    "Formas como Mimikyu, Aegislash o Zygarde se detectan solas, no hace falta "
    "especificarlas."
)


@bot.command(name="cambioayuda")
async def trade_help(ctx: commands.Context):
    if ctx.channel.id != ALLOWED_CHANNEL_ID:
        return
    await ctx.reply(HELP_TEXT)


@bot.command(name=COMMAND_NAME)
async def trade(ctx: commands.Context):
    if ctx.channel.id != ALLOWED_CHANNEL_ID:
        return  # ignora silenciosamente comandos fuera del canal permitido

    if ctx.message.content.strip().lower() in (f"{PREFIX}{COMMAND_NAME}", f"{PREFIX}{COMMAND_NAME} ayuda"):
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
    await ctx.send(
        content=f"**{ctx.author.mention}** publicó un intercambio:",
        file=file,
    )

    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass  # el bot no tiene permiso, o el mensaje ya no existe


async def pokemon_autocomplete(interaction: discord.Interaction, current: str):
    if not POKEMON_NAMES:
        return []
    current = current.lower().strip()
    if not current:
        matches = POKEMON_NAMES[:25]
    else:
        starts = [n for n in POKEMON_NAMES if n.startswith(current)]
        contains = [n for n in POKEMON_NAMES if current in n and n not in starts]
        matches = (starts + contains)[:25]
    return [app_commands.Choice(name=n, value=n) for n in matches]


@app_commands.command(name="cambio", description="Publica un intercambio (Busco/Ofrezco) con sprites y autocomplete")
@app_commands.describe(
    busco1="Pokémon que buscas (obligatorio)",
    busco1_shiny="¿Shiny?",
    ofrezco1="Pokémon que ofreces (obligatorio)",
    ofrezco1_shiny="¿Shiny?",
    busco2="Pokémon que buscas", busco2_shiny="¿Shiny?",
    busco3="Pokémon que buscas", busco3_shiny="¿Shiny?",
    busco4="Pokémon que buscas", busco4_shiny="¿Shiny?",
    busco5="Pokémon que buscas", busco5_shiny="¿Shiny?",
    ofrezco2="Pokémon que ofreces", ofrezco2_shiny="¿Shiny?",
    ofrezco3="Pokémon que ofreces", ofrezco3_shiny="¿Shiny?",
    ofrezco4="Pokémon que ofreces", ofrezco4_shiny="¿Shiny?",
    ofrezco5="Pokémon que ofreces", ofrezco5_shiny="¿Shiny?",
)
@app_commands.autocomplete(
    busco1=pokemon_autocomplete, busco2=pokemon_autocomplete, busco3=pokemon_autocomplete,
    busco4=pokemon_autocomplete, busco5=pokemon_autocomplete,
    ofrezco1=pokemon_autocomplete, ofrezco2=pokemon_autocomplete, ofrezco3=pokemon_autocomplete,
    ofrezco4=pokemon_autocomplete, ofrezco5=pokemon_autocomplete,
)
async def cambio_slash(
    interaction: discord.Interaction,
    busco1: str,
    ofrezco1: str,
    busco1_shiny: bool = False,
    ofrezco1_shiny: bool = False,
    busco2: Optional[str] = None,
    busco2_shiny: bool = False,
    busco3: Optional[str] = None,
    busco3_shiny: bool = False,
    busco4: Optional[str] = None,
    busco4_shiny: bool = False,
    busco5: Optional[str] = None,
    busco5_shiny: bool = False,
    ofrezco2: Optional[str] = None,
    ofrezco2_shiny: bool = False,
    ofrezco3: Optional[str] = None,
    ofrezco3_shiny: bool = False,
    ofrezco4: Optional[str] = None,
    ofrezco4_shiny: bool = False,
    ofrezco5: Optional[str] = None,
    ofrezco5_shiny: bool = False,
):
    if interaction.channel_id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(
            "Este comando solo funciona en el canal de trades.", ephemeral=True
        )
        return

    busco_raw = [
        (busco1, busco1_shiny), (busco2, busco2_shiny), (busco3, busco3_shiny),
        (busco4, busco4_shiny), (busco5, busco5_shiny),
    ]
    ofrezco_raw = [
        (ofrezco1, ofrezco1_shiny), (ofrezco2, ofrezco2_shiny), (ofrezco3, ofrezco3_shiny),
        (ofrezco4, ofrezco4_shiny), (ofrezco5, ofrezco5_shiny),
    ]
    busco = [(normalize_name(n)[0], shiny) for n, shiny in busco_raw if n]
    ofrezco = [(normalize_name(n)[0], shiny) for n, shiny in ofrezco_raw if n]

    await interaction.response.defer()

    try:
        image_buf = await build_trade_card(interaction.user.display_name, busco, ofrezco)
    except Exception as e:
        await interaction.followup.send(f"No pude generar la imagen: {e}")
        return

    file = discord.File(image_buf, filename="trade.png")
    await interaction.followup.send(
        content=f"**{interaction.user.mention}** publicó un intercambio:",
        file=file,
    )


bot.tree.add_command(cambio_slash)

bot.run(TOKEN)
