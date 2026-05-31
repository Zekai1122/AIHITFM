"""
预生成"主持人台词 + TTS"

把"LLM 生成台词 → TTS 合成为 wav"这条 pipeline 包在一个后台任务里。
和 PrebakedSpeech 的关系：
- PrebakedSpeech: 输入已知 text，后台合成 TTS
- PrebakedScript: 输入 unannounced/next_song 等结构化信息，后台先调 LLM 生成 text，再合成 TTS

使用：
    pb = PrebakedScript(
        generator=script_gen,
        tts=tts,
        unannounced=[track_a],
        next_song=track_b,
        situation="between_songs",
    )
    pb.start()
    # ... 歌还在播 ...
    result = pb.wait_and_get()   # PrebakedScriptResult(wav_path, text, announced_tracks)
    afplay(result.wav_path)
    scheduler.note_host_talk_done(announced_tracks=result.announced_tracks, ...)
    pb.cleanup()
"""

import os
import threading
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .music_controller import Track
from .script_generator import ScriptGenerator, SituationKind
from .tts import TTSBase


@dataclass
class PrebakedScriptResult:
    wav_path: str
    text: str
    announced_tracks: List[Track] = field(default_factory=list)
    llm_elapsed: float = 0.0
    tts_elapsed: float = 0.0


class PrebakedScript:
    """
    后台执行：调 LLM → 拿 text → 调 TTS → 得到 wav 文件。
    
    线程模型：
    - __init__: 不做任何 I/O
    - start(): 起后台线程，依次跑 LLM 和 TTS
    - is_done(): 任务是否结束（成功或失败）
    - is_ready(): 任务是否成功完成
    - wait_and_get(timeout): 阻塞等结果，返回 PrebakedScriptResult；失败抛异常
    - cleanup(): 删 wav 临时文件
    
    失败时 self._error 保存原始异常，wait_and_get 会重抛。
    """
    
    def __init__(
        self,
        generator: ScriptGenerator,
        tts: TTSBase,
        unannounced: List[Track],
        next_song: Optional[Track] = None,
        situation: SituationKind = "between_songs",
        extra_context: str = "",
        time_offset_seconds: float = 0.0,
    ):
        self.generator = generator
        self.tts = tts
        self.unannounced = list(unannounced)
        self.next_song = next_song
        self.situation = situation
        self.extra_context = extra_context
        self.time_offset_seconds = time_offset_seconds
        
        self._wav_path: Optional[str] = None
        self._result: Optional[PrebakedScriptResult] = None
        self._error: Optional[Exception] = None
        self._thread: Optional[threading.Thread] = None
        self._done = threading.Event()
        self._start_time: Optional[float] = None
    
    def start(self) -> None:
        """启动后台合成。非阻塞。重复调用是 no-op。"""
        if self._thread is not None:
            return
        fd, self._wav_path = tempfile.mkstemp(suffix=".wav", prefix="hitfm_script_")
        os.close(fd)
        self._start_time = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
    
    def _run(self) -> None:
        try:
            # --- 1. LLM 生成台词 ---
            llm_t0 = time.monotonic()
            print(f"[prebake_script] 启动 LLM 生成（situation={self.situation}, "
                  f"unannounced={len(self.unannounced)}, next={self.next_song}）")
            script = self.generator.generate(
                unannounced=self.unannounced,
                next_song=self.next_song,
                situation=self.situation,
                extra_context=self.extra_context,
                time_offset_seconds=self.time_offset_seconds,
            )
            llm_elapsed = time.monotonic() - llm_t0
            # print(f"[prebake_script] LLM 完成（{llm_elapsed:.1f}s）：{script.text[:60]}...")
            print(f"[prebake_script] LLM 完成（{llm_elapsed:.1f}s）：{script.text}")
            
            if not script.text.strip():
                raise RuntimeError("LLM 生成的台词为空")
            
            # --- 2. TTS 合成 ---
            tts_t0 = time.monotonic()
            self.tts.synthesize_to_file(script.text, self._wav_path)
            tts_elapsed = time.monotonic() - tts_t0
            print(f"[prebake_script] TTS 完成（{tts_elapsed:.1f}s）")
            
            self._result = PrebakedScriptResult(
                wav_path=self._wav_path,
                text=script.text,
                announced_tracks=script.announced_tracks,
                llm_elapsed=llm_elapsed,
                tts_elapsed=tts_elapsed,
            )
        except Exception as e:
            self._error = e
            print(f"[prebake_script] 失败：{e}")
        finally:
            self._done.set()
    
    def is_done(self) -> bool:
        return self._done.is_set()
    
    def is_ready(self) -> bool:
        return self._done.is_set() and self._error is None
    
    @property
    def error(self) -> Optional[Exception]:
        return self._error
    
    def wait_and_get(self, timeout: Optional[float] = None) -> PrebakedScriptResult:
        """阻塞等结果。成功返回 PrebakedScriptResult；失败重抛异常；超时抛 TimeoutError。"""
        if self._thread is None:
            raise RuntimeError("PrebakedScript 未启动，先调用 start()")
        if not self._done.wait(timeout=timeout):
            raise TimeoutError(f"预生成超过 {timeout}s 仍未完成")
        if self._error is not None:
            raise self._error
        return self._result
    
    def cleanup(self) -> None:
        if self._wav_path and os.path.exists(self._wav_path):
            try:
                os.unlink(self._wav_path)
            except Exception:
                pass
            self._wav_path = None