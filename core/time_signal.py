"""
整点报时（Time Signal）

负责：
- 计算下一个整点的精确时刻（按当前时区）
- 给出"何时该启动报时序列"的窗口判断
- 实际执行报时序列：hours/N.mp3 → pis.mp3，让 pis 末尾对齐整点

设计要点：
- 报时序列总长 = hours_duration + pis_duration ≈ 10.2 + 5.8 = 16 秒
- 序列**结束时刻**必须正好是整点
- 因此 hours/N.mp3 的**启动时刻** = 整点 - signal_total_duration
- 主循环逻辑：每段决策前查 should_arm()，true 就停下手头所有事、sleep 到启动时刻、播报时
- 已报过的小时通过内部状态记录，避免一个小时内重复触发
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .broadcast_time import BroadcastTimeProvider


# 报时音频文件夹下应有 0.mp3 - 23.mp3 + pis.mp3
DEFAULT_HOURS_DIR = "audio/hours"
PIS_FILENAME = "pis.mp3"

# pis.mp3 文件末尾可能有微小尾音（淡出/静音），让 pis 的"那声长 pi"
# 比文件末尾早结束 N 秒。如果对齐显得"晚了一点"可以加大这个值。
# 0.0 = 让文件末尾恰好落在整点。
PIS_TAIL_SECONDS = 0.0


def _get_audio_duration(path: str) -> float:
    """用 ffprobe 测 mp3 时长（秒）。失败返回 0.0。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True, timeout=5
        )
        return float(out.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
        return 0.0


@dataclass
class TimeSignalPlan:
    """已锁定的下一次报时计划"""
    target_hour: int                 # 报到几点（当前时区）
    target_iso: str                  # 整点的 ISO 时间字符串（调试用）
    start_wall_time: float           # 何时该开始播 hours/N.mp3（time.monotonic 锚）
    pis_start_wall_time: float       # 何时该接上 pis.mp3
    end_wall_time: float             # pis 文件末尾的时刻（≈ 整点）


class TimeSignalChecker:
    """
    整点报时调度器。
    
    用法：
        checker = TimeSignalChecker(time_provider, audio_dir="audio/hours")
        
        # 主循环每段决策前：
        plan = checker.should_arm()
        if plan:
            # 立刻停下手上的活，进入报时模式
            checker.execute(plan, before_signal=lambda: music.pause())
            scheduler.note_time_signal_done()
    """
    
    def __init__(
        self,
        time_provider: BroadcastTimeProvider,
        hours_dir: str = DEFAULT_HOURS_DIR,
        pis_tail_seconds: float = PIS_TAIL_SECONDS,
    ):
        self.time_provider = time_provider
        self.hours_dir = Path(hours_dir)
        self.pis_tail_seconds = pis_tail_seconds
        
        self.pis_path = self.hours_dir / PIS_FILENAME
        if not self.pis_path.exists():
            raise FileNotFoundError(f"找不到 pis 音频: {self.pis_path}")
        
        # 测量音频时长，缓存
        self.pis_duration = _get_audio_duration(str(self.pis_path))
        if self.pis_duration <= 0:
            raise RuntimeError(f"无法获取 pis 时长（ffprobe 不可用？路径: {self.pis_path}）")
        
        # 各小时报时音频的时长：可能略有差异，按需测量
        self._hour_durations: dict[int, float] = {}
        for h in range(24):
            p = self.hours_dir / f"{h}.mp3"
            if not p.exists():
                raise FileNotFoundError(f"缺少报时音频: {p}")
            self._hour_durations[h] = _get_audio_duration(str(p))
            if self._hour_durations[h] <= 0:
                raise RuntimeError(f"无法获取报时时长: {p}")
        
        # 已报过的小时记录：("yyyy-mm-dd-HH",)，避免同一个小时内重复触发
        self._reported_hours: set[str] = set()
        
        avg_hour_dur = sum(self._hour_durations.values()) / 24
        total = avg_hour_dur + self.pis_duration
        print(f"[time_signal] 报时音频加载完成：每小时报时平均 {avg_hour_dur:.1f}s，"
              f"pis {self.pis_duration:.1f}s（- 尾音 {self.pis_tail_seconds:.1f}s），"
              f"序列总长约 {total:.1f}s")
    
    # ============ 整点计算 ============
    
    def _next_top_hour_in_tz(self) -> datetime:
        """下一个整点时刻（带 tzinfo）。当前时区。"""
        bt = self.time_provider.now()
        now = bt.now  # datetime with tzinfo
        if now.minute == 0 and now.second == 0 and now.microsecond == 0:
            return now + timedelta(hours=1)
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    
    def _signal_total_duration(self, hour: int) -> float:
        """指定小时的报时序列总长 = hour_duration + pis_duration - 尾音"""
        return self._hour_durations[hour] + self.pis_duration - self.pis_tail_seconds
    
    # ============ 调度判断 ============
    
    # arm 窗口的预留余量：除了 signal_total 时长外再多盯几秒——
    # 让我们提前 arm，execute 内部 sleep 到精准 start_wall_time 才真正开播。
    # 不提前 arm 的话主循环可能错过窗口。
    ARM_WINDOW_LEAD_SECONDS = 5.0
    
    def should_arm(self, lookahead_seconds: float = 0.0) -> Optional[TimeSignalPlan]:
        """
        检查现在是否应该启动报时（注意 arm ≠ 开始播；arm 后 execute 内部
        会 sleep 到精确启动时刻才播第一个音频）。
        
        lookahead_seconds: 给主循环一个"提前量"——如果调用方知道接下来会做
            一件持续 N 秒的事（比如开始播一首歌），可以传 N。一般传 0 即可。
        
        返回 TimeSignalPlan 表示"现在该执行报时了"；返回 None 表示还不到时候。
        """
        now_dt = self.time_provider.now().now
        next_top = self._next_top_hour_in_tz()
        seconds_to_top = (next_top - now_dt).total_seconds()
        
        target_hour = next_top.hour  # 即将报的小时数
        signal_total = self._signal_total_duration(target_hour)
        
        # 已经报过这个小时？跳过
        hour_key = next_top.strftime("%Y-%m-%d-%H")
        if hour_key in self._reported_hours:
            return None
        
        # arm 窗口：距整点剩 signal_total + ARM_WINDOW_LEAD + lookahead 秒以内
        if seconds_to_top > signal_total + self.ARM_WINDOW_LEAD_SECONDS + lookahead_seconds:
            return None
        
        # 万一已经过了整点（比如刚启动或主循环卡了），过太多就标记已报、跳过
        if seconds_to_top < -signal_total:
            self._reported_hours.add(hour_key)
            return None
        
        # 构造执行计划，全部锚到 time.monotonic
        mono_now = time.monotonic()
        # 序列 end 时刻 = 整点（剩余 seconds_to_top 秒后）
        end_wall = mono_now + max(0.0, seconds_to_top - self.pis_tail_seconds)
        # pis 起播 = end - pis_duration
        pis_start = end_wall - (self.pis_duration - self.pis_tail_seconds)
        # hours 起播 = pis_start - hour_duration
        start = pis_start - self._hour_durations[target_hour]
        
        return TimeSignalPlan(
            target_hour=target_hour,
            target_iso=next_top.isoformat(),
            start_wall_time=start,
            pis_start_wall_time=pis_start,
            end_wall_time=end_wall,
        )
    
    # ============ 执行 ============
    
    def execute(
        self,
        plan: TimeSignalPlan,
        before_signal=None,
    ) -> None:
        """
        执行报时序列：阻塞到 plan.start_wall_time，依次播 hours/N.mp3 + pis.mp3。
        
        before_signal: 可选回调，在 sleep 到 start_wall_time 之前调用一次——
            主循环可以传一个 lambda 来 pause Apple Music、停止预生成 等。
        """
        if before_signal is not None:
            try:
                before_signal()
            except Exception as e:
                print(f"[time_signal] before_signal 回调失败（继续）: {e}")
        
        # sleep 到 start 时刻
        now = time.monotonic()
        wait = plan.start_wall_time - now
        if wait > 0:
            print(f"[time_signal] 等待 {wait:.1f}s 后启动报时（目标整点 {plan.target_iso}）")
            time.sleep(wait)
        else:
            # 已经到点了，直接开始（可能略过整点几秒）
            print(f"[time_signal] 启动报时（已迟 {-wait:.1f}s，目标整点 {plan.target_iso}）")
        
        # 播 hours/N.mp3
        hour_path = self.hours_dir / f"{plan.target_hour}.mp3"
        print(f"[time_signal] 播放报时正文: {hour_path.name}")
        try:
            subprocess.run(["afplay", str(hour_path)], check=True)
        except subprocess.CalledProcessError as e:
            print(f"[time_signal] 报时正文播放失败: {e}")
            # 即便正文失败也尝试播 pis，至少有个对齐声
        
        # 播 pis.mp3
        print(f"[time_signal] 播放 pis（落在整点）")
        try:
            subprocess.run(["afplay", str(self.pis_path)], check=True)
        except subprocess.CalledProcessError as e:
            print(f"[time_signal] pis 播放失败: {e}")
        
        # 标记已报
        # 注意：用 plan 的 target_hour 来构造 key，跟 should_arm 里的逻辑保持一致
        hour_key = plan.target_iso[:13].replace("T", "-")  # "YYYY-MM-DD-HH"
        self._reported_hours.add(hour_key)
        
        # 报告对齐误差（调试用）
        actual_end = time.monotonic()
        offset = actual_end - plan.end_wall_time
        print(f"[time_signal] 报时完成（pis 末尾对齐误差 {offset:+.3f}s）")