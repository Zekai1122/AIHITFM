"""
Apple Music 控制器
使用 AppleScript (osascript) 控制 macOS 上的 Music.app

关键能力：
- 获取当前播放曲目的元信息
- 获取当前播放进度和总时长
- 暂停 / 继续 / 下一首
- 获取"即将播放"队列里的下一首（用于提前生成文案）
"""

import subprocess
import json
from dataclasses import dataclass
from typing import Optional


_MISSING_APPLESCRIPT_VALUES = {"", "missing value"}


def _is_missing(value: str) -> bool:
    return value.strip() in _MISSING_APPLESCRIPT_VALUES


def _text_value(value: str) -> str:
    return "" if _is_missing(value) else value


def _float_value(value: str, default: float = 0.0) -> float:
    if _is_missing(value):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _optional_int_value(value: str) -> Optional[int]:
    if _is_missing(value):
        return None
    return int(value) if value.isdigit() and value != "0" else None


@dataclass
class Track:
    """歌曲元信息"""
    name: str
    artist: str
    album: str
    duration: float  # 秒
    year: Optional[int] = None
    genre: Optional[str] = None
    
    def __str__(self):
        return f"《{self.name}》- {self.artist}"


@dataclass
class PlaybackState:
    """播放状态"""
    is_playing: bool
    current_track: Optional[Track]
    position: float  # 当前播放位置（秒）
    
    @property
    def remaining(self) -> float:
        if self.current_track is None:
            return 0
        return max(0, self.current_track.duration - self.position)


def _run_osascript(script: str) -> str:
    """执行 AppleScript 并返回 stdout"""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript 失败: {result.stderr.strip()}")
    return result.stdout.strip()


class AppleMusicController:
    """Apple Music 控制接口"""
    
    def is_music_running(self) -> bool:
        """Music.app 是否在运行"""
        script = 'tell application "System Events" to (name of processes) contains "Music"'
        return _run_osascript(script) == "true"
    
    def get_playback_state(self) -> PlaybackState:
        """获取当前播放状态和曲目信息"""
        # 一次 AppleScript 调用拿齐所有信息，避免多次 IPC
        # 用分隔符返回是因为 AppleScript 输出 JSON 需要字符串转义，很烦
        script = '''
        tell application "Music"
            if player state is stopped then
                return "STOPPED"
            end if
            set isPlaying to (player state is playing)
            set t to current track
            set trackName to name of t
            set trackArtist to artist of t
            set trackAlbum to album of t
            set trackDuration to duration of t
            set trackYear to year of t
            set trackGenre to genre of t
            set pos to player position
            return (isPlaying as string) & "|||" & trackName & "|||" & trackArtist & "|||" & trackAlbum & "|||" & trackDuration & "|||" & trackYear & "|||" & trackGenre & "|||" & pos
        end tell
        '''
        output = _run_osascript(script)
        
        if output == "STOPPED":
            return PlaybackState(is_playing=False, current_track=None, position=0)
        
        parts = output.split("|||")
        if len(parts) != 8:
            raise RuntimeError(f"AppleScript 输出格式异常: {output}")
        
        is_playing = parts[0] == "true"
        track = Track(
            name=_text_value(parts[1]),
            artist=_text_value(parts[2]),
            album=_text_value(parts[3]),
            duration=_float_value(parts[4]),
            year=_optional_int_value(parts[5]),
            genre=_text_value(parts[6]) or None,
        )
        position = _float_value(parts[7])
        
        return PlaybackState(is_playing=is_playing, current_track=track, position=position)
    
    def pause(self):
        _run_osascript('tell application "Music" to pause')
    
    def play(self):
        _run_osascript('tell application "Music" to play')
    
    def next_track(self):
        _run_osascript('tell application "Music" to next track')
    
    def set_position(self, seconds: float) -> None:
        """跳到曲目内的指定秒数（player position）。常用于回到开头：set_position(0)。"""
        _run_osascript(f'tell application "Music" to set player position to {seconds}')
    
    def set_volume(self, volume: int):
        """设置 Music.app 音量（0-100）"""
        _run_osascript(f'tell application "Music" to set sound volume to {volume}')
    
    def get_volume(self) -> int:
        return int(_run_osascript('tell application "Music" to get sound volume'))
    
    def get_shuffle_enabled(self) -> bool:
        """
        Music.app 是否开了随机播放。
        
        AppleScript 接口：`shuffle enabled` 是 Music 应用级别的属性
        （不是 current playlist 的属性——后者在 iTunes 11+ 之后只读且总返回 false）。
        """
        try:
            out = _run_osascript('tell application "Music" to get shuffle enabled')
            return out.strip().lower() == "true"
        except RuntimeError:
            # 老版本或异常情况：当作 false 处理，让上层逻辑往下走（最坏就是
            # 介绍下一首时拿不到正确歌名——不算致命）
            return False
    
    def set_shuffle_enabled(self, enabled: bool) -> None:
        """关掉/打开随机播放。"""
        val = "true" if enabled else "false"
        _run_osascript(f'tell application "Music" to set shuffle enabled to {val}')
    
    def get_upcoming_track(self, debug: bool = False) -> Optional[Track]:
        """
        获取"即将播放"队列里的下一首。
        
        注意：Apple Music 的 AppleScript API 没有直接暴露 up next 队列，
        这里用的方案是获取当前播放列表里当前曲目的下一首。
        局限：
        - Apple Music 推荐电台 → 拿不到 current playlist，返回 None
        - 用户在 "Library" 视图直接双击某首歌播 → current playlist 可能是 "Music" 而不是用户预期的
        - 当前曲目正好是播放列表最后一首 → 返回 None
        
        debug=True 会打印诊断信息，方便定位"为什么 LLM 没引出下一首"。
        """
        script = '''
        tell application "Music"
            try
                set currentPlaylist to current playlist
                set currentTrackID to database ID of current track
                set allTracks to tracks of currentPlaylist
                set foundCurrent to false
                repeat with t in allTracks
                    if foundCurrent then
                        set trackName to name of t
                        set trackArtist to artist of t
                        set trackAlbum to album of t
                        set trackDuration to duration of t
                        set trackYear to year of t
                        set trackGenre to genre of t
                        return trackName & "|||" & trackArtist & "|||" & trackAlbum & "|||" & trackDuration & "|||" & trackYear & "|||" & trackGenre
                    end if
                    if (database ID of t) is currentTrackID then
                        set foundCurrent to true
                    end if
                end repeat
                return "NO_NEXT"
            on error errMsg
                return "ERROR:" & errMsg
            end try
        end tell
        '''
        output = _run_osascript(script)
        
        if output == "NO_NEXT":
            if debug:
                print("[music] get_upcoming_track: 当前曲目可能是播放列表最后一首，或 current playlist 不含当前曲目")
            return None
        if output.startswith("ERROR:"):
            if debug:
                print(f"[music] get_upcoming_track: AppleScript 报错 → {output}")
            return None
        
        parts = output.split("|||")
        if len(parts) != 6:
            return None
        
        return Track(
            name=_text_value(parts[0]),
            artist=_text_value(parts[1]),
            album=_text_value(parts[2]),
            duration=_float_value(parts[3]),
            year=_optional_int_value(parts[4]),
            genre=_text_value(parts[5]) or None,
        )