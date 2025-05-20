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
import logging
import subprocess

current_playing_proccess:subprocess.Popen = None
current_volume = 0.15
#大展鴻圖
# yt-dlp 設定
# ydl_opts = {
#     'format': 'bestaudio[ext=m4a]/bestaudio',
#     'noplaylist': True,
#     'youtube_include_dash_manifest': False,
#     'youtube_include_hls_manifest': False,
# }

# ffmpeg 設定
ffmpeg_opts = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'executable': get_ffmpeg_exe(),  
}

# 儲存每個伺服器的音樂佇列
queues = defaultdict(asyncio.Queue)

bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())

# 單曲循環狀態（每個 guild 各自獨立）
loop_flags = defaultdict(bool)

# 非同步搜尋 YouTube 音樂（只回傳歌曲資訊，不回傳 direct stream url）
def search_ytdlp_async(query, max_result=10):
    # logging.debug(f"search_ytdlp_async query: {query}")
    def ytdlp_search():
        if query.startswith("http://") or query.startswith("https://"):
            # 若是網址，直接回傳
            ydl_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio',
                'noplaylist': True,
                'youtube_include_dash_manifest': False,
                'youtube_include_hls_manifest': False,
                'extract_flat': True,
                'quiet': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
                return [{
                    'title': info.get('title'),
                    'author': info.get('uploader') or info.get('artist'),
                    'url': query,
                    'duration': info.get('duration'),
                    'thumbnail': info.get('thumbnail'),
                }]
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio',
            'noplaylist': True,
            'youtube_include_dash_manifest': False,
            'youtube_include_hls_manifest': False,
            'extract_flat': True,
            'default_search': 'ytsearch',
            'quiet': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_result}:{query}", download=False)
            # 若是 playlist/search 結果
            if 'entries' in info:
                info2 = info['entries']
            # 只回傳必要資訊
                return [{
                    'title': data.get('title'),
                    'author': data.get('uploader') or data.get('artist'),
                    'url': data.get('url'),
                    'duration': data.get('duration'),
                    'thumbnail': data.get('thumbnail'),
                } for data in info2]
    try:
        result = ytdlp_search()
        return result
    except Exception as e:
        raise Exception(f"yt-dlp 查詢失敗: {e}")

# 取得 direct stream url（僅用於播放）
async def get_direct_stream_url(webpage_url):
    ydl_opts = {
            'quiet': True,  # Suppress yt-dlp output
            'format': 'bestaudio/best',  # Ensure we only deal with audio or best formats
            # 'default_search': 'ytsearch',  # Use YouTube search
            'extract_flat': True,  # Get metadata only, no downloads
            'noplaylist': True,  # Exclude playlists
            'quiet': True,  # Suppress yt-dlp output
        }
    loop = asyncio.get_running_loop()
    def ytdlp_get_url():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
            return info.get('url')
    try:
        url = await loop.run_in_executor(None, ytdlp_get_url)
        return url
    except Exception as e:
        raise Exception(f"direct stream url 取得失敗: {e}")

# 播放下一首（根據 queue 內的 webpage_url 取得 direct stream url 再播放）
# play_next 會根據 queue 內的 webpage_url 取得 direct stream url（audio_url）
# audio_url 是直接給 FFmpeg 播放的音訊串流網址
async def play_next(guild: discord.Guild, channel: discord.TextChannel):
    global current_playing_proccess
    global current_volume
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name="休眠狀態💤"))
        return
    if vc.is_playing():
        return
    try:
        item = queues[guild.id].get_nowait()
        webpage_url = item[0] if len(item) > 0 else None
        title = item[1] if len(item) > 1 and item[1] else '未知'
        author = item[2] if len(item) > 2 and item[2] else '未知'
        if not webpage_url:
            await channel.send(f"❌ 佇列歌曲網址無效，已跳過。")
            await play_next(guild, channel)
            return
        # 取得 direct stream url（audio_url）
        try:
            audio_url = await get_direct_stream_url(webpage_url)
        except Exception as e:
            await channel.send(f"❌ 取得 direct stream url 失敗：{e}，已跳過。")
            await play_next(guild, channel)
            return
    except asyncio.QueueEmpty:
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name="休眠狀態💤"))
        return
    def after_playing(error):
        global current_playing_proccess
        current_playing_proccess.terminate()
        if loop_flags[guild.id]:
            queues[guild.id]._queue.appendleft((webpage_url, title, author))
        fut = asyncio.run_coroutine_threadsafe(play_next(guild, channel), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"播放錯誤：{e}")
    print(f"正在播放：{title} | 作者：{author} | 來源：{webpage_url}")

    ffmpeg_options = (
            f"ffmpeg -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
            f"-i {audio_url} -acodec libopus -f opus -ar 48000 -ac 2 pipe:1"
        )
    current_playing_proccess = subprocess.Popen(ffmpeg_options.split(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    source = PCMVolumeTransformer(FFmpegPCMAudio(current_playing_proccess.stdout, pipe=True), volume=current_volume)
    source.title = title
    source.author = author
    source.audio_url = audio_url  # 這裡的 audio_url 是 direct stream url
    vc.play(source, after=after_playing)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=title))
    await channel.send(f"🎶 正在播放：`{title}`")

# Bot 啟動時
@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.playing,
            name="休眠狀態💤"
        )
    )
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
        # 初始化該 guild 的 queue
        queues[interaction.guild.id] = asyncio.Queue()
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
# /play 指令：queue 只存 (webpage_url, title, author)，不存 direct stream url
# audio_url 僅在播放時才會產生
@bot.tree.command(name="play", description="播放音樂")
@app_commands.describe(song="要播放的音樂網址或關鍵字")
async def play(interaction: discord.Interaction, song: str):
    await interaction.response.defer(thinking=True)
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.followup.send("❌ 請先加入語音頻道")
        return

    # 判斷是否為 YouTube 連結
    if song.startswith("http://") or song.startswith("https://"):
        url = song
        info = search_ytdlp_async(url, 1)
        info = info[0] if info else None

    else:
        query = song
        info = search_ytdlp_async(query, 1)
        info = info[0] if info else None
        url = info.get('url', None) if info else None
    try:
        if url is None:
            await interaction.followup.send("❌ 找不到音樂")
            return
        # queue 只存 (webpage_url, title, author)
        # audio_url（direct stream url）不會在這裡產生
        queue_empty = queues[interaction.guild.id].empty()
        await queues[interaction.guild.id].put((url, info['title'], info['author']))
        await interaction.followup.send(f"🔄 已加入佇列：`{info['title']}`")
        if not vc.is_playing():
            await play_next(interaction.guild, interaction.channel)
    except Exception as e:
        logging.exception("播放音樂時發生錯誤：")
        await interaction.followup.send(f"❌ 發生錯誤：{e}")

@play.autocomplete("song")
async def song_autocomplete(ctx:discord.Interaction, current:str):
    logging.debug(f"autocomplete current: {current}")
    loop = asyncio.get_running_loop()
    # Run the blocking search in a thread
    videos = await loop.run_in_executor(None, search_ytdlp_async, current, 10)
    logging.debug(f"autocomplete videos: {videos}")
    return [
            discord.app_commands.Choice(name=video["title"], value=video["url"])
            for video in videos
        ]

# /volume 調整音量
@bot.tree.command(name="volume", description="調整播放音量（單位：百分比）")
@app_commands.describe(percent="音量百分比（例如：70 = 70%）")
async def volume(interaction: discord.Interaction, percent: int):
    global current_volume
    if percent < 0 or percent > 100:
        await interaction.response.send_message("❌ 音量請輸入 0 ~100 之間的數值", ephemeral=True)
        if percent > 100:
            await interaction.followup.send("阿你耳朵不好喔", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ 沒有正在播放的音樂", ephemeral=True)
        return

    # 若目前有 PCMVolumeTransformer，直接調整音量
    if isinstance(vc.source, discord.PCMVolumeTransformer):
        current_volume = percent / 100
        vc.source.volume = current_volume
        await interaction.response.send_message(f"🔊 音量已設定為 `{percent}%`")
    else:
        await interaction.response.send_message("⚠️ 無法調整音量", ephemeral=True)
#/current_volume 顯示當前音量
@bot.tree.command(name="current_volume", description="顯示當前音量")
async def current1_volume(interaction: discord.Interaction):
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
        # item: (audio_url, title, author)
        title = item[1] if len(item) > 1 and item[1] else '未知'
        author = item[2] if len(item) > 2 and item[2] else '未知'
        queue_list.append(f"{idx+1}. 標題: `{title}` | 作者: `{author}`")
    if now_playing:
        queue_list.insert(0, f"▶️ 正在播放: `{now_playing[1]}` | 作者: `{now_playing[2]}`")
    color = [0x10c919, 0x2d3fe0, 0x5400a3, 0xcc0621]
    result = random.choices(color, weights=[0.5, 0.5, 0.5, 0.5], k=1)[0]
    embed = discord.Embed(title="🎵 當前音樂佇列", description="\n".join(queue_list) if queue_list else "📭 音樂佇列是空的！", color=result)
    await interaction.response.send_message(embed=embed)
#/clear_queue 清空音樂佇列或刪除指定歌曲
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
#/loop 啟用單曲循環
@bot.tree.command(name="loop", description="啟用單曲循環（持續重複播放當前歌曲）")
async def loop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ 沒有正在播放的音樂", ephemeral=True)
        return
    loop_flags[interaction.guild.id] = True
    await interaction.response.send_message("🔁 單曲循環已啟用，將持續重複播放當前歌曲")
# /unloop 停用單曲循環
@bot.tree.command(name="unloop", description="停用單曲循環")
async def unloop(interaction: discord.Interaction):
    if loop_flags[interaction.guild.id]:
        loop_flags[interaction.guild.id] = False
        await interaction.response.send_message("⏹️ 單曲循環已停用")
    else:
        await interaction.response.send_message("⚠️ 單曲循環本來就未啟用", ephemeral=True)
# /pause 暫停當前歌曲
@bot.tree.command(name="pause", description="暫停當前歌曲")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client 
    if not vc or not vc.is_connected() or not vc.is_playing():
        await interaction.response.send_message("❌ 沒有正在播放的音樂", ephemeral=True)
        return
    if vc.is_paused():
        await interaction.response.send_message("⏸️ 音樂已經是暫停狀態", ephemeral=True)
        return
    vc.pause()
    await interaction.response.send_message("⏸️ 當前歌曲已暫停")

@bot.tree.command(name="resume", description="繼續播放當前歌曲")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("❌ 沒有語音連線", ephemeral=True)
        return
    if not vc.is_paused():
        await interaction.response.send_message("▶️ 音樂未處於暫停狀態", ephemeral=True)
        return
    vc.resume()
    await interaction.response.send_message("▶️ 當前歌曲已繼續播放")
@bot.tree.command(name="shuffle", description="隨機播放音樂佇列（不影響正在播放的歌曲）")
async def shuffle(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("❌ 沒有語音連線", ephemeral=True)
        return
    q = queues[interaction.guild.id]
    queue_items = list(q._queue)
    if not queue_items:
        await interaction.response.send_message("❌ 音樂佇列是空的！", ephemeral=True)
        return
    # 隨機打亂佇列（不影響正在播放的歌曲）
    random.shuffle(queue_items)
    q._queue.clear()
    for item in queue_items:
        q._queue.append(item)
    await interaction.response.send_message("🔀 已隨機打亂待播佇列")
@bot.tree.command(name="say", description="讓機器人輸出你輸入的訊息")
@app_commands.describe(message="你想讓機器人說的內容")
async def say(interaction: discord.Interaction, message: str):
    allowedd_user=683130418031362202
    if interaction.user.id != allowedd_user:
        await interaction.response.send_message("❌ 你沒有權限使用這個指令！", ephemeral=True)
        return
    await interaction.channel.send(message)


# 啟動 bot
if __name__ == "__main__":
    bot.run(TOKEN)