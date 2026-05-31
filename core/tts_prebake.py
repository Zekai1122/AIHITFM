"""
TTS 预生成辅助

场景：歌曲还在播放时，后台合成下一段过场白的 TTS 到临时文件。
等歌曲结束时，直接 afplay 这个文件，不用等合成。

用法:
    from core.tts_prebake import PrebakedSpeech
    
    # 在歌1还在播时启动
    prebaked = PrebakedSpeech(tts, "过场白文字...")
    prebaked.start()
    
    # 歌1结束后
    wav_path = prebaked.wait_and_get_path()  # 如果已完成就立刻返回
    subprocess.run(["afplay", wav_path])
    prebaked.cleanup()
"""

import threading
import tempfile
import os
import time
from typing import Optional

from .tts import TTSBase


class PrebakedSpeech:
    """
    后台合成一段 TTS 到临时 wav 文件。
    
    线程模型：
    - __init__: 不做合成
    - start(): 起后台线程开始合成
    - wait_and_get_path(timeout): 阻塞等合成完成，返回 wav 路径
    - cleanup(): 删除临时文件
    """
    
    def __init__(self, tts: TTSBase, text: str):
        self.tts = tts
        self.text = text
        self._wav_path: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._done = threading.Event()
        self._error: Optional[Exception] = None
        self._start_time: Optional[float] = None
    
    def start(self) -> None:
        """启动后台合成。非阻塞。"""
        if self._thread is not None:
            return
        
        # 提前创建临时文件路径
        fd, self._wav_path = tempfile.mkstemp(suffix=".wav", prefix="hitfm_prebake_")
        os.close(fd)
        
        self._start_time = time.monotonic()
        self._thread = threading.Thread(target=self._synthesize, daemon=True)
        self._thread.start()
    
    def _synthesize(self):
        try:
            self.tts.synthesize_to_file(self.text, self._wav_path)
        except Exception as e:
            self._error = e
            # 立刻打印错误，不等到调用方 wait 时才发现
            print(f"[prebake] 合成失败: {e}")
        finally:
            self._done.set()
    
    def is_done(self) -> bool:
        """合成线程是否已结束（不区分成功/失败）"""
        return self._done.is_set()
    
    def is_ready(self) -> bool:
        """合成是否已成功完成（失败返回 False）"""
        return self._done.is_set() and self._error is None
    
    @property
    def error(self) -> Optional[Exception]:
        """如果合成失败，返回异常对象；否则 None"""
        return self._error
    
    def wait_and_get_path(self, timeout: Optional[float] = None) -> str:
        """
        阻塞等合成完成，返回 wav 路径。
        
        如果合成出错，抛出原始异常。
        timeout 为 None 时无限等待；超时抛 TimeoutError。
        """
        if self._thread is None:
            raise RuntimeError("PrebakedSpeech 未启动，先调用 start()")
        
        if not self._done.wait(timeout=timeout):
            raise TimeoutError(f"TTS 合成超过 {timeout}s 仍未完成")
        
        if self._error is not None:
            raise self._error
        
        elapsed = time.monotonic() - self._start_time
        print(f"[prebake] TTS 合成完成，耗时 {elapsed:.1f}s")
        return self._wav_path
    
    def cleanup(self) -> None:
        """删除临时 wav 文件"""
        if self._wav_path and os.path.exists(self._wav_path):
            try:
                os.unlink(self._wav_path)
            except Exception:
                pass
            self._wav_path = None