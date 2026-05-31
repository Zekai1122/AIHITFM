"""
UnannouncedSongs 队列

维护"已经完整播放过、但主持人还没介绍过"的歌曲列表。

规则（来自项目设计文档）：
- 歌曲开始播放但没有被主持人介绍过 → 加入
- 主持人台词介绍了某首 unannounced 歌曲 → 移除
- 如果歌曲在播放前就被介绍过（"接下来要听到的是..."），完整播放后不加入

注意：这里只是数据结构，不做"识别 LLM 输出里有没有提到某首歌"这种 NLP 工作。
那个判断由 Scheduler / ScriptGenerator 在生成台词时主动告知"我介绍了这些"。
"""

from dataclasses import dataclass, field
from typing import List, Optional

from .music_controller import Track


@dataclass
class UnannouncedSongs:
    """已播但未被介绍的歌曲队列。先进先出。"""
    _queue: List[Track] = field(default_factory=list)
    
    def add(self, track: Track) -> None:
        """加入一首歌。如果已经在队列里就不重复加。"""
        if not any(self._same(t, track) for t in self._queue):
            self._queue.append(track)
    
    def remove(self, track: Track) -> bool:
        """从队列移除一首歌。返回是否真的移除了。"""
        for i, t in enumerate(self._queue):
            if self._same(t, track):
                del self._queue[i]
                return True
        return False
    
    def remove_many(self, tracks: List[Track]) -> int:
        """批量移除。返回实际移除的数量。"""
        removed = 0
        for t in tracks:
            if self.remove(t):
                removed += 1
        return removed
    
    def all(self) -> List[Track]:
        """返回当前队列的拷贝"""
        return list(self._queue)
    
    def is_empty(self) -> bool:
        return len(self._queue) == 0
    
    def __len__(self) -> int:
        return len(self._queue)
    
    @staticmethod
    def _same(a: Track, b: Track) -> bool:
        """歌曲相等判断：用 (歌名, 艺人) 作为 key"""
        return a.name == b.name and a.artist == b.artist