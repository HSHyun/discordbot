import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

load_dotenv()  # .env 파일 로드

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!!", intents=intents)


@bot.event
async def on_ready():
    print("server ON!!")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


@bot.command()
async def 안녕(ctx):
    await ctx.send("안녕하세요! 👋")


@bot.tree.command(name="hello", description="안녕하세요! 👋")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message("안녕하세요! 👋")


bot.run(os.getenv("DISCORD_TOKEN"))
