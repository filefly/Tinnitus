import asyncio
import disnake
import subprocess
import yt_dlp
from collections import deque
from config import config
from disnake import Embed
from disnake.ext import commands
from disnake.errors import ClientException
from random import shuffle
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

yt_dlp.utils.bug_reports_message = lambda: ""

ytdl_format_options = {
    "extractaudio": True,
    "format": "bestaudio/best",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "retries": 3,
    "cachedir": False,
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0"
}

ffmpeg_options = {
    "before_options": "-re -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rtbufsize 15M",
    "options": "-vn -threads 4"# -af loudnorm=linear=true -report"
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)


def duration_to_hms(seconds=None):
    if not seconds:
        return "0:00"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    else:
        return f"{m:d}:{s:02d}"


def get_git_info():
    try:
        github_repo_url = config.get("github_repo_url")
        log_format_str = f"I am deployed from Git commit [%h]({github_repo_url}/commit/%H) (%ci): \"%s\""
        git_log = subprocess.check_output(["git", "log", "-1", f"--pretty=format:{log_format_str}"]).decode("utf-8")
        return git_log
    except Exception as e:
        return ""


class PlayQueue(deque):
    def __init__(self):
        super().__init__()

    def is_empty(self):
        return len(self) == 0

    def add(self, obj):
        return self.append(obj)

    def length(self):
        return len(self)

    def get_next(self):
        return self.popleft()

    def shuffle(self):
        if self.is_empty():
            return self
        else:
            return shuffle(self)

    def total_duration(self):
        total_seconds = int()
        for entry in self:
            if not entry["duration"]:
                pass
            else:
                total_seconds += entry["duration"]
        return duration_to_hms(total_seconds)

    def delete(self, tracknum):
        index = tracknum - 1
        try:
            self.rotate(-index)
            deleted_item = self.popleft()
            self.rotate(index)
            return deleted_item
        except Exception as e:
            raise IndexError


class YTDLSource(disnake.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=1.0):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.media_url = data.get("url")
        self.original_url = data.get("original_url")
        self.duration = data.get("duration")
        self.uploader = data.get("uploader")
        self.thumbnail = data.get("thumbnail")

    @classmethod
    @retry(retry=retry_if_exception_type(ClientException), wait=wait_fixed(3), stop=stop_after_attempt(2))
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, lambda: ytdl.sanitize_info(ytdl.extract_info(url, download=False)))
        except Exception as e:
            raise commands.CommandError(f"YouTube says: \"{str(e).replace('ERROR: ', '')}\"")

        if 'entries' in data:
            # take first item from a playlist
            data = data["entries"][0]

        try:
            return cls(disnake.FFmpegPCMAudio(data["url"], **ffmpeg_options), data=data)
        except Exception as e:
            print(f"ffmpeg failure: {e}")
            raise commands.CommandError(f"<@{config.get('owner_id')}> FFmpeg says: \"{str(e)}\"")


class YouTubeMusicBot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.play_queue = PlayQueue()

    async def create_embed(self, title, author=Embed.Empty, image=Embed.Empty, color=0x114411, description=Embed.Empty, url=Embed.Empty, fields=Embed.Empty, footer=Embed.Empty):
        embed = disnake.Embed(title=title, color=color, url=url, description=description)
        if author:
            embed.set_author(name=author)
        if image:
            embed.set_image(url=image)
        if fields:
            for field in fields:
                embed.add_field(name=field["name"], value=field["value"])
        if footer:
            embed.set_footer(text=footer)
        return embed

    @commands.command()
    async def join(self, ctx):
        """Join the bot to the voice channel that you are currently in"""
        if ctx.author.voice:
            channel = ctx.author.voice.channel
            if ctx.voice_client is not None:
                await ctx.voice_client.move_to(channel)
            else:
                await channel.connect()
            await ctx.guild.change_voice_state(channel=channel, self_deaf=True, self_mute=False)
        
    @commands.command(aliases=["p"])
    async def play(self, ctx, *, url):
        """Add a track to the queue, or play immediately if the queue is empty"""
        async with ctx.typing():
            if ctx.voice_client and ctx.voice_client.is_playing():
                player = await YTDLSource.from_url(url, loop=self.bot.loop)
                self.play_queue.add({"ctx": ctx, "url": url, "original_url": player.original_url, "title": player.title, "duration": player.duration, "added_by": str(ctx.author).split("#")[0]})
                await self.queue(ctx)
            if self.play_queue.is_empty():
                await self.stream_from_yt(ctx, url)

    @commands.command()
    async def stop(self, ctx):
        """Stop playing and disconnect from the voice channel"""
        if not ctx.author.voice:
            raise commands.CommandError("You are not connected to a voice channel.")
        elif not ctx.voice_client:
            raise commands.CommandError("I'm not playing anything right now.")
        else:
            await ctx.voice_client.disconnect()

    @commands.command(aliases=["next"])
    async def skip(self, ctx):
        """Skip the currently playing track and go to the next track in the queue"""
        if self.play_queue.is_empty():
            if ctx.voice_client.is_playing():
                embed = await self.create_embed(title="Play Queue", description="There are no more tracks in the queue; stopping.")
                await ctx.reply(embed=embed)
                ctx.voice_client.stop()
            else:
                raise commands.CommandError("I'm not playing anything right now.")
        else:
            ctx.voice_client.stop()

    @commands.command(aliases=["q"])
    async def queue(self, ctx):
        """List the current play queue"""
        if self.play_queue.is_empty():
            embed = await self.create_embed(title="Play Queue", description="There are no tracks in the queue.")
            await ctx.reply(embed=embed)
        else:
            counter = int()
            output = str()
            for entry in self.play_queue:
                counter += 1
                output += f"{counter}.  {entry['title']} ({duration_to_hms(entry['duration'])}) [Added by {entry['added_by']}]\n"
            fields = [{"name": "Tracks", "value": self.play_queue.length()}, {"name": "Total play time", "value": self.play_queue.total_duration()}]
            embed = await self.create_embed(title="Play Queue", description=output, fields=fields)
            await ctx.reply(embed=embed)

    @commands.command(aliases=["del", "d"])
    async def delete(self, ctx, tracknum):
        """Delete an entry from the play queue"""
        if self.play_queue.is_empty():
            raise commands.CommandError("There are no tracks in the queue.")
        else:
            try:
                tracknum = int(tracknum)
                if tracknum < 1 or tracknum > self.play_queue.length():
                    raise ValueError
                self.play_queue.delete(tracknum)
                await self.queue(ctx)
            except Exception as e:
                raise commands.CommandError("Provide the number of the track you'd like to delete.")

    @commands.command(aliases=["shuf"])
    async def shuffle(self, ctx):
        """Shuffle the play queue"""
        self.play_queue.shuffle()
        await self.queue(ctx)

    @commands.command(aliases=["nuke"])
    async def clear(self, ctx):
        """Clear the play queue"""
        self.play_queue.clear()
        await self.queue(ctx)

    @commands.command(aliases=["ver", "v"])
    async def version(self, ctx):
        """Display the bot's version"""
        git_info = get_git_info()
        version_info = f"I am {config.get('bot_name')} v{config.get('bot_version')}. {git_info}"
        embed = await self.create_embed(title="Version Info", description=f"{version_info}")
        await ctx.reply(embed=embed)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Leave the voice channel if the bot is alone in it"""
        vc = member.guild.voice_client
        if vc is None:
            return
        if len(vc.channel.voice_states.keys()) == 1:
            await vc.disconnect()

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        """Return command errors, ignoring nonexistant commands"""
        if isinstance(error, commands.CommandNotFound):
            return
        embed = await self.create_embed(title="Error", color=0x441111, description=error)
        await ctx.reply(embed=embed)

    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.change_presence(activity=disnake.Game(name=f"music. Type {config.get('command_prefix')}help for help."))
        print(f"Logged in as {bot.user} ({bot.user.id})")

    @join.before_invoke
    @play.before_invoke
    @skip.before_invoke
    @queue.before_invoke
    @delete.before_invoke
    @clear.before_invoke
    async def ensure_voice(self, ctx):
        """Make sure the user invoking the command is in a voice channel"""
        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
                await ctx.guild.change_voice_state(channel=ctx.author.voice.channel, self_deaf=True, self_mute=False)
            else:
                raise commands.CommandError("You are not connected to a voice channel.")

    async def stream_from_yt(self, ctx, url):
        """Stream from a YouTube URL/search query"""
        if ctx.voice_client is None:
            return
        async with ctx.typing():
            player = await YTDLSource.from_url(url, loop=self.bot.loop)
            ctx.voice_client.play(player, after=self.done_playing)

        if self.play_queue.is_empty():
            fields = [{"name": "Track length", "value": duration_to_hms(player.duration)}]
        else:
            fields = [
                {"name": "Track length", "value": duration_to_hms(player.duration)},
                {"name": "Remaining in queue", "value": f"{self.play_queue.length()} ({self.play_queue.total_duration()})"},
                {"name": "Up next", "value": f"{self.play_queue[0]['title']} ({duration_to_hms(self.play_queue[0]['duration'])})"}
                ]
        embed = await self.create_embed(title=player.title, author=player.uploader, image=player.thumbnail, url=player.original_url, fields=fields)
        await ctx.reply(f"Now playing:", embed=embed)

    def done_playing(self, *args):
        """Play the next track in the queue, if there is one"""
        if not self.play_queue.is_empty():
            queue_entry = self.play_queue.get_next()
            play_next = self.stream_from_yt(queue_entry["ctx"], queue_entry["url"])
            fut = asyncio.run_coroutine_threadsafe(play_next, self.bot.loop)
            try:
                fut.result()
            except Exception as e:
                print(e)


bot = commands.Bot(command_prefix=commands.when_mentioned_or(config.get("command_prefix")), case_insensitive=True)
bot.add_cog(YouTubeMusicBot(bot))
bot.run(config.get("api_token"))
