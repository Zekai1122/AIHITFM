"""
Scheduler: 电台调度状态机

职责：根据当前状态决定下一段播什么。**不做 IO，不知道具体音频路径**。

状态变量：
- consecutive_songs: 当前已经连续播了多少首歌（不算 station_id）
- last_segment_kind: 上一段播的是什么（用于决定下一段的转移）
- last_break_kind: 上一次"非歌曲间隔"是 host_talk 还是 station_id —— 用于交替
- unannounced: 引用一个 UnannouncedSongs 实例

决策规则（来自项目设计文档）：
- 开场：station_id + 第一首歌（不再用 host_talk 开场）
- 两首歌之间的"间隔"在 host_talk 和 station_id 之间交替：
    STATION_ID(开场) → SONG → HOST_TALK → SONG → STATION_ID → SONG → HOST_TALK → ...
- 每次 host_talk 都会"引出下一首歌"——next_song_to_introduce 被填入，
  对应的歌曲在 note_song_finished 时应传 was_announced_before=True
- 接近整点时不启动 host_talk（top_hour_guard_seconds 窗口）；
  在保护窗口里轮到 host_talk 时，降级成 station_id
- 主持人台词不会被报时打断（决策时就规避）

抢断（time signal）的实际执行不在 Scheduler 里，由主循环 + TimeSignalChecker 协作。
Scheduler 提供 note_time_signal_done() 让主循环告诉它"刚报完时"，scheduler 进入相应状态。
"""

import time
from dataclasses import dataclass, field
from typing import List, Literal, Optional
from datetime import datetime, timedelta

from .music_controller import Track
from .unannounced import UnannouncedSongs


SegmentKind = Literal["song", "station_id", "host_talk"]
SituationKind = Literal["opening", "between_songs", "after_time_signal"]


@dataclass
class SegmentDecision:
    """Scheduler 输出的下一段决策"""
    kind: SegmentKind
    # host_talk 的辅助信息（kind == "host_talk" 时有效）
    situation: SituationKind = "between_songs"
    # 期望介绍的歌（来自 unannouncedSongs）
    songs_to_announce: List[Track] = field(default_factory=list)
    # 即将播放的下一首歌（如果 host_talk 后接歌）
    next_song_to_introduce: Optional[Track] = None
    # 调试用的决策原因
    reason: str = ""
    
    def __repr__(self):
        if self.kind == "host_talk":
            return (
                f"SegmentDecision(host_talk, situation={self.situation}, "
                f"announce={len(self.songs_to_announce)}, "
                f"next={self.next_song_to_introduce}, reason={self.reason!r})"
            )
        return f"SegmentDecision({self.kind}, reason={self.reason!r})"


class Scheduler:
    """
    电台调度状态机。
    
    主循环每次播一段前调一次 decide_next()，根据返回执行对应动作；
    动作执行后调用 note_xxx_done() 通知 Scheduler 状态变化。
    """
    
    def __init__(
        self,
        unannounced: UnannouncedSongs,
        max_consecutive_songs: int = 2,
        top_hour_guard_seconds: int = 120,
        expected_host_talk_duration_seconds: int = 30,
    ):
        """
        max_consecutive_songs: 默认连播几首歌就要 host_talk（兜底，但正常流程是交替）
        top_hour_guard_seconds: 距整点多少秒内不启动 host_talk
        expected_host_talk_duration_seconds: 估算 host_talk 大约多久，用于检查"会不会撞到整点"
        """
        self.unannounced = unannounced
        self.max_consecutive_songs = max_consecutive_songs
        self.top_hour_guard_seconds = top_hour_guard_seconds
        self.expected_host_talk_duration = expected_host_talk_duration_seconds
        
        # 内部状态
        self._consecutive_songs = 0           # 连续播了几首歌（不含 station_id）
        self._last_kind: Optional[SegmentKind] = None
        self._has_opened = False              # 是否已经开过场（开场段播完后置 true）
        # 最近一次"非歌曲间隔"是什么。开场用 station_id 起头，所以初始视为 station_id —— 
        # 第一首歌之后的间隔就会是 host_talk（与开场交替）。
        self._last_break_kind: SegmentKind = "station_id"
        # 上一段 host_talk 里"引出"的下一首歌；主循环播这首歌前查询并消费
        self._pending_announced_next: Optional[Track] = None
    
    # =================== 决策入口 ===================
    
    def decide_next(
        self,
        next_song_preview: Optional[Track] = None,
        now: Optional[datetime] = None,
    ) -> SegmentDecision:
        """
        决定下一段播什么。
        """
        if now is None:
            now = datetime.now()
        
        # ========== 开场 ==========
        # 用 station_id 开场（短，不挑整点），紧跟一首歌
        if not self._has_opened:
            return SegmentDecision(
                kind="station_id",
                situation="opening",
                reason="开场 station_id",
            )
        
        # ========== 先按"上一段是什么"做基础决策 ==========
        # 上一段是歌 → 接 host_talk 或 station_id（与上一次间隔交替）
        # 上一段是 station_id 或 host_talk → 接歌
        
        if self._last_kind == "song":
            # 决定这次间隔放什么：和上一次间隔类型相反
            want_host_talk = (self._last_break_kind == "station_id")
            
            if want_host_talk:
                # 想放 host_talk，但要检查整点保护窗口
                if self._is_in_top_hour_guard(now):
                    return SegmentDecision(
                        kind="station_id",
                        reason=f"本该 HOST_TALK，但距整点 < {self.top_hour_guard_seconds}s，降级为 STATION_ID",
                    )
                return SegmentDecision(
                    kind="host_talk",
                    situation="between_songs",
                    songs_to_announce=self.unannounced.all(),
                    next_song_to_introduce=next_song_preview,
                    reason="歌后接 HOST_TALK",
                )
            else:
                return SegmentDecision(
                    kind="station_id",
                    reason="歌曲中间接STATION_ID",
                )
        
        # 上一段是 station_id 或 host_talk（或刚开始 _last_kind=None）→ 接歌
        return SegmentDecision(
            kind="song",
            reason="接歌"
        )
    
    # =================== 状态更新 hook ===================
    
    def note_song_finished(self, track: Track, was_announced_before: bool, was_interrupted: bool = False) -> None:
        """
        通知 scheduler：一首歌播完了。
        
        was_announced_before: 这首歌在播放前是否被主持人提前介绍过
        was_interrupted: 是否被整点报时打断（被打断也要加入 unannouncedSongs）
        """
        self._consecutive_songs += 1
        self._last_kind = "song"
        
        # 按规则：不论被中断与否，只要"还没被介绍过"就加入 unannounced
        if not was_announced_before:
            self.unannounced.add(track)
    
    def note_station_id_done(self) -> None:
        """station_id 播完。注意不重置 consecutive_songs 计数。
        
        开场的 station_id 是特例：它标志"开场刚结束，进入正常循环的起点"，
        所以 _last_kind 留作 None（而不是 "station_id"），表达"零状态"。
        _last_break_kind 仍然记为 station_id，让下一次间隔走到 host_talk（与开场交替）。
        """
        was_opening = not self._has_opened
        self._last_break_kind = "station_id"
        if was_opening:
            self._has_opened = True
            self._last_kind = None
        else:
            self._last_kind = "station_id"
    
    def note_host_talk_done(
        self,
        announced_tracks: List[Track],
        introduced_next: Optional[Track] = None,
    ) -> None:
        """
        host_talk 播完。从 unannounced 移除已介绍的歌曲，重置连播计数。
        
        introduced_next: host_talk 里"引出"的下一首歌。它之后播完时，
                         was_announced_before 应为 True，因此不应加入 unannounced。
                         scheduler 暂存它，等主循环用 consume_pending_announced_next 取回。
        """
        self.unannounced.remove_many(announced_tracks)
        self._consecutive_songs = 0
        self._last_kind = "host_talk"
        self._last_break_kind = "host_talk"
        self._has_opened = True
        self._pending_announced_next = introduced_next
    
    def consume_pending_announced_next(self, track: Track) -> bool:
        """
        查询并消费：即将播放的这首歌，是否在上一段 host_talk 里被预先介绍过。
        主循环在 note_song_finished 之前调用，把结果作为 was_announced_before 传入。
        """
        pending = self._pending_announced_next
        if pending is not None and pending.name == track.name and pending.artist == track.artist:
            self._pending_announced_next = None
            return True
        return False
    
    def note_time_signal_done(self) -> None:
        """
        整点报时播完。按规则：
        - 报时后接 station_id → back → 下一首歌
        - 重置连播计数（视为"清零，从新的一段开始"）
        
        实际的 station_id 和 back 由主循环顺序播放，scheduler 这里只更新状态。
        """
        self._consecutive_songs = 0
        # 把状态设成 "after_time_signal" 的语义——下一段 host_talk 会用 after_time_signal situation
        # 但我们不专门加一个状态枚举值，靠 _last_kind = None + _has_opened = True 区分
        self._last_kind = None
    
    # =================== 辅助 ===================
    
    def _is_in_top_hour_guard(self, now: datetime) -> bool:
        """
        距下一个整点是否在保护窗口内。
        
        要考虑 host_talk 本身的预计时长——如果 host_talk 会撞到报时区，也算 in_guard。
        """
        next_top = self._next_top_hour(now)
        seconds_to_top = (next_top - now).total_seconds()
        # 留出 host_talk 的预计时长 + 保护窗口
        threshold = self.top_hour_guard_seconds + self.expected_host_talk_duration
        return seconds_to_top < threshold
    
    @staticmethod
    def _next_top_hour(now: datetime) -> datetime:
        """下一个整点的精确时刻"""
        if now.minute == 0 and now.second == 0 and now.microsecond == 0:
            # 正好整点，下一个就是 1 小时后
            return now + timedelta(hours=1)
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    
    # =================== 调试/查询 ===================
    
    def state_summary(self) -> str:
        return (
            f"Scheduler(consecutive_songs={self._consecutive_songs}, "
            f"last={self._last_kind}, opened={self._has_opened}, "
            f"unannounced={len(self.unannounced)})"
        )
    
    def predict_next_break_after_song(self) -> SegmentKind:
        """
        预测：当前正要播的（或刚开始播的）这首歌结束后，下一段间隔会是什么。
        
        主循环用这个判断：歌刚开始时要不要后台启动 PrebakedScript。
        - 如果预测是 "host_talk" → 启动后台 LLM+TTS 预生成
        - 如果预测是 "station_id" → 不需要预生成，到时候随机挑个 mp3 即可
        
        逻辑：与"上一次间隔"交替。所以下一次间隔 = (_last_break_kind 的反面)。
        """
        return "host_talk" if self._last_break_kind == "station_id" else "station_id"