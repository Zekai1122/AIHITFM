"""
DJ 主持人编排

核心职责：
- 监听当前播放状态
- 在合适的时机（歌曲快结束、或开场时）生成并播放文案
- 协调暂停音乐 → 播放文案 → 恢复音乐的动作
"""

import time
from typing import Optional

from .music_controller import AppleMusicController, Track, PlaybackState
from .metadata import fetch_musicbrainz_metadata
from .script_writer import ScriptWriter
from .tts import TTSBase


class DJ:
    def __init__(
        self,
        music: AppleMusicController,
        writer: ScriptWriter,
        tts: TTSBase,
        enable_metadata_fetch: bool = True,
    ):
        self.music = music
        self.writer = writer
        self.tts = tts
        self.enable_metadata_fetch = enable_metadata_fetch
        
        self._previous_track: Optional[Track] = None
        self._announced_track_id: Optional[str] = None  # 用艺人+歌名做 key，避免重复介绍
    
    @staticmethod
    def _track_key(track: Track) -> str:
        return f"{track.artist}|{track.name}"
    
    def announce(self, track: Track, is_opening: bool = False) -> None:
        """为一首歌生成并播放介绍"""
        print(f"\n[DJ] 准备介绍: {track}")
        
        # 1. 查元数据（失败不阻塞主流程）
        enriched = None
        if self.enable_metadata_fetch:
            try:
                enriched = fetch_musicbrainz_metadata(track.artist, track.album)
                print(f"[DJ] 元数据: {enriched}")
            except Exception as e:
                print(f"[DJ] 元数据获取失败（继续）: {e}")
        
        # 2. 生成文案
        script = self.writer.write_script(
            track=track,
            enriched=enriched,
            is_opening=is_opening,
            previous_track=self._previous_track,
        )
        print(f"[DJ] 文案:\n  {script}\n")
        
        # 3. 暂停音乐 → 播放文案 → 恢复
        was_playing = self.music.get_playback_state().is_playing
        if was_playing:
            self.music.pause()
        
        self.tts.synthesize_and_play(script)
        
        if was_playing:
            self.music.play()
        
        self._announced_track_id = self._track_key(track)
    
    def run_demo(self, max_songs: int = 5, poll_interval: float = 2.0):
        """
        Demo 模式：介绍接下来播放的 N 首歌然后退出。
        
        策略：
        - 启动时如果 Music.app 在播放，先暂停，介绍当前曲目，然后播放
        - 之后每当曲目切换到新的一首，就暂停并介绍
        - 介绍完 max_songs 首后退出
        """
        print(f"[DJ] Demo 启动，计划介绍 {max_songs} 首歌\n")
        
        if not self.music.is_music_running():
            print("[DJ] Music.app 没有运行，请先打开并选好播放列表/队列")
            return
        
        state = self.music.get_playback_state()
        if state.current_track is None:
            print("[DJ] 当前没有正在播放的曲目，请先在 Music.app 里点播放")
            return
        
        announced_count = 0
        
        # 第一首：不管是在播还是暂停，都从介绍它开始
        # 为了让流程自然，先暂停，介绍完再放
        if state.is_playing:
            self.music.pause()
            # 回到歌曲开头，这样介绍完从头播，体验像电台
            # （如果不想跳回开头，删掉下一行即可）
            # self.music.set_position(0)  # 留作以后扩展
        
        current = state.current_track
        self.announce(current, is_opening=True)
        self._previous_track = current
        announced_count += 1
        
        # 之后：轮询等曲目切换
        last_key = self._track_key(current)
        while announced_count < max_songs:
            time.sleep(poll_interval)
            
            try:
                state = self.music.get_playback_state()
            except RuntimeError as e:
                print(f"[DJ] 查询状态失败: {e}")
                continue
            
            if state.current_track is None:
                print("[DJ] 播放已停止，退出 demo")
                break
            
            current_key = self._track_key(state.current_track)
            if current_key != last_key:
                # 曲目换了，介绍新的这首
                # 先暂停（Apple Music 已经在播新歌了，暂停会让用户听到新歌的前几秒，
                # 为了体验更好，切歌瞬间就拦截 —— 这里接受这个小瑕疵，后面可以优化成
                # 在上一首剩余 N 秒时提前介绍下一首）
                if state.is_playing:
                    self.music.pause()
                
                self.announce(state.current_track, is_opening=False)
                self._previous_track = state.current_track
                last_key = current_key
                announced_count += 1
        
        print(f"\n[DJ] Demo 完成，共介绍了 {announced_count} 首歌")
