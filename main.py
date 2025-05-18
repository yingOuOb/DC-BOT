import discord
from discord.ext import commands
import asyncio
from config import TOKEN,YTDLP_PATH
from discord import app_commands, FFmpegPCMAudio, PCMVolumeTransformer
import yt_dlp
from collections import defaultdict
import concurrent.futures
import json
from imageio_ffmpeg import get_ffmpeg_exe
import random 


# yt-dlp 設定
ydl_opts = {
    'format': 'bestaudio[ext=m4a]/bestaudio',
    'noplaylist': True,
    'youtube_include_dash_manifest': False,
    'youtube_include_hls_manifest': False,
}

# ffmpeg 設定
ffmpeg_opts = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'executable': get_ffmpeg_exe(),  
}

# 儲存每個伺服器的音樂佇列
queues = defaultdict(asyncio.Queue)

bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())

# 全域 ThreadPoolExecutor，避免每次查詢都新建
thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# 非同步搜尋 YouTube 音樂（使用 subprocess，不佔用 thread pool）
async def search_ytdlp_async(query, ydl_opts):
    """
    使用 asyncio subprocess 執行 yt-dlp 查詢 YouTube 音樂，
    完全不佔用 thread pool，查詢與播放互不干擾。
    """
    # 構建 yt-dlp 命令列
    ytdlp_cmd = [
        YTDLP_PATH, '--no-playlist', '--print-json', '-f', ydl_opts['format'], '--skip-download', query
    ]
    # 啟動 subprocess
    proc = await asyncio.create_subprocess_exec(
        *ytdlp_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise Exception(f"yt-dlp error: {stderr.decode(errors='ignore')}")
    # 解析 JSON 結果
    try:
        result = json.loads(stdout.decode())
        # 模擬原本 entries 結構
        return {"entries": [result]}
    except Exception as e:
        raise Exception(f"yt-dlp output parse error: {e}\nRaw: {stdout.decode(errors='ignore')}")

# 播放下一首（將 title/author 設為 source 屬性，方便 /queue 顯示）
async def play_next(guild: discord.Guild, channel: discord.TextChannel):
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return

    if vc.is_playing():
        return

    try:
        # 嘗試取得 (audio_url, title, author)，若無則補 '未知'
        item = queues[guild.id].get_nowait()
        audio_url = item[0] if len(item) > 0 else 'N/A'
        title = item[1] if len(item) > 1 and item[1] else '未知'
        author = item[2] if len(item) > 2 and item[2] else '未知'
    except asyncio.QueueEmpty:
        return

    def after_playing(error):
        fut = asyncio.run_coroutine_threadsafe(play_next(guild, channel), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"播放錯誤：{e}")

    source = PCMVolumeTransformer(FFmpegPCMAudio(audio_url, **ffmpeg_opts), volume=0.4)
    # 將 title/author/audio_url 屬性掛到 source 物件上，方便 /queue 顯示
    source.title = title
    source.author = author
    source.audio_url = audio_url
    vc.play(source, after=after_playing)
    await channel.send(f"🎶 正在播放：`{title}`")

# Bot 啟動時
@bot.event
async def on_ready():
    slash_commands = await bot.tree.sync()
    print("\n".join(f'已註冊 {sc.name} 指令' for sc in slash_commands))
    print(f"{bot.user} 已登入")

# 防嘴砲功能
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if "傻逼" in message.content:
        await message.reply(f"{message.author.mention} 你才傻逼呢！")
        await asyncio.sleep(3)
    await bot.process_commands(message)

# /join 加入語音
@bot.tree.command(name="join", description="加入或移動到指定語音頻道")
@app_commands.describe(channl="要加入的語音頻道")
async def join(interaction: discord.Interaction, channl: discord.VoiceChannel):
    vc = interaction.guild.voice_client
    if vc:
        if vc.channel.id == channl.id:
            await interaction.response.send_message(f"✅ 我已在 `{channl.name}`", ephemeral=True)
        else:
            await vc.move_to(channl)
            await interaction.response.send_message(f"🔄 已移動到 `{channl.name}`", ephemeral=True)
    else:
        await channl.connect()
        await interaction.response.send_message(f"✅ 已加入 `{channl.name}`")

# /leave 離開語音
@bot.tree.command(name="leave", description="離開語音頻道")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("👋 已離開語音頻道")
    else:
        await interaction.response.send_message("❌ 我不在語音頻道")

# /play 播放歌曲（加入佇列）
@bot.tree.command(name="play", description="播放音樂")
@app_commands.describe(song="要播放的音樂網址或關鍵字")
async def play(interaction: discord.Interaction, song: str):
    await interaction.response.defer(thinking=True)
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.followup.send("❌ 請先加入語音頻道")
        return

    query = "ytsearch1:" + song
    try:
        result = await search_ytdlp_async(query, ydl_opts)
        tracks = result.get("entries", [])
        if not tracks:
            await interaction.followup.send("❌ 找不到音樂")
            return

        track = tracks[0]
        audio_url = track.get("url")
        title = track.get("title")
        author = track.get("uploader") or track.get("artist") or None

        queue_empty = queues[interaction.guild.id].empty()
        await queues[interaction.guild.id].put((audio_url, title, author))
        await interaction.followup.send(f"🔄 已加入佇列：`{title}`")

        if not vc.is_playing():
            await play_next(interaction.guild, interaction.channel)

    except Exception as e:
        await interaction.followup.send(f"❌ 發生錯誤：{e}")

# /volume 調整音量
@bot.tree.command(name="volume", description="調整播放音量（單位：百分比）")
@app_commands.describe(percent="音量百分比（例如：70 = 70%）")
async def volume(interaction: discord.Interaction, percent: int):
    if percent < 0 or percent > 100:
        await interaction.response.send_message("❌ 音量請輸入 0 ~100 之間的數值", ephemeral=True)
        if percent >100:
            await interaction.followup.send("阿你耳朵不好喔", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ 沒有正在播放的音樂", ephemeral=True)
        return

    if isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = percent / 100
        await interaction.response.send_message(f"🔊 音量已設定為 `{percent}%`")
    else:
        await interaction.response.send_message("⚠️ 無法調整音量", ephemeral=True)
#/current_volume 顯示當前音量
@bot.tree.command(name="current_volume", description="顯示當前音量")
async def current_volume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ 沒有正在播放的音樂", ephemeral=True)
        return

    if isinstance(vc.source, discord.PCMVolumeTransformer):
        volume = int(vc.source.volume * 100)
        await interaction.response.send_message(f"🔊 當前音量為 `{volume}%`")
    else:
        await interaction.response.send_message("⚠️ 無法獲取音量", ephemeral=True)
# /skip 跳過當前歌曲
@bot.tree.command(name="skip", description="跳過當前歌曲")
async def skip(interaction:discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ 沒有正在播放的音樂", ephemeral=True)
        return
    vc.stop()
    await interaction.response.send_message("⏭️ 已跳過當前歌曲")
@bot.tree.command(name="queue", description="顯示當前佇列")
async def queue(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    now_playing = None
    # 僅當有正在播放時才顯示
    if vc and vc.is_connected() and vc.is_playing() and hasattr(vc, 'source') and hasattr(vc.source, 'title') and getattr(vc.source, 'title', None):
        now_playing = (getattr(vc.source, 'audio_url', 'N/A'), getattr(vc.source, 'title', '未知'), getattr(vc.source, 'author', '未知'))
    else:
        now_playing = None
    # 直接從 queue 物件取得所有待播歌曲
    queue_list = []
    for idx, item in enumerate(list(queues[interaction.guild.id]._queue)):
        queue_list.append(f"{idx+1}. 標題: `{item[1]}`\n   作者: `{item[2] if len(item) > 2 and item[2] else '未知'}`")
    if now_playing:
        queue_list.insert(0, f"▶️ 正在播放: `{now_playing[1]}`\n   作者: `{now_playing[2]}`")
    color = [0x10c919, 0x2d3fe0, 0x5400a3, 0xcc0621]
    result = random.choices(color, weights=[0.5, 0.5, 0.5, 0.5], k=1)[0]
    embed = discord.Embed(title="🎵 當前音樂佇列", description="\n".join(queue_list) if queue_list else "📭 音樂佇列是空的！", color=result)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="clear_queue", description="清空音樂佇列或刪除指定歌曲")
@app_commands.describe(index="要刪除的歌曲編號（留空則清空全部）")
async def clear_queue(interaction: discord.Interaction, index: int = 0):
    q = queues[interaction.guild.id]
    # 取得 queue 內容
    queue_items = list(q._queue)
    if index is None or index == 0:
        # 清空全部
        q._queue.clear()
        await interaction.response.send_message("🗑️ 已清空音樂佇列！")
    elif 1 <= index <= len(queue_items):
        removed = queue_items.pop(index - 1) # 取得要刪除的歌曲
        # 重新建立 queue
        q._queue.clear()
        for item in queue_items:
            q._queue.append(item)
        await interaction.response.send_message(f"🗑️ 已刪除第 {index} 首歌：`{removed[1]}`")
    else:
        await interaction.response.send_message("❌ 無效的歌曲編號！", ephemeral=True)

# 啟動 bot
if __name__ == "__main__":
    bot.run(TOKEN)
