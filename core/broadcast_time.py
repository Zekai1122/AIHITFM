"""
广播时间：把"现在几点几分、在哪个时区、什么季节"封装成主持人能用的中文上下文。

支持三种时区模式（config.yaml 的 time.time_zone_mode）：
- "auto"：用系统本地时区（macOS 上常能拿到 IANA 名）
- "beijing"：强制北京时间（Asia/Shanghai）
- 直接给 IANA 名字符串：例如 "Australia/Sydney"、"America/Los_Angeles"

输出的 BroadcastTime 结构包含：
- spoken_zone_name：口语化时区名（"北京时间"、"悉尼时间"、"当地时间"）
- year/month/day/hour/minute
- weekday_zh：星期几（中文）
- day_part：早间/上午/午间/下午/晚间/深夜
- season_zh：春/夏/秋/冬（考虑南北半球）
- hemisphere："北半球"/"南半球"/"赤道附近"
- as_prompt_block()：直接生成给 LLM 的上下文字符串
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


# IANA 时区名 → 口语化中文名映射
# 没列出的会降级为 "当地时间"
SPOKEN_ZONE_NAMES = {
    # 大中华
    "Asia/Shanghai": "北京时间",
    "Asia/Chongqing": "北京时间",
    "Asia/Harbin": "北京时间",
    "Asia/Urumqi": "新疆时间",
    "Asia/Hong_Kong": "香港时间",
    "Asia/Macau": "澳门时间",
    "Asia/Taipei": "台北时间",
    # 东亚 / 东南亚
    "Asia/Tokyo": "东京时间",
    "Asia/Seoul": "首尔时间",
    "Asia/Singapore": "新加坡时间",
    "Asia/Bangkok": "曼谷时间",
    "Asia/Kuala_Lumpur": "吉隆坡时间",
    "Asia/Manila": "马尼拉时间",
    "Asia/Jakarta": "雅加达时间",
    "Asia/Ho_Chi_Minh": "胡志明市时间",
    # 大洋洲
    "Australia/Sydney": "悉尼时间",
    "Australia/Melbourne": "墨尔本时间",
    "Australia/Brisbane": "布里斯班时间",
    "Australia/Perth": "珀斯时间",
    "Australia/Adelaide": "阿德莱德时间",
    "Pacific/Auckland": "奥克兰时间",
    # 欧洲
    "Europe/London": "伦敦时间",
    "Europe/Paris": "巴黎时间",
    "Europe/Berlin": "柏林时间",
    "Europe/Madrid": "马德里时间",
    "Europe/Rome": "罗马时间",
    "Europe/Amsterdam": "阿姆斯特丹时间",
    "Europe/Moscow": "莫斯科时间",
    # 北美
    "America/New_York": "纽约时间",
    "America/Los_Angeles": "洛杉矶时间",
    "America/Chicago": "芝加哥时间",
    "America/Denver": "丹佛时间",
    "America/Vancouver": "温哥华时间",
    "America/Toronto": "多伦多时间",
    "America/Mexico_City": "墨西哥城时间",
    # 南美
    "America/Sao_Paulo": "圣保罗时间",
    "America/Buenos_Aires": "布宜诺斯艾利斯时间",
    # 中东 / 印度
    "Asia/Dubai": "迪拜时间",
    "Asia/Kolkata": "新德里时间",
    # 非洲
    "Africa/Cairo": "开罗时间",
    "Africa/Johannesburg": "约翰内斯堡时间",
}


# 部分时区的近似纬度（用来判断半球，从而决定季节）
# 只列出 SPOKEN_ZONE_NAMES 里有的且有歧义的（南半球或赤道附近）；
# 其他默认按北半球处理（IANA 数据库本身只记位置不记纬度，自己列简化的）
SOUTHERN_HEMISPHERE_ZONES = {
    "Australia/Sydney",
    "Australia/Melbourne",
    "Australia/Brisbane",
    "Australia/Perth",
    "Australia/Adelaide",
    "Pacific/Auckland",
    "America/Sao_Paulo",
    "America/Buenos_Aires",
    "Africa/Johannesburg",
}

# 大约赤道附近（季节不明显，季节字段写"全年炎热"）
EQUATORIAL_ZONES = {
    "Asia/Singapore",
    "Asia/Kuala_Lumpur",
    "Asia/Jakarta",
    "Asia/Manila",
}


def detect_system_iana_zone() -> Optional[str]:
    """
    检测系统的 IANA 时区名（macOS/Linux）。
    
    优先级：
    1. TZ 环境变量
    2. /etc/localtime 符号链接的目标路径
    3. /etc/timezone 文件内容
    
    都失败返回 None（调用方降级到 fixed offset 或 "当地时间"）
    """
    # 1. TZ 环境变量
    tz_env = os.environ.get("TZ", "").strip()
    if tz_env and "/" in tz_env:  # 排除 "PST8PDT" 这种 POSIX 风格
        return tz_env
    
    # 2. /etc/localtime 是符号链接（macOS、多数 Linux）
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = os.readlink(localtime)
        # 典型形如 /var/db/timezone/zoneinfo/Asia/Shanghai
        # 或 ../usr/share/zoneinfo/Asia/Shanghai
        parts = Path(target).parts
        # 找到 "zoneinfo" 之后的部分
        try:
            idx = parts.index("zoneinfo")
            iana = "/".join(parts[idx + 1:])
            if iana:
                return iana
        except ValueError:
            pass
    
    # 3. /etc/timezone（部分 Linux 发行版）
    tz_file = Path("/etc/timezone")
    if tz_file.is_file():
        try:
            content = tz_file.read_text(encoding="utf-8").strip()
            if content and "/" in content:
                return content
        except OSError:
            pass
    
    return None


@dataclass
class BroadcastTime:
    """主持人需要的时间上下文"""
    
    now: datetime           # 已经带 tzinfo 的当前时刻
    iana_zone: Optional[str]  # IANA 时区名，可能为 None（未知）
    spoken_zone_name: str   # 口语化中文，"北京时间"等
    
    @property
    def year(self) -> int:
        return self.now.year
    
    @property
    def month(self) -> int:
        return self.now.month
    
    @property
    def day(self) -> int:
        return self.now.day
    
    @property
    def hour(self) -> int:
        return self.now.hour
    
    @property
    def minute(self) -> int:
        return self.now.minute
    
    @property
    def weekday_zh(self) -> str:
        names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        return names[self.now.weekday()]
    
    @property
    def day_part(self) -> str:
        """早间/上午/午间/下午/晚间/深夜"""
        h = self.hour
        if 5 <= h < 9:
            return "早间"
        elif 9 <= h < 11:
            return "上午"
        elif 11 <= h < 14:
            return "午间"
        elif 14 <= h < 18:
            return "下午"
        elif 18 <= h < 23:
            return "晚间"
        else:
            return "深夜"
    
    @property
    def hemisphere(self) -> str:
        if self.iana_zone in EQUATORIAL_ZONES:
            return "赤道附近"
        if self.iana_zone in SOUTHERN_HEMISPHERE_ZONES:
            return "南半球"
        return "北半球"
    
    @property
    def season_zh(self) -> str:
        """根据月份和半球返回春/夏/秋/冬。赤道附近返回'全年炎热'。"""
        if self.hemisphere == "赤道附近":
            return "全年炎热（无明显四季）"
        
        m = self.month
        # 北半球：3-5 春，6-8 夏，9-11 秋，12-2 冬
        if self.hemisphere == "北半球":
            if 3 <= m <= 5:
                return "春天"
            elif 6 <= m <= 8:
                return "夏天"
            elif 9 <= m <= 11:
                return "秋天"
            else:
                return "冬天"
        # 南半球：相反
        else:
            if 3 <= m <= 5:
                return "秋天"
            elif 6 <= m <= 8:
                return "冬天"
            elif 9 <= m <= 11:
                return "春天"
            else:
                return "夏天"
    
    def as_prompt_block(self) -> str:
        """生成给 LLM 的上下文字符串，可直接拼进 user prompt"""
        return (
            f"**当前时间**：{self.spoken_zone_name} "
            f"{self.year}年{self.month}月{self.day}日{self.weekday_zh}，"
            f"{self.hour:02d}:{self.minute:02d}\n"
            f"**时段**：{self.day_part}\n"
            f"**季节**：{self.season_zh}（{self.hemisphere}）\n"
            f"\n报时请按以下格式自然融入台词："
            f"\"{self.spoken_zone_name}{self.hour}点{self.minute:02d}分\"。"
        )


class BroadcastTimeProvider:
    """
    根据配置决定用哪个时区，每次 now() 拿"当前广播时间"。
    
    用法：
        provider = BroadcastTimeProvider.from_config(cfg["time"])
        bt = provider.now()
        prompt_text = bt.as_prompt_block()
    """
    
    def __init__(self, iana_zone: Optional[str], spoken_zone_name: str):
        self.iana_zone = iana_zone
        self.spoken_zone_name = spoken_zone_name
        if iana_zone:
            try:
                self._tzinfo: Optional[tzinfo] = ZoneInfo(iana_zone)
            except Exception:
                # IANA 名无效，退回系统本地
                self._tzinfo = None
        else:
            self._tzinfo = None
    
    @classmethod
    def from_config(cls, time_cfg: Optional[dict]) -> "BroadcastTimeProvider":
        """
        从 config.yaml 的 time 段构造。time_cfg 形如：
            time:
              time_zone_mode: auto | beijing | <IANA-name>
              spoken_name_override: "<自定义中文>"   # 可选，覆盖默认口语化名
        time_cfg 为 None 时按 auto 处理。
        """
        time_cfg = time_cfg or {}
        mode = (time_cfg.get("time_zone_mode") or "auto").strip()
        override = time_cfg.get("spoken_name_override")
        
        if mode == "beijing":
            iana = "Asia/Shanghai"
        elif mode == "auto":
            iana = detect_system_iana_zone()
        else:
            # 当 IANA 名处理（如 "Australia/Sydney"）
            iana = mode
        
        spoken = override or cls._derive_spoken_name(iana)
        return cls(iana_zone=iana, spoken_zone_name=spoken)
    
    @staticmethod
    def _derive_spoken_name(iana: Optional[str]) -> str:
        if iana and iana in SPOKEN_ZONE_NAMES:
            return SPOKEN_ZONE_NAMES[iana]
        return "当地时间"
    
    def now(self, offset_seconds: float = 0.0) -> BroadcastTime:
        """
        返回当前广播时间。
        
        offset_seconds > 0 时返回"未来"那一刻——用于预生成场景：
        台词在歌曲结束后才朗读，所以生成时应该用"歌结束时"的时间，
        而不是"调用 generate() 那一刻"的时间。
        """
        from datetime import timedelta
        if self._tzinfo:
            current = datetime.now(self._tzinfo)
        else:
            # 系统本地（无明确 IANA）
            current = datetime.now().astimezone()
        if offset_seconds:
            current = current + timedelta(seconds=offset_seconds)
        return BroadcastTime(
            now=current,
            iana_zone=self.iana_zone,
            spoken_zone_name=self.spoken_zone_name,
        )