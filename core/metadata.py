"""
元数据增强

Apple Music 给的元信息比较少（就歌名、艺人、专辑、年份、流派）。
这里从 MusicBrainz（免费、无 key）补充艺人简介、专辑背景等。

MusicBrainz API 文档: https://musicbrainz.org/doc/MusicBrainz_API
需要在 User-Agent 里标识自己的应用，这是 MB 的硬性要求。
"""

import urllib.request
import urllib.parse
import json
from typing import Optional
from dataclasses import dataclass, field


USER_AGENT = "HITFM-Local/0.1 (https://github.com/yourname/hitfm-local)"


@dataclass
class EnrichedMetadata:
    """补充的背景资料"""
    artist_country: Optional[str] = None
    artist_type: Optional[str] = None  # Person / Group
    artist_begin_year: Optional[str] = None
    artist_tags: list = field(default_factory=list)  # 流派/风格标签
    album_release_date: Optional[str] = None
    album_type: Optional[str] = None  # Album / Single / EP
    
    def is_empty(self) -> bool:
        return (self.artist_country is None 
                and self.artist_type is None 
                and not self.artist_tags
                and self.album_release_date is None)


def _http_get_json(url: str, timeout: float = 5.0) -> Optional[dict]:
    """简单的 HTTP GET，失败返回 None"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[metadata] 请求失败 {url}: {e}")
        return None


def fetch_musicbrainz_metadata(artist: str, album: Optional[str] = None) -> EnrichedMetadata:
    """
    查询 MusicBrainz 获取艺人和专辑信息。
    
    注意：MusicBrainz 对匿名请求限速 1 req/s。Demo 场景一首歌查一次，够用。
    如果后面要高频调用，需要注册 MB 账户提升额度。
    """
    meta = EnrichedMetadata()
    
    # 查艺人
    q = urllib.parse.quote(artist)
    artist_data = _http_get_json(
        f"https://musicbrainz.org/ws/2/artist/?query=artist:{q}&fmt=json&limit=1"
    )
    if artist_data and artist_data.get("artists"):
        a = artist_data["artists"][0]
        meta.artist_country = a.get("country")
        meta.artist_type = a.get("type")
        life_span = a.get("life-span", {})
        begin = life_span.get("begin")
        if begin:
            meta.artist_begin_year = begin[:4]  # 只取年份
        tags = a.get("tags", [])
        # 按 count 排序取前 3 个
        tags_sorted = sorted(tags, key=lambda t: t.get("count", 0), reverse=True)
        meta.artist_tags = [t["name"] for t in tags_sorted[:3]]
    
    # 查专辑（可选）
    if album:
        q_album = urllib.parse.quote(album)
        q_artist = urllib.parse.quote(artist)
        album_data = _http_get_json(
            f"https://musicbrainz.org/ws/2/release-group/?query=release:{q_album}%20AND%20artist:{q_artist}&fmt=json&limit=1"
        )
        if album_data and album_data.get("release-groups"):
            rg = album_data["release-groups"][0]
            meta.album_release_date = rg.get("first-release-date")
            meta.album_type = rg.get("primary-type")
    
    return meta


def format_for_prompt(meta: EnrichedMetadata) -> str:
    """把元数据格式化成给 LLM 的文本"""
    if meta.is_empty():
        return "（暂无额外资料）"
    
    lines = []
    if meta.artist_country:
        lines.append(f"艺人来自: {meta.artist_country}")
    if meta.artist_type:
        lines.append(f"艺人类型: {meta.artist_type}")
    if meta.artist_begin_year:
        lines.append(f"艺人出道/成立年份: {meta.artist_begin_year}")
    if meta.artist_tags:
        lines.append(f"艺人风格标签: {', '.join(meta.artist_tags)}")
    if meta.album_release_date:
        lines.append(f"专辑发行日期: {meta.album_release_date}")
    if meta.album_type:
        lines.append(f"专辑类型: {meta.album_type}")
    
    return "\n".join(lines)
