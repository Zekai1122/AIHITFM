"""
垫音（bed music）播放器

真实电台 DJ 说话时，背景有一段循环的轻音乐。
这里实现一个简单的播放器：用 afplay 后台进程，压低音量循环播放一个 mp3。

关键设计：
- 循环播放用 bash while true 包 afplay，整个进程组一起管理
- 淡出用"阶梯式多个 afplay 进程"模拟——因为 afplay 不支持启动后调音量
- 所有曾经启动的子进程都集中追踪，stop() 时强制 SIGKILL 全部
- 全局注册 atexit hook：解释器退出时兜底杀光所有还活着的 bed music 进程

用法:
    with play_bed_music(volume=0.25, fade_out=1.5) as bed:
        tts.speak("...")
    # 离开 with 块时触发 fade_out
"""

import atexit
import contextlib
import os
import random
import signal
import subprocess
import threading
import time
import weakref
from pathlib import Path
from typing import List, Optional


DEFAULT_BED_MUSIC_DIR = "audio/bed_music"
DEFAULT_VOLUME = 0.05

# 全局追踪所有活着的 BedMusicPlayer 实例（弱引用），atexit 时强制清理
_active_players: "weakref.WeakSet[BedMusicPlayer]" = weakref.WeakSet()


def _atexit_cleanup():
    """解释器退出时调用：杀光所有还活着的 BedMusicPlayer 子进程。"""
    for p in list(_active_players):
        try:
            p.stop()
        except Exception:
            pass


atexit.register(_atexit_cleanup)


def pick_random_bed_music(directory: str = DEFAULT_BED_MUSIC_DIR) -> Optional[str]:
    """从垫音目录里随机选一个音频文件。找不到返回 None。"""
    dir_path = Path(directory)
    if not dir_path.exists() or not dir_path.is_dir():
        return None
    exts = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
    candidates = [p for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if not candidates:
        return None
    return str(random.choice(candidates))


def _kill_process_hard(proc: subprocess.Popen, label: str = "afplay") -> None:
    """
    把一个子进程杀干净——先 SIGTERM 等 0.2s，没死就 SIGKILL 再 wait。
    macOS 上 afplay 对 SIGTERM 反应不稳定，必须 SIGKILL 兜底。
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=0.2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        # 实在杀不死就放弃 wait，避免阻塞主流程；进程在 atexit 时再补杀一次
        pass


class BedMusicPlayer:
    """
    用 afplay 后台进程循环播放垫音。
    
    线程模型：
    - start() 起一个 bash while-true 包 afplay 的循环进程（独立 session）
    - fade_out() 阻塞执行——把循环 kill 掉，串行启动几个递减音量的短 afplay
    - stop() 立刻杀光所有曾经启动的子进程
    """
    
    def __init__(self, audio_path: str, volume: float = DEFAULT_VOLUME):
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"垫音文件不存在: {audio_path}")
        if not 0.0 <= volume <= 1.0:
            raise ValueError(f"volume 必须在 0.0-1.0 之间，收到 {volume}")
        
        self.audio_path = audio_path
        self.volume = volume
        # 所有曾经启动的子进程都加进这里，stop() 时全部杀掉
        self._all_procs: List[subprocess.Popen] = []
        self._loop_proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._stopped = False
        _active_players.add(self)
    
    def start(self) -> None:
        with self._lock:
            if self._loop_proc is not None or self._stopped:
                return
            shell_cmd = f'while true; do afplay -v {self.volume} "{self.audio_path}"; done'
            self._loop_proc = subprocess.Popen(
                ["bash", "-c", shell_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._all_procs.append(self._loop_proc)
    
    def _kill_loop(self) -> None:
        """杀掉循环进程组。"""
        with self._lock:
            proc = self._loop_proc
            self._loop_proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                try:
                    proc.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    pass
        except (ProcessLookupError, OSError):
            pass
        except Exception as e:
            print(f"[bed_music] 停止循环时出错（忽略）: {e}")
    
    def fade_out(self, duration: float = 1.5) -> None:
        """
        阶梯式淡出。**同步阻塞**约 duration 秒，期间播一串音量递减的短 afplay。
        duration <= 0 时硬切（立刻杀循环并返回）。
        
        注意：这个方法**不**使用 daemon 线程——之前用线程导致 daemon 线程超时后
        还在后台启动 afplay，主程序退出也清理不掉。
        """
        if self._stopped:
            return
        
        # 先把循环杀掉，避免和阶梯叠加
        self._kill_loop()
        
        if duration <= 0:
            return
        
        # 阶梯：原音量 → 0
        steps = 4
        step_dur = duration / steps
        step_volumes = [self.volume * (1 - (i + 1) / steps) for i in range(steps)]
        
        for vol in step_volumes:
            if self._stopped:
                return
            if vol <= 0.001:
                break
            try:
                p = subprocess.Popen(
                    ["afplay", "-v", f"{vol:.4f}", self.audio_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                print(f"[bed_music] 启动 fade 步骤失败: {e}")
                continue
            
            with self._lock:
                self._all_procs.append(p)
            
            time.sleep(step_dur)
            _kill_process_hard(p, "fade afplay")
    
    def stop(self) -> None:
        """
        立刻硬停：杀光所有曾经启动的子进程。重复调用安全。
        """
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            procs = list(self._all_procs)
            self._all_procs.clear()
            self._loop_proc = None
        
        for proc in procs:
            if proc.poll() is not None:
                continue
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
                try:
                    proc.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    pass
            except (ProcessLookupError, OSError):
                _kill_process_hard(proc, "bed_music proc")
            except Exception:
                _kill_process_hard(proc, "bed_music proc")


@contextlib.contextmanager
def play_bed_music(
    audio_path: Optional[str] = None,
    directory: str = DEFAULT_BED_MUSIC_DIR,
    volume: float = DEFAULT_VOLUME,
    fade_in: float = 0.3,
    fade_out_on_exit: float = 0.0,
):
    """
    上下文管理器：进入时开始垫音，离开时停止。
    
    fade_out_on_exit: 离开 with 块时的淡出时长。默认 0 硬切。
        如果你希望 with 块退出时自动淡出，设成 1.5 之类的值。
        更灵活的做法是在 with 块内手动调用 bed.fade_out(1.5)。
    """
    if audio_path is None:
        audio_path = pick_random_bed_music(directory)
    
    if audio_path is None:
        print(f"[bed_music] 警告：目录 {directory} 里没找到音频文件，没有垫音")
        yield None
        return
    
    print(f"[bed_music] 垫音: {Path(audio_path).name} (音量 {volume:.0%})")
    player = BedMusicPlayer(audio_path, volume=volume)
    try:
        player.start()
        if fade_in > 0:
            time.sleep(fade_in)
        yield player
    finally:
        # 不管 with 块内做了什么（包括手动调过 fade_out），都强制 stop——
        # stop 是幂等的，已经停了再调一次也没害处。
        if fade_out_on_exit > 0 and not player._stopped:
            try:
                player.fade_out(fade_out_on_exit)
            except Exception:
                pass
        player.stop()