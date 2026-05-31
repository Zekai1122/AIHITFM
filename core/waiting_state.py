"""
电台等待状态

场景：某个异步任务（如 TTS warmup、模型加载）正在进行中，
HITFM 用循环播放的台宣音频掩盖等待，营造"电台正在准备节目"的感觉。

工作流：
    1. 启动时给一个 readiness_check 函数（返回 True 表示任务完成）
    2. 按某种组合策略循环播放音频片段
    3. 每个音频结束时检查 readiness：
       - 没好 → 接下一个组合
       - 好了 → 接 back 音频结束等待

文件命名约定（在 audio/station_id/ 目录）：
    coming_soon_N.mp3 —— "即将开始" 类
    radio_promo_N.mp3 —— 台宣推广类
    back_N.mp3        —— "回来啦" 收尾类
"""

import subprocess
import time
import re
from pathlib import Path
from typing import Callable, List, Tuple, Optional, Iterator


# 默认目录
DEFAULT_STATION_ID_DIR = "audio/station_id"


def _natural_sort_key(s: str):
    """按文件名里的数字自然排序：coming_soon_2 在 coming_soon_10 之前"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def list_station_files(prefix: str, directory: str = DEFAULT_STATION_ID_DIR) -> List[str]:
    """
    列出目录下所有以指定 prefix 开头的音频文件，按数字自然排序。
    
    例：list_station_files("coming_soon") 返回
        ["audio/station_id/coming_soon_1.mp3",
         "audio/station_id/coming_soon_2.mp3",
         "audio/station_id/coming_soon_3.mp3"]
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        return []
    
    exts = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
    files = [
        p for p in dir_path.iterdir()
        if p.is_file()
        and p.suffix.lower() in exts
        and p.stem.startswith(prefix)
    ]
    files.sort(key=lambda p: _natural_sort_key(p.stem))
    return [str(p) for p in files]


def cyclic_combo_pairs(
    list_a: List[str],
    list_b: List[str],
) -> Iterator[Tuple[str, str]]:
    """
    生成器：按 (a1,b1), (a2,b2), (a3,b1), (a1,b2), (a2,b1), (a3,b2)... 的方式组合。
    
    规律：每次推进 (i, j) 都各自走一步取模，所以走遍所有组合需要 lcm(len_a, len_b) 步。
    
    例：list_a 有 3 个，list_b 有 2 个 → 序列长度 6:
        (0,0) (1,1) (2,0) (0,1) (1,0) (2,1)
    """
    if not list_a or not list_b:
        return
    i = 0
    j = 0
    while True:
        yield list_a[i], list_b[j]
        i = (i + 1) % len(list_a)
        j = (j + 1) % len(list_b)


class WaitingState:
    """
    电台等待状态播放器。
    
    用法:
        ws = WaitingState(
            coming_soon_files=list_station_files("coming_soon"),
            promo_files=list_station_files("radio_promo"),
            back_files=list_station_files("back"),
        )
        ws.run_until_ready(readiness_check=lambda: tts.is_warmup_done())
    """
    
    def __init__(
        self,
        coming_soon_files: List[str],
        promo_files: List[str],
        back_files: List[str],
    ):
        if not coming_soon_files:
            raise ValueError("coming_soon_files 至少要 1 个")
        if not promo_files:
            raise ValueError("promo_files 至少要 1 个")
        if not back_files:
            raise ValueError("back_files 至少要 1 个")
        
        self.coming_soon_files = coming_soon_files
        self.promo_files = promo_files
        self.back_files = back_files
    
    @classmethod
    def from_directory(cls, directory: str = DEFAULT_STATION_ID_DIR) -> "WaitingState":
        """从目录里按文件名前缀自动加载所有片段"""
        return cls(
            coming_soon_files=list_station_files("coming_soon", directory),
            promo_files=list_station_files("radio_promo", directory),
            back_files=list_station_files("back", directory),
        )
    
    def run_until_ready(
        self,
        readiness_check: Callable[[], bool],
        check_interval_during_play: float = 1.0,
        max_wait_seconds: Optional[float] = None,
        always_play_back: bool = False,
    ) -> bool:
        """
        循环播放 (coming_soon, promo) 组合，直到 readiness_check 返回 True。
        
        readiness 在每段音频自然结束时检查。如果好了，接一个 back 音频收尾。
        
        Args:
            readiness_check: 无参函数，返回 bool 表示任务是否完成
            check_interval_during_play: 播放过程中每隔多少秒检查一次（仅用于早退判断超时）
            max_wait_seconds: 最大等待时间，超过就强制退出（不接 back）
            always_play_back: True 时即使进入就已经 ready 也播 back（让主程序有一个清楚的"进入"过渡音）
        
        Returns:
            True if readiness 满足后正常退出，False if 超时
        """
        # 进入前先快速检查一次——也许根本不用等
        if readiness_check():
            print("[waiting] 进入时检查：任务已完成，无需等待")
            if always_play_back:
                self._play_back()
            return True
        
        print("[waiting] 进入电台等待状态...")
        start_time = time.monotonic()
        combo_iter = cyclic_combo_pairs(self.coming_soon_files, self.promo_files)
        
        for coming_soon, promo in combo_iter:
            # 检查超时
            if max_wait_seconds is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= max_wait_seconds:
                    print(f"[waiting] 已等待 {elapsed:.0f}s，超过最大等待 {max_wait_seconds}s，强制退出")
                    return False
            
            # 播一段 coming_soon
            print(f"[waiting] 播放: {Path(coming_soon).name}")
            self._play_with_periodic_check(coming_soon, readiness_check, check_interval_during_play)
            
            # coming_soon 播完——检查是否完成
            if readiness_check():
                print("[waiting] 任务完成，接 back 音频收尾")
                self._play_back()
                return True
            
            # 播一段 promo
            print(f"[waiting] 播放: {Path(promo).name}")
            self._play_with_periodic_check(promo, readiness_check, check_interval_during_play)
            
            # promo 播完——检查是否完成
            if readiness_check():
                print("[waiting] 任务完成，接 back 音频收尾")
                self._play_back()
                return True
            
            # 都没好，进入下一轮
        
        return False  # 不会到这里，combo_iter 是无限的
    
    def _play_with_periodic_check(
        self,
        path: str,
        readiness_check: Callable[[], bool],
        check_interval: float,
    ) -> None:
        """
        阻塞播放一个音频文件。
        
        注意：用户选了"等当前音频播完再接 back"，所以这里**不**主动打断当前音频。
        check_interval 参数保留是为了未来扩展（比如做超时检查），现在只用普通 afplay。
        """
        # 简单的阻塞播放——播完为止
        subprocess.run(["afplay", path], check=True)
    
    def _play_back(self) -> None:
        """播一个 back 音频（随机选一个）"""
        import random
        back = random.choice(self.back_files)
        print(f"[waiting] 收尾: {Path(back).name}")
        subprocess.run(["afplay", back], check=True)