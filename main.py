"""
Soul Moments - QQ 说说自动发布 + 社交互动插件
让 Bot 像真人一样发 QQ 说说/动态，内容完全由角色人格驱动。
v1.0.0: 每天随机 0-N 条，自动规划发布时间，极致省 token
v1.1.0: 社交扫描 - 回复自己说说下的评论，时段式扫描，省 token
v1.2.0: 好友空间监测 - 刷好友说说/签名，LLM 驱动点赞/评论/私聊
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.api.star import Context, Star, register

# 尝试导入 StarTools
try:
    from astrbot.api.star import StarTools
    HAS_STARTOOLS = True
except ImportError:
    HAS_STARTOOLS = False


# ============================================================
# 工具函数
# ============================================================

def _now_tz(tz_name: str | None) -> datetime:
    """获取指定时区的当前时间"""
    if tz_name:
        try:
            import zoneinfo
            return datetime.now(zoneinfo.ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now()


def _parse_hhmm(s: str) -> Optional[Tuple[int, int]]:
    """解析 HH:MM 格式"""
    if not s:
        return None
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", s.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _parse_range(val: str, fallback_min: float = 0, fallback_max: float = 2) -> Tuple[float, float]:
    """解析 '0-2' 格式的区间"""
    val = str(val).strip()
    if not val:
        return fallback_min, fallback_max
    if "-" in val:
        parts = val.split("-", 1)
        try:
            a, b = float(parts[0]), float(parts[1])
            return (min(a, b), max(a, b))
        except ValueError:
            return fallback_min, fallback_max
    try:
        v = float(val)
        return v, v
    except ValueError:
        return fallback_min, fallback_max


def _gtk(skey: str) -> int:
    """计算 QQ g_tk (CSRF token)"""
    h = 5381
    for c in skey:
        h += (h << 5) + ord(c)
    return h & 0x7FFFFFFF


def _parse_scan_schedule(schedule_str: str) -> List[Tuple[int, int, int, int, int]]:
    """解析扫描时段配置。

    格式: "HH:MM-HH:MM/间隔分钟, ..."
    返回: [(start_h, start_m, end_h, end_m, interval_min), ...]
    """
    if not schedule_str or not schedule_str.strip():
        return []

    result = []
    segments = re.split(r"[,，]", schedule_str)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        # 解析 "HH:MM-HH:MM/N"
        m = re.match(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*/\s*(\d+)", seg)
        if not m:
            logger.warning(f"[SoulMoments] 无法解析扫描时段: '{seg}'，格式应为 HH:MM-HH:MM/分钟")
            continue
        start = _parse_hhmm(m.group(1))
        end = _parse_hhmm(m.group(2))
        interval = int(m.group(3))
        if start and end and interval > 0:
            result.append((start[0], start[1], end[0], end[1], interval))
    return result


def _extract_json_from_jsonp(text: str) -> dict | None:
    """从 JSONP callback(json) 格式中提取 JSON"""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ============================================================
# 状态数据
# ============================================================

@dataclass
class FriendWatchState:
    """单个好友的监测状态"""
    qq: str = ""
    last_seen_tid: str = ""           # 上次看到的最新说说 ID
    last_seen_ts: float = 0.0        # 上次看到的最新说说的时间戳（用于判断新旧）
    last_seen_sign: str = ""          # 上次看到的签名
    last_check_ts: float = 0.0       # 上次检查时间
    interacted_tids: List[str] = field(default_factory=list)  # 已互动的说说 ID
    total_likes: int = 0
    total_comments: int = 0
    total_chats: int = 0             # 因好友动态触发的私聊次数

    def to_dict(self) -> dict:
        return {
            "qq": self.qq,
            "last_seen_tid": self.last_seen_tid,
            "last_seen_ts": self.last_seen_ts,
            "last_seen_sign": self.last_seen_sign,
            "last_check_ts": self.last_check_ts,
            "interacted_tids": self.interacted_tids[-100:],  # 最多保留 100 条
            "total_likes": self.total_likes,
            "total_comments": self.total_comments,
            "total_chats": self.total_chats,
        }

    @staticmethod
    def from_dict(d: dict) -> "FriendWatchState":
        return FriendWatchState(
            qq=d.get("qq", ""),
            last_seen_tid=d.get("last_seen_tid", ""),
            last_seen_ts=d.get("last_seen_ts", 0.0),
            last_seen_sign=d.get("last_seen_sign", ""),
            last_check_ts=d.get("last_check_ts", 0.0),
            interacted_tids=d.get("interacted_tids", []),
            total_likes=d.get("total_likes", 0),
            total_comments=d.get("total_comments", 0),
            total_chats=d.get("total_chats", 0),
        )


@dataclass
class MomentsState:
    """单个 Bot 的说说状态"""
    # --- 发布计划 ---
    today_date: str = ""
    today_plan_count: int = 0
    today_plan_times: List[float] = field(default_factory=list)
    today_posted_count: int = 0
    total_posts: int = 0
    last_post_content: str = ""
    last_post_ts: float = 0.0
    # --- 社交扫描 ---
    last_scan_ts: float = 0.0
    replied_comment_ids: List[str] = field(default_factory=list)
    total_replies: int = 0
    # --- 好友监测 (Phase 2) ---
    friend_watch: Dict[str, FriendWatchState] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "today_date": self.today_date,
            "today_plan_count": self.today_plan_count,
            "today_plan_times": self.today_plan_times,
            "today_posted_count": self.today_posted_count,
            "total_posts": self.total_posts,
            "last_post_content": self.last_post_content,
            "last_post_ts": self.last_post_ts,
            "last_scan_ts": self.last_scan_ts,
            "replied_comment_ids": self.replied_comment_ids[-500:],
            "total_replies": self.total_replies,
            "friend_watch": {qq: fw.to_dict() for qq, fw in self.friend_watch.items()},
        }

    @staticmethod
    def from_dict(d: dict) -> "MomentsState":
        fw_raw = d.get("friend_watch", {})
        friend_watch = {}
        if isinstance(fw_raw, dict):
            for qq, fw_data in fw_raw.items():
                friend_watch[qq] = FriendWatchState.from_dict(fw_data)
        return MomentsState(
            today_date=d.get("today_date", ""),
            today_plan_count=d.get("today_plan_count", 0),
            today_plan_times=d.get("today_plan_times", []),
            today_posted_count=d.get("today_posted_count", 0),
            total_posts=d.get("total_posts", 0),
            last_post_content=d.get("last_post_content", ""),
            last_post_ts=d.get("last_post_ts", 0.0),
            last_scan_ts=d.get("last_scan_ts", 0.0),
            replied_comment_ids=d.get("replied_comment_ids", []),
            total_replies=d.get("total_replies", 0),
            friend_watch=friend_watch,
        )


def _parse_watch_friends(raw: str) -> List[Tuple[str, str]]:
    """解析关注好友配置。

    格式: "QQ号" 或 "QQ号:等级"，逗号分隔
    返回: [(qq, level), ...] level = close/normal/casual
    """
    if not raw or not raw.strip():
        return []
    result = []
    for part in re.split(r"[,，]+", raw.strip()):
        part = part.strip()
        if not part:
            continue
        if ":" in part or "：" in part:
            segs = re.split(r"[:：]", part, 1)
            qq = segs[0].strip()
            level = segs[1].strip().lower() if len(segs) > 1 else "normal"
            if level not in ("close", "normal", "casual"):
                level = "normal"
        else:
            qq = part
            level = "normal"
        if qq:
            result.append((qq, level))
    return result


# ============================================================
# 主插件类
# ============================================================

@register(
    "astrbot_plugin_soul_moments",
    "bk的殿下",
    "让 Bot 像真人一样发 QQ 说说/动态，内容完全由角色人格驱动",
    "1.2.0",
    repo="",
)
class SoulMomentsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self._states: Dict[str, MomentsState] = {}
        self._scheduler_task: Optional[asyncio.Task] = None
        self._heartbeat_count = 0

        # 数据目录
        if HAS_STARTOOLS:
            data_dir = str(StarTools.get_data_dir() / "astrbot_plugin_soul_moments")
        else:
            data_dir = os.path.join(os.getcwd(), "data", "plugin_data", "astrbot_plugin_soul_moments")
        os.makedirs(data_dir, exist_ok=True)
        self._state_file = os.path.join(data_dir, "moments_states.json")
        self._load_states()

    async def initialize(self):
        """插件启动"""
        tz = self._get_cfg("moments_settings", "timezone", default="Asia/Shanghai")

        for pid in self._get_platform_ids():
            if pid not in self._states:
                self._states[pid] = MomentsState()
            self._ensure_today_plan(pid, tz)

        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        pids = self._get_platform_ids()
        schedule = self._get_cfg("social_scan_settings", "scan_schedule", default="")
        watch_friends = self._get_cfg("social_scan_settings", "watch_friends", default="")
        watch_list = _parse_watch_friends(watch_friends)
        logger.info(
            f"[SoulMoments] 插件已启动 v1.2.0，管理 {len(pids)} 个 Bot: "
            f"{', '.join(pids) if pids else '(未配置)'}，"
            f"扫描时段: {schedule if schedule else '(未配置)'}，"
            f"关注好友: {len(watch_list)} 个"
        )

    async def terminate(self):
        """插件卸载"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
        self._save_states()
        logger.info("[SoulMoments] 插件已卸载")

    # ============================================================
    # 配置读取
    # ============================================================

    def _get_cfg(self, *keys, default=None):
        """从插件配置读取值"""
        node = self.cfg
        for k in keys:
            if isinstance(node, dict):
                node = node.get(k, None)
            else:
                return default
            if node is None:
                return default
        return node

    def _get_platform_ids(self) -> list[str]:
        """解析配置中的平台 ID 列表"""
        raw = self._get_cfg("moments_settings", "platform_id", default="")
        if not raw or not raw.strip():
            return []
        ids = re.split(r"[,，\s]+", raw.strip())
        return [pid.strip() for pid in ids if pid.strip()]

    # ============================================================
    # 状态持久化
    # ============================================================

    def _load_states(self):
        """加载状态"""
        if os.path.exists(self._state_file):
            try:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for pid, state_data in data.items():
                    self._states[pid] = MomentsState.from_dict(state_data)
                logger.debug(f"[SoulMoments] 已加载 {len(self._states)} 个平台状态")
            except Exception as e:
                logger.warning(f"[SoulMoments] 加载状态失败: {e}")

    def _save_states(self):
        """保存状态"""
        try:
            data = {pid: st.to_dict() for pid, st in self._states.items()}
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[SoulMoments] 保存状态失败: {e}")

    # ============================================================
    # 每日计划（发布说说）
    # ============================================================

    def _ensure_today_plan(self, platform_id: str, tz: str):
        """确保今天的发布计划已制定"""
        now = _now_tz(tz)
        today_str = now.strftime("%Y-%m-%d")

        st = self._states.get(platform_id)
        if not st:
            st = MomentsState()
            self._states[platform_id] = st

        if st.today_date == today_str:
            return

        daily_str = self._get_cfg("moments_settings", "daily_range", default="0-2")
        min_count, max_count = _parse_range(daily_str, 0, 2)
        plan_count = random.randint(int(min_count), int(max_count))

        plan_times = []
        if plan_count > 0:
            plan_times = self._pick_random_times(now, plan_count, tz)

        st.today_date = today_str
        st.today_plan_count = plan_count
        st.today_plan_times = plan_times
        st.today_posted_count = 0

        if plan_count > 0:
            time_strs = [datetime.fromtimestamp(t).strftime("%H:%M") for t in sorted(plan_times)]
            logger.info(f"[SoulMoments] [{platform_id}] 今日计划: {plan_count} 条说说，时间: {', '.join(time_strs)}")
        else:
            logger.info(f"[SoulMoments] [{platform_id}] 今日计划: 不发说说")

        self._save_states()

    def _pick_random_times(self, now: datetime, count: int, tz: str) -> List[float]:
        """在活跃时段内随机选择 count 个发布时间"""
        active_str = self._get_cfg("moments_settings", "active_hours", default="08:00-23:00")

        start_h, start_m = 8, 0
        end_h, end_m = 23, 0
        if active_str and "-" in active_str:
            parts = active_str.split("-", 1)
            p1 = _parse_hhmm(parts[0])
            p2 = _parse_hhmm(parts[1])
            if p1:
                start_h, start_m = p1
            if p2:
                end_h, end_m = p2

        today = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        today_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

        if now > today:
            today = now + timedelta(minutes=5)

        if today >= today_end:
            return []

        start_ts = today.timestamp()
        end_ts = today_end.timestamp()
        times = sorted(random.uniform(start_ts, end_ts) for _ in range(count))
        return times

    # ============================================================
    # 调度逻辑
    # ============================================================

    async def _scheduler_loop(self):
        """主调度循环，每 60 秒检查"""
        await asyncio.sleep(15)
        logger.info("[SoulMoments] 调度器已启动")

        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SoulMoments] 调度器异常: {e}")
            await asyncio.sleep(60)

    async def _tick(self):
        """每次调度检查"""
        if not self._get_cfg("enable", default=True):
            return

        platform_ids = self._get_platform_ids()
        if not platform_ids:
            return

        tz = self._get_cfg("moments_settings", "timezone", default="Asia/Shanghai")
        now = _now_tz(tz)
        now_ts = now.timestamp()

        # 心跳日志（每 10 分钟）
        self._heartbeat_count += 1
        if self._heartbeat_count % 10 == 0:
            parts = []
            for pid in platform_ids:
                st = self._states.get(pid)
                if st:
                    remaining = st.today_plan_count - st.today_posted_count
                    next_ts_list = [t for t in st.today_plan_times if t > now_ts]
                    next_str = datetime.fromtimestamp(next_ts_list[0]).strftime("%H:%M") if next_ts_list else "无"
                    scan_ago = int((now_ts - st.last_scan_ts) / 60) if st.last_scan_ts else -1
                    scan_str = f"{scan_ago}分钟前" if scan_ago >= 0 else "从未"
                    parts.append(f"{pid}: 剩{remaining}条, 下次{next_str}, 扫描{scan_str}")
                else:
                    parts.append(f"{pid}: 未初始化")
            logger.info(f"[SoulMoments] ❤️ 心跳 | {' | '.join(parts)}")

        # 逐个平台检查
        for pid in platform_ids:
            try:
                self._ensure_today_plan(pid, tz)

                st = self._states.get(pid)
                if not st:
                    continue

                # === 检查发布计划 ===
                if st.today_posted_count < st.today_plan_count:
                    for plan_ts in sorted(st.today_plan_times):
                        if plan_ts <= now_ts:
                            idx = sorted(st.today_plan_times).index(plan_ts)
                            if idx < st.today_posted_count:
                                continue
                            logger.info(f"[SoulMoments] [{pid}] 到达发布时间，开始生成说说...")
                            await self._do_post(pid, now)
                            break

                # === 检查社交扫描 ===
                if self._should_scan(pid, now):
                    logger.info(f"[SoulMoments] [{pid}] 到达扫描时间，开始社交扫描...")
                    await self._do_scan(pid, now)
                    # === 好友空间监测 ===
                    await self._do_friend_scan(pid, now)

            except Exception as e:
                logger.error(f"[SoulMoments] [{pid}] 检查时出错: {e}")

    def _should_scan(self, platform_id: str, now: datetime) -> bool:
        """判断是否应该执行社交扫描"""
        schedule_str = self._get_cfg("social_scan_settings", "scan_schedule", default="")
        if not schedule_str:
            return False

        schedule = _parse_scan_schedule(schedule_str)
        if not schedule:
            return False

        st = self._states.get(platform_id)
        if not st:
            return False

        cur_h, cur_m = now.hour, now.minute
        now_ts = now.timestamp()

        for start_h, start_m, end_h, end_m, interval_min in schedule:
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m
            cur_minutes = cur_h * 60 + cur_m

            if start_minutes <= cur_minutes < end_minutes:
                # 在此时段内，检查间隔
                if st.last_scan_ts == 0:
                    return True  # 从未扫描过
                elapsed = (now_ts - st.last_scan_ts) / 60.0
                if elapsed >= interval_min:
                    return True
                return False

        return False

    # ============================================================
    # QZone 认证（复用）
    # ============================================================

    async def _get_qzone_auth(self, platform_id: str) -> dict | None:
        """获取 QZone 认证信息。

        返回 {"cookies": str, "uin": str, "g_tk": int, "bot": bot} 或 None
        """
        try:
            platform = self.context.get_platform_inst(platform_id)
            if not platform:
                logger.error(f"[SoulMoments] [{platform_id}] 找不到平台")
                return None

            bot = platform.get_client()
            if not bot:
                logger.error(f"[SoulMoments] [{platform_id}] 获取 bot 客户端失败")
                return None

            try:
                cookies_data = await bot.call_action(action="get_cookies", domain="qzone.qq.com")
            except Exception as e:
                logger.error(f"[SoulMoments] [{platform_id}] 获取 QZone cookies 失败: {e}")
                return None

            if not cookies_data:
                logger.error(f"[SoulMoments] [{platform_id}] QZone cookies 为空")
                return None

            cookies_str = cookies_data.get("cookies", "") if isinstance(cookies_data, dict) else ""
            if not cookies_str:
                logger.error(f"[SoulMoments] [{platform_id}] 无法解析 cookies")
                return None

            skey = ""
            p_skey = ""
            uin = ""
            for part in cookies_str.split(";"):
                part = part.strip()
                if part.startswith("skey="):
                    skey = part.split("=", 1)[1]
                elif part.startswith("p_skey="):
                    p_skey = part.split("=", 1)[1]
                elif part.startswith("uin=") or part.startswith("p_uin="):
                    uin = part.split("=", 1)[1]
                    if uin.startswith("o"):
                        uin = uin[1:]

            gtk_key = p_skey or skey
            if not gtk_key:
                logger.error(f"[SoulMoments] [{platform_id}] cookies 中无 skey/p_skey")
                return None

            if not uin:
                logger.error(f"[SoulMoments] [{platform_id}] cookies 中无 uin")
                return None

            g_tk = _gtk(gtk_key)
            logger.info(f"[SoulMoments] [{platform_id}] QZone 认证成功: uin={uin}, g_tk={g_tk}")
            return {"cookies": cookies_str, "uin": uin, "g_tk": g_tk, "bot": bot}

        except Exception as e:
            logger.error(f"[SoulMoments] [{platform_id}] QZone 认证失败: {e}")
            return None

    def _qzone_headers(self, cookies: str, uin: str) -> dict:
        """构建 QZone 请求头"""
        return {
            "Cookie": cookies,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"https://user.qzone.qq.com/{uin}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://user.qzone.qq.com",
        }

    # ============================================================
    # 发布说说
    # ============================================================

    async def _do_post(self, platform_id: str, now: datetime):
        """执行一次说说发布"""
        st = self._states.get(platform_id)
        if not st:
            return

        persona = await self._get_persona(platform_id)
        max_length = self._get_cfg("moments_settings", "max_length", default=200)
        prompt = self._build_post_prompt(now, persona, max_length)

        content = await self._generate_content(prompt, platform_id, max_length)
        if not content:
            logger.warning(f"[SoulMoments] [{platform_id}] LLM 未返回有效内容")
            st.today_posted_count += 1
            self._save_states()
            return

        success = await self._publish_qzone(platform_id, content)

        if success:
            st.today_posted_count += 1
            st.total_posts += 1
            st.last_post_content = content
            st.last_post_ts = now.timestamp()
            logger.info(f"[SoulMoments] [{platform_id}] ✅ 说说已发布: {content[:50]}...")
        else:
            st.today_posted_count += 1
            logger.error(f"[SoulMoments] [{platform_id}] ❌ 说说发布失败")

        self._save_states()

    def _build_post_prompt(self, now: datetime, persona: str, max_length: int) -> str:
        """构建说说生成 prompt"""
        override = self._get_cfg("moments_settings", "prompt_override", default="")
        if override:
            return override.format(
                now=now.strftime("%Y-%m-%d %H:%M %A"),
                persona=persona,
                max_length=max_length,
            )

        now_str = now.strftime("%Y年%m月%d日 %H:%M %A")

        return f"""你正在发一条 QQ 说说（动态/朋友圈）。

当前时间：{now_str}

你的人格设定：
{persona if persona else "（无特定设定，做你自己）"}

请以你自己的角色身份，写一条 QQ 说说。

要求：
- 字数不超过 {max_length} 个字
- 完全用你自己的说话方式和语气
- 像真人发的动态一样自然
- 可以是：此刻的心情、日常记录、吐槽、感悟、分享、段子……什么都行
- 做你自己，发你想发的内容
- 不要用话题标签（#），不要 @任何人

直接回复说说内容，不要加引号，不要解释。"""

    async def _publish_qzone(self, platform_id: str, content: str) -> bool:
        """发布 QQ 说说"""
        auth = await self._get_qzone_auth(platform_id)
        if not auth:
            return False

        try:
            api_url = (
                f"https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
                f"/cgi-bin/emotion_cgi_publish_v6?g_tk={auth['g_tk']}"
            )
            headers = self._qzone_headers(auth["cookies"], auth["uin"])
            form_data = {
                "syn_tweet_verson": "1",
                "paramstr": "1",
                "who": "1",
                "con": content,
                "feedversion": "1",
                "ver": "1",
                "ugc_right": "1",
                "to_sign": "0",
                "hostuin": auth["uin"],
                "code_version": "1",
                "format": "json",
                "qzreferrer": f"https://user.qzone.qq.com/{auth['uin']}",
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url, headers=headers, data=form_data,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    text = await resp.text()
                    logger.debug(f"[SoulMoments] [{platform_id}] 发布响应: {text[:200]}")

                    if resp.status == 200:
                        result = _extract_json_from_jsonp(text)
                        if result:
                            if result.get("code") == 0 or result.get("subcode") == 0:
                                return True
                            logger.error(f"[SoulMoments] [{platform_id}] 发布返回错误: {result}")
                            return False
                        if "succ" in text.lower() or '"code":0' in text:
                            return True
                        logger.error(f"[SoulMoments] [{platform_id}] 发布返回解析失败: {text[:200]}")
                        return False
                    else:
                        logger.error(f"[SoulMoments] [{platform_id}] 发布 HTTP {resp.status}")
                        return False

        except Exception as e:
            logger.error(f"[SoulMoments] [{platform_id}] 发布说说异常: {e}")
            return False

    # ============================================================
    # 社交扫描：获取说说列表 + 评论
    # ============================================================

    async def _fetch_my_moments(self, platform_id: str, auth: dict, count: int = 5, target_uin: str = "") -> List[dict]:
        """获取说说列表（含评论）。

        target_uin 为空时获取自己的，填好友 QQ 号则获取好友的。
        返回: [{"tid": str, "content": str, "name": str, "created_time": int, "comments": [...]}]
        """
        host = target_uin if target_uin else auth["uin"]
        # 尝试多个 API 域名（QZone 对不同域名的限流策略不同）
        api_urls = [
            f"https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6?g_tk={auth['g_tk']}",
            f"https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6?g_tk={auth['g_tk']}",
        ]

        for api_url in api_urls:
            try:
                # 关键：uin 填目标 QQ 号（不管是自己还是好友）
                # hostuin 也填目标 QQ 号
                # 参考 huanxin996/qzone_api 的 get_self_zone 实现
                params = {
                    "uin": host,
                    "hostuin": host,
                    "ftype": "0",
                    "sort": "0",
                    "pos": "0",
                    "num": str(count),
                    "replynum": "100",
                    "callback": "_preloadCallback",
                    "code_version": "1",
                    "format": "jsonp",
                    "need_private_comment": "1",
                    "g_tk": str(auth["g_tk"]),
                }
                headers = {
                    "Cookie": auth["cookies"],
                    "Referer": f"https://user.qzone.qq.com/{host}",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    "Origin": "https://user.qzone.qq.com",
                    "Accept": "*/*",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                }

                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        api_url, params=params, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        text = await resp.text()
                        target_label = f"(好友{host})" if target_uin else "(自己)"
                        logger.info(
                            f"[SoulMoments] [{platform_id}] 说说列表响应{target_label}"
                            f"({api_url[:60]}...): HTTP {resp.status}, 长度 {len(text)}"
                        )

                        result = _extract_json_from_jsonp(text)
                        if not result:
                            try:
                                result = json.loads(text)
                            except json.JSONDecodeError:
                                pass
                        if not result:
                            logger.warning(f"[SoulMoments] [{platform_id}] 解析失败，尝试下一个域名: {text[:200]}")
                            continue

                        code = result.get("code")
                        subcode = result.get("subcode")
                        if code != 0 and subcode != 0:
                            logger.warning(
                                f"[SoulMoments] [{platform_id}] API 返回 code={code}, subcode={subcode}, "
                                f"msg={result.get('message', '')}，尝试下一个域名..."
                            )
                            continue

                        # 成功！解析说说列表
                        return self._parse_moments_response(platform_id, result)

            except Exception as e:
                logger.warning(f"[SoulMoments] [{platform_id}] 请求异常({api_url[:50]}): {e}")
                continue

        logger.error(f"[SoulMoments] [{platform_id}] 所有 API 域名均失败，无法获取说说列表")
        return []

    def _parse_moments_response(self, platform_id: str, result: dict) -> List[dict]:
        """解析说说列表 API 的响应"""
        msg_list = result.get("msglist") or []
        logger.info(f"[SoulMoments] [{platform_id}] API 返回 {len(msg_list)} 条说说原始数据")
        moments = []
        for msg in msg_list:
            tid = msg.get("tid", "")
            content = msg.get("content", msg.get("con", ""))
            poster_name = msg.get("name", "")
            created_time = msg.get("created_time", 0)
            comments = []

            # 评论在 commentlist 字段中
            comment_list = msg.get("commentlist") or []
            for cmt in comment_list:
                cmt_uin = str(cmt.get("uin", ""))
                cmt_name = cmt.get("name", cmt.get("nick", ""))
                cmt_content = cmt.get("content", "")
                cmt_time = cmt.get("create_time", 0)
                # 构建唯一 ID: tid_评论者uin_时间戳
                cmt_id = f"{tid}_{cmt_uin}_{cmt_time}"

                comments.append({
                    "uin": cmt_uin,
                    "name": cmt_name,
                    "content": cmt_content,
                    "create_time": cmt_time,
                    "id": cmt_id,
                })

            moments.append({
                "tid": tid,
                "content": content,
                "name": poster_name,
                "created_time": created_time,
                "comments": comments,
            })

        total_comments = sum(len(m["comments"]) for m in moments)
        logger.info(f"[SoulMoments] [{platform_id}] 获取到 {len(moments)} 条说说, 共 {total_comments} 条评论")
        # 打印每条说说的摘要，方便调试
        for i, m in enumerate(moments):
            ct = m.get("created_time", 0)
            ct_str = datetime.fromtimestamp(ct).strftime("%m-%d %H:%M") if ct else "?"
            logger.info(
                f"[SoulMoments] [{platform_id}]   #{i+1} tid={m['tid'][:12]}... "
                f"时间={ct_str} 内容=「{m['content'][:30]}」"
            )
        return moments

    # ============================================================
    # 社交扫描：回复评论
    # ============================================================

    async def _reply_qzone_comment(
        self, platform_id: str, auth: dict,
        tid: str, owner_uin: str, reply_to_uin: str, content: str,
    ) -> bool:
        """回复 QZone 说说的评论"""
        try:
            api_url = (
                f"https://user.qzone.qq.com/proxy/domain/taotao.qq.com"
                f"/cgi-bin/emotion_cgi_re_feeds?g_tk={auth['g_tk']}"
            )
            headers = self._qzone_headers(auth["cookies"], auth["uin"])
            form_data = {
                "topicId": f"{owner_uin}_{tid}",
                "content": content,
                "feedsType": "100",
                "hostUin": owner_uin,
                "uin": owner_uin,
                "replyUin": reply_to_uin,
                "format": "json",
                "ref": "feeds",
                "qzreferrer": f"https://user.qzone.qq.com/{owner_uin}",
            }

            logger.info(f"[SoulMoments] [{platform_id}] 回复评论: to={reply_to_uin}, 内容={content[:40]}...")

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url, headers=headers, data=form_data,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    text = await resp.text()
                    logger.info(f"[SoulMoments] [{platform_id}] 回复评论响应: HTTP {resp.status}, {text[:200]}")

                    if resp.status == 200:
                        result = _extract_json_from_jsonp(text)
                        if not result:
                            try:
                                result = json.loads(text)
                            except json.JSONDecodeError:
                                pass
                        if result:
                            if result.get("code") == 0 or result.get("subcode") == 0:
                                return True
                            logger.error(f"[SoulMoments] [{platform_id}] 回复评论返回错误: {result}")
                            return False
                        if "succ" in text.lower() or '"code":0' in text:
                            return True
                        logger.error(f"[SoulMoments] [{platform_id}] 回复评论解析失败: {text[:200]}")
                        return False
                    else:
                        logger.error(f"[SoulMoments] [{platform_id}] 回复评论 HTTP {resp.status}")
                        return False

        except Exception as e:
            logger.error(f"[SoulMoments] [{platform_id}] 回复评论异常: {e}")
            return False

    # ============================================================
    # 社交扫描：核心流程
    # ============================================================

    async def _do_scan(self, platform_id: str, now: datetime):
        """执行一次社交扫描"""
        st = self._states.get(platform_id)
        if not st:
            return

        # 更新扫描时间（在开始时更新，防止扫描期间被重复触发）
        st.last_scan_ts = now.timestamp()
        self._save_states()

        reply_enabled = self._get_cfg("social_scan_settings", "reply_to_comments", default=True)
        if not reply_enabled:
            logger.info(f"[SoulMoments] [{platform_id}] 评论回复功能已关闭，跳过扫描")
            return

        # 获取认证
        auth = await self._get_qzone_auth(platform_id)
        if not auth:
            logger.warning(f"[SoulMoments] [{platform_id}] QZone 认证失败，无法扫描")
            return

        # 获取说说列表
        max_posts = self._get_cfg("social_scan_settings", "max_check_posts", default=5)
        logger.info(f"[SoulMoments] [{platform_id}] 开始获取最近 {max_posts} 条说说...")
        moments = await self._fetch_my_moments(platform_id, auth, count=max_posts)
        if not moments:
            logger.info(f"[SoulMoments] [{platform_id}] 没有说说或获取失败")
            return

        # 过滤新评论
        bot_uin = auth["uin"]
        # 确保 uin 是纯数字（去掉 o 前缀等）
        bot_uin_clean = bot_uin.lstrip("o0") if bot_uin.startswith("o") else bot_uin
        logger.info(f"[SoulMoments] [{platform_id}] Bot UIN: {bot_uin}, 已回复 {len(st.replied_comment_ids)} 条历史评论")

        new_comments = []  # [(moment_content, comment_info, tid)]
        skipped_self = 0
        skipped_replied = 0
        for moment in moments:
            for cmt in moment["comments"]:
                cmt_uin = cmt["uin"]
                # 跳过 Bot 自己的评论（兼容不同格式的 uin）
                cmt_uin_clean = cmt_uin.lstrip("o0") if cmt_uin.startswith("o") else cmt_uin
                if cmt_uin == bot_uin or cmt_uin_clean == bot_uin_clean:
                    skipped_self += 1
                    continue
                # 跳过已回复的
                if cmt["id"] in st.replied_comment_ids:
                    skipped_replied += 1
                    continue
                new_comments.append((moment["content"], cmt, moment["tid"]))

        logger.info(
            f"[SoulMoments] [{platform_id}] 评论过滤: "
            f"新评论 {len(new_comments)}, 跳过自己 {skipped_self}, 已回复 {skipped_replied}"
        )

        if not new_comments:
            logger.info(f"[SoulMoments] [{platform_id}] 没有新评论需要回复")
            return

        logger.info(f"[SoulMoments] [{platform_id}] 发现 {len(new_comments)} 条新评论，开始处理...")

        # 获取角色人格
        persona = await self._get_persona(platform_id)

        # 逐条回复
        replied = 0
        failed = 0
        for moment_content, cmt, tid in new_comments:
            # 构建回复 prompt
            prompt = self._build_reply_prompt(persona, moment_content, cmt["name"], cmt["content"], now)

            # 调 LLM 生成回复
            reply_text = await self._generate_content(prompt, platform_id, max_len=200)
            if not reply_text:
                logger.warning(f"[SoulMoments] [{platform_id}] LLM 未生成回复内容，跳过评论 {cmt['id']}")
                st.replied_comment_ids.append(cmt["id"])  # 标记跳过，避免重试
                failed += 1
                continue

            # 发布回复
            success = await self._reply_qzone_comment(
                platform_id, auth,
                tid=tid,
                owner_uin=bot_uin,
                reply_to_uin=cmt["uin"],
                content=reply_text,
            )

            if success:
                st.replied_comment_ids.append(cmt["id"])
                st.total_replies += 1
                replied += 1
                logger.info(
                    f"[SoulMoments] [{platform_id}] ✅ 回复 {cmt['name']}: "
                    f"{reply_text[:40]}..."
                )
            else:
                st.replied_comment_ids.append(cmt["id"])  # 失败也标记，避免重试浪费 token
                failed += 1
                logger.error(f"[SoulMoments] [{platform_id}] ❌ 回复 {cmt['name']} 失败")

            # 多条评论之间间隔 2-5 秒，更像真人
            if len(new_comments) > 1:
                await asyncio.sleep(random.uniform(2, 5))

        self._save_states()
        logger.info(f"[SoulMoments] [{platform_id}] 扫描完成: {replied} 条回复成功, {failed} 条失败")

    def _build_reply_prompt(
        self, persona: str, post_content: str,
        commenter_name: str, comment_content: str, now: datetime,
    ) -> str:
        """构建评论回复 prompt"""
        now_str = now.strftime("%Y年%m月%d日 %H:%M")

        return f"""你正在 QQ 空间回复别人对你说说的评论。

当前时间：{now_str}

你的人格设定：
{persona if persona else "（无特定设定，做你自己）"}

你发的说说内容：
「{post_content}」

{commenter_name} 评论了你的说说：
「{comment_content}」

请以你自己的角色身份回复这条评论。

要求：
- 完全用你自己的说话方式和语气
- 像真人在 QQ 空间回复评论一样自然
- 根据你和对方的关系、你的性格来决定回复的态度和内容
- 简短自然，一般一两句话就够了
- 不要解释，直接回复内容"""

    # ============================================================
    # Phase 2: 好友空间监测
    # ============================================================

    async def _do_friend_scan(self, platform_id: str, now: datetime) -> dict:
        """扫描关注好友的空间：检查新说说和签名变化。

        返回扫描结果摘要 dict，供命令反馈用。
        """
        result = {"scanned": 0, "new_moments": 0, "actions": 0, "sign_changes": 0, "skipped_old": 0}

        watch_raw = self._get_cfg("social_scan_settings", "watch_friends", default="")
        watch_list = _parse_watch_friends(watch_raw)
        if not watch_list:
            return result

        st = self._states.get(platform_id)
        if not st:
            return result

        auth = await self._get_qzone_auth(platform_id)
        if not auth:
            logger.warning(f"[SoulMoments] [{platform_id}] QZone 认证失败，无法扫描好友空间")
            return result

        persona = await self._get_persona(platform_id)
        watch_max = self._get_cfg("social_scan_settings", "watch_max_posts", default=3)
        watch_sign = self._get_cfg("social_scan_settings", "watch_signature", default=True)

        logger.info(f"[SoulMoments] [{platform_id}] 开始好友空间监测，共 {len(watch_list)} 个好友")

        for friend_qq, level in watch_list:
            try:
                # 初始化好友监测状态
                if friend_qq not in st.friend_watch:
                    st.friend_watch[friend_qq] = FriendWatchState(qq=friend_qq)
                fw = st.friend_watch[friend_qq]

                # --- 获取好友昵称 ---
                friend_name = await self._get_friend_nickname(platform_id, auth, friend_qq)

                # --- 检查好友说说 ---
                # 请求比 watch_max 更多的说说，确保能覆盖到新发的
                fetch_count = max(watch_max, 10)
                moments = await self._fetch_my_moments(
                    platform_id, auth, count=fetch_count, target_uin=friend_qq
                )
                result["scanned"] += 1
                if moments:
                    actions, new_count, skipped = await self._process_friend_moments(
                        platform_id, auth, persona, now,
                        friend_qq, friend_name, level, moments, fw
                    )
                    result["actions"] += actions
                    result["new_moments"] += new_count
                    result["skipped_old"] += skipped
                else:
                    logger.info(f"[SoulMoments] [{platform_id}] 好友 {friend_name}({friend_qq}) 无可见说说或获取失败")

                # --- 检查好友签名变化 ---
                if watch_sign:
                    changed = await self._check_friend_sign(
                        platform_id, auth, persona, now,
                        friend_qq, friend_name, level, fw
                    )
                    if changed:
                        result["sign_changes"] += 1

                fw.last_check_ts = now.timestamp()

                # 好友之间间隔 1-3 秒，避免请求过快
                if len(watch_list) > 1:
                    await asyncio.sleep(random.uniform(1, 3))

            except Exception as e:
                logger.error(f"[SoulMoments] [{platform_id}] 监测好友 {friend_qq} 时出错: {e}")

        self._save_states()
        logger.info(
            f"[SoulMoments] [{platform_id}] 好友空间监测完成: "
            f"检查 {result['scanned']} 人, 发现新说说 {result['new_moments']} 条, "
            f"跳过旧说说 {result['skipped_old']} 条, "
            f"互动 {result['actions']} 次, 签名变化 {result['sign_changes']} 次"
        )
        return result

    async def _process_friend_moments(
        self, platform_id: str, auth: dict, persona: str, now: datetime,
        friend_qq: str, friend_name: str, level: str,
        moments: List[dict], fw: FriendWatchState,
    ) -> Tuple[int, int, int]:
        """处理好友的新说说。

        返回: (互动数量, 新说说数量, 跳过的旧说说数量)
        """
        actions_done = 0
        new_count = 0
        skipped_old = 0

        # 首次扫描：只记录基线（最新说说的 tid 和时间），不做任何互动
        if not fw.last_seen_tid:
            newest = moments[0] if moments else None
            if newest:
                fw.last_seen_tid = newest["tid"]
                fw.last_seen_ts = newest.get("created_time", 0)
                # 把所有已有的说说都标记为"已看过"，防止下次当新说说处理
                for m in moments:
                    if m["tid"] and m["tid"] not in fw.interacted_tids:
                        fw.interacted_tids.append(m["tid"])
            logger.info(
                f"[SoulMoments] [{platform_id}] 好友 {friend_name}({friend_qq}) 首次扫描，"
                f"记录基线: 最新tid={fw.last_seen_tid}, 标记 {len(moments)} 条为已读"
            )
            return 0, 0, len(moments)

        for moment in moments:
            tid = moment["tid"]
            content = moment.get("content", "")

            # 唯一的判断标准：这条说说我"看过"没有？
            # 看过 = tid 在 interacted_tids 里
            if tid in fw.interacted_tids:
                skipped_old += 1
                continue

            if not content:
                fw.interacted_tids.append(tid)
                skipped_old += 1
                continue

            # 🎉 这是一条真正的新说说！
            new_count += 1

            # 让 LLM 判断如何反应
            decision = await self._judge_friend_moment(
                platform_id, persona, now,
                friend_name, level, content
            )
            if not decision:
                continue

            action = decision.get("action", "ignore")
            logger.info(
                f"[SoulMoments] [{platform_id}] 好友 {friend_name}({friend_qq}) "
                f"说说「{content[:30]}...」→ 决定: {action}"
            )

            if action == "like":
                success = await self._like_qzone_moment(platform_id, auth, friend_qq, tid)
                if success:
                    fw.total_likes += 1
                    actions_done += 1
                    logger.info(f"[SoulMoments] [{platform_id}] 👍 已点赞好友 {friend_name} 的说说")

            elif action == "comment":
                comment_text = decision.get("comment_text", "")
                if comment_text:
                    success = await self._comment_friend_moment(
                        platform_id, auth, friend_qq, tid, comment_text
                    )
                    if success:
                        fw.total_comments += 1
                        actions_done += 1
                        logger.info(f"[SoulMoments] [{platform_id}] 💬 已评论好友 {friend_name}: {comment_text[:40]}")

            elif action == "chat":
                chat_text = decision.get("chat_text", "")
                if chat_text:
                    await self._send_private_message(platform_id, friend_qq, chat_text)
                    fw.total_chats += 1
                    actions_done += 1
                    logger.info(f"[SoulMoments] [{platform_id}] 📨 已私聊好友 {friend_name}: {chat_text[:40]}")

            # 标记已互动
            fw.interacted_tids.append(tid)

            # 互动之间间隔
            if action != "ignore":
                await asyncio.sleep(random.uniform(2, 5))

        # 更新最新看到的说说 ID 和时间戳
        if moments:
            fw.last_seen_tid = moments[0]["tid"]
            fw.last_seen_ts = moments[0].get("created_time", 0) or fw.last_seen_ts

        logger.info(
            f"[SoulMoments] [{platform_id}] 好友 {friend_name}({friend_qq}): "
            f"获取 {len(moments)} 条说说, 新 {new_count} 条, 跳过旧 {skipped_old} 条, 互动 {actions_done} 次"
        )
        return actions_done, new_count, skipped_old

    async def _check_friend_sign(
        self, platform_id: str, auth: dict, persona: str, now: datetime,
        friend_qq: str, friend_name: str, level: str, fw: FriendWatchState,
    ) -> bool:
        """检查好友签名是否变化。返回 True 表示发现了变化。"""
        new_sign = await self._fetch_friend_sign(platform_id, auth, friend_qq)
        if new_sign is None:
            return False  # 获取失败

        old_sign = fw.last_seen_sign

        # 首次记录或没变化
        if not old_sign:
            fw.last_seen_sign = new_sign
            return False
        if new_sign == old_sign:
            return False

        logger.info(
            f"[SoulMoments] [{platform_id}] 好友 {friend_name}({friend_qq}) "
            f"签名变化: 「{old_sign}」→「{new_sign}」"
        )
        fw.last_seen_sign = new_sign

        # 让 LLM 判断是否要私聊
        decision = await self._judge_friend_sign_change(
            platform_id, persona, now,
            friend_name, level, old_sign, new_sign
        )
        if not decision:
            return True

        action = decision.get("action", "ignore")
        if action == "chat":
            chat_text = decision.get("chat_text", "")
            if chat_text:
                await self._send_private_message(platform_id, friend_qq, chat_text)
                fw.total_chats += 1
                logger.info(
                    f"[SoulMoments] [{platform_id}] 📨 因签名变化私聊 {friend_name}: {chat_text[:40]}"
                )

        return True

    # ============================================================
    # Phase 2: 好友空间 API
    # ============================================================

    async def _fetch_friend_sign(self, platform_id: str, auth: dict, friend_qq: str) -> Optional[str]:
        """通过 NapCat 获取好友的个性签名"""
        try:
            bot = auth.get("bot")
            if not bot:
                return None
            info = await bot.call_action(
                action="get_stranger_info",
                user_id=int(friend_qq),
                no_cache=True,
            )
            if not info or not isinstance(info, dict):
                return None
            return info.get("longNick", info.get("sign", "")) or ""
        except Exception as e:
            logger.warning(f"[SoulMoments] [{platform_id}] 获取好友 {friend_qq} 签名失败: {e}")
            return None

    async def _get_friend_nickname(self, platform_id: str, auth: dict, friend_qq: str) -> str:
        """获取好友昵称"""
        try:
            bot = auth.get("bot")
            if not bot:
                return friend_qq
            info = await bot.call_action(
                action="get_stranger_info",
                user_id=int(friend_qq),
                no_cache=False,  # 昵称不需要实时
            )
            if info and isinstance(info, dict):
                return info.get("nickname", info.get("nick", friend_qq)) or friend_qq
        except Exception:
            pass
        return friend_qq

    async def _like_qzone_moment(
        self, platform_id: str, auth: dict, friend_qq: str, tid: str
    ) -> bool:
        """给好友的说说点赞"""
        try:
            api_url = (
                f"https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com"
                f"/cgi-bin/likes/internal_dolike_app?g_tk={auth['g_tk']}"
            )
            headers = self._qzone_headers(auth["cookies"], auth["uin"])
            unikey = f"http://user.qzone.qq.com/{friend_qq}/mood/{tid}"
            form_data = {
                "qzreferrer": f"https://user.qzone.qq.com/{friend_qq}",
                "opuin": auth["uin"],
                "unikey": unikey,
                "curkey": unikey,
                "from": "1",
                "appid": "311",
                "typeid": "0",
                "abstime": str(int(time_module.time())),
                "fid": tid,
                "active": "0",
                "fupdate": "1",
                "format": "json",
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url, headers=headers, data=form_data,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    text = await resp.text()
                    logger.info(f"[SoulMoments] [{platform_id}] 点赞响应: HTTP {resp.status}, {text[:200]}")
                    if resp.status == 200:
                        result = _extract_json_from_jsonp(text)
                        if not result:
                            try:
                                result = json.loads(text)
                            except json.JSONDecodeError:
                                pass
                        if result and (result.get("code") == 0 or result.get("ret") == 0):
                            return True
                        # 有些情况下已经赞过了，也算成功
                        if result and result.get("code") == -10001:
                            return True
                    return False
        except Exception as e:
            logger.error(f"[SoulMoments] [{platform_id}] 点赞异常: {e}")
            return False

    async def _comment_friend_moment(
        self, platform_id: str, auth: dict,
        friend_qq: str, tid: str, content: str,
    ) -> bool:
        """评论好友的说说"""
        try:
            api_url = (
                f"https://user.qzone.qq.com/proxy/domain/taotao.qq.com"
                f"/cgi-bin/emotion_cgi_re_feeds?g_tk={auth['g_tk']}"
            )
            headers = self._qzone_headers(auth["cookies"], auth["uin"])
            form_data = {
                "topicId": f"{friend_qq}_{tid}",
                "content": content,
                "feedsType": "100",
                "hostUin": friend_qq,
                "uin": auth["uin"],
                "format": "json",
                "ref": "feeds",
                "qzreferrer": f"https://user.qzone.qq.com/{friend_qq}",
            }

            logger.info(f"[SoulMoments] [{platform_id}] 评论好友说说: qq={friend_qq}, 内容={content[:40]}")

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url, headers=headers, data=form_data,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    text = await resp.text()
                    logger.info(f"[SoulMoments] [{platform_id}] 评论响应: HTTP {resp.status}, {text[:200]}")
                    if resp.status == 200:
                        result = _extract_json_from_jsonp(text)
                        if not result:
                            try:
                                result = json.loads(text)
                            except json.JSONDecodeError:
                                pass
                        if result and (result.get("code") == 0 or result.get("subcode") == 0):
                            return True
                        if "succ" in text.lower() or '"code":0' in text:
                            return True
                    return False
        except Exception as e:
            logger.error(f"[SoulMoments] [{platform_id}] 评论好友说说异常: {e}")
            return False

    async def _send_private_message(self, platform_id: str, target_qq: str, text: str):
        """给好友发私聊消息"""
        try:
            umo = f"{platform_id}:FriendMessage:{target_qq}"
            chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"[SoulMoments] [{platform_id}] 发送私聊失败({target_qq}): {e}")

    # ============================================================
    # Phase 2: LLM 判断 prompts
    # ============================================================

    async def _judge_friend_moment(
        self, platform_id: str, persona: str, now: datetime,
        friend_name: str, level: str, moment_content: str,
    ) -> Optional[dict]:
        """让 LLM 判断如何回应好友的说说"""
        level_desc = {"close": "亲密好友（你很在意ta）", "normal": "普通朋友", "casual": "点头之交（不太熟）"}
        now_str = now.strftime("%Y年%m月%d日 %H:%M")

        prompt = f"""你正在刷 QQ 空间，看到了好友的新动态。

当前时间：{now_str}

你的人格设定：
{persona if persona else "（无特定设定，做你自己）"}

好友信息：
- 昵称: {friend_name}
- 你和 ta 的关系: {level_desc.get(level, "普通朋友")}

{friend_name} 发了一条新说说：
「{moment_content}」

请以你的角色身份判断：你会怎么做？

选项：
1. "ignore" — 划走，不感兴趣/跟你没关系
2. "like" — 点个赞
3. "comment" — 评论一下（要附上评论内容）
4. "chat" — 这条动态让你想私聊对方（要附上私聊内容）

考虑因素：
- 你的性格决定你的社交习惯（有人爱点赞，有人从不互动）
- 关系亲密度影响你的关注程度和反应
- 内容是否和你有关、是否引起你的兴趣
- 做真实的自己，不要刻意讨好

请严格按以下 JSON 格式回答，不要输出任何其他内容：
```json
{{
  "action": "ignore/like/comment/chat",
  "comment_text": "评论内容（仅 action=comment 时填写，其他填空字符串）",
  "chat_text": "私聊内容（仅 action=chat 时填写，其他填空字符串）",
  "reason": "一句话说明判断依据"
}}
```"""

        result_text = await self._generate_content(prompt, platform_id, max_len=500)
        if not result_text:
            return None
        return self._parse_json_response(result_text)

    async def _judge_friend_sign_change(
        self, platform_id: str, persona: str, now: datetime,
        friend_name: str, level: str, old_sign: str, new_sign: str,
    ) -> Optional[dict]:
        """让 LLM 判断好友签名变化后是否要私聊"""
        level_desc = {"close": "亲密好友（你很在意ta）", "normal": "普通朋友", "casual": "点头之交（不太熟）"}
        now_str = now.strftime("%Y年%m月%d日 %H:%M")

        prompt = f"""你在 QQ 空间注意到好友换了个性签名。

当前时间：{now_str}

你的人格设定：
{persona if persona else "（无特定设定，做你自己）"}

好友信息：
- 昵称: {friend_name}
- 你和 ta 的关系: {level_desc.get(level, "普通朋友")}

签名变化：
- 旧签名：「{old_sign if old_sign else "（空）"}」
- 新签名：「{new_sign if new_sign else "（空）"}」

你会因为这个签名变化去私聊对方吗？

选项：
1. "ignore" — 不管，就是换了个签名而已
2. "chat" — 签名内容触发了你想私聊的念头（要附上私聊内容）

考虑因素：
- 签名是否暗示对方心情不好、发生了什么事
- 你和对方的关系是否亲密到你会注意到签名变化
- 你的性格是否会因此主动关心
- 大多数情况下应该 ignore，只有特别的情况才私聊

请严格按以下 JSON 格式回答，不要输出任何其他内容：
```json
{{
  "action": "ignore/chat",
  "chat_text": "私聊内容（仅 action=chat 时填写，其他填空字符串）",
  "reason": "一句话说明判断依据"
}}
```"""

        result_text = await self._generate_content(prompt, platform_id, max_len=500)
        if not result_text:
            return None
        return self._parse_json_response(result_text)

    def _parse_json_response(self, text: str) -> Optional[dict]:
        """从 LLM 回复中提取 JSON"""
        # 尝试从 ```json ``` 块中提取
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 尝试直接找 JSON 对象
        m = re.search(r"\{[^{}]*\"action\"[^{}]*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        # 整段解析
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
        return None

    # ============================================================
    # LLM 调用
    # ============================================================

    async def _generate_content(self, prompt: str, platform_id: str, max_len: int = 200) -> str:
        """调用 LLM 生成内容"""
        try:
            provider = self._get_provider(platform_id)
            if not provider:
                logger.error(f"[SoulMoments] [{platform_id}] 无法获取 LLM Provider")
                return ""

            resp = await provider.text_chat(prompt=prompt, contexts=[])
            if not resp or not resp.completion_text:
                return ""

            content = resp.completion_text.strip()
            # 去掉引号包裹
            if (content.startswith('"') and content.endswith('"')) or \
               (content.startswith("'") and content.endswith("'")) or \
               (content.startswith("「") and content.endswith("」")):
                content = content[1:-1].strip()

            if len(content) > max_len:
                content = content[:max_len]

            return content

        except Exception as e:
            logger.error(f"[SoulMoments] [{platform_id}] LLM 生成失败: {e}")
            return ""

    # ============================================================
    # 辅助方法
    # ============================================================

    def _get_provider(self, platform_id: str):
        """获取 LLM Provider"""
        provider_id = self._get_cfg("moments_settings", "provider_id", default="")
        if provider_id:
            p = self.context.get_provider_by_id(provider_id)
            if p:
                return p
        umo = f"{platform_id}:FriendMessage:0"
        return self.context.get_using_provider(umo=umo)

    async def _get_persona(self, platform_id: str) -> str:
        """获取角色人格设定"""
        try:
            persona_mgr = getattr(self.context, "persona_manager", None)
            if not persona_mgr:
                return ""
            umo = f"{platform_id}:FriendMessage:0"
            default_persona = await persona_mgr.get_default_persona_v3(umo=umo)
            if default_persona and default_persona.get("prompt"):
                return default_persona["prompt"]
        except Exception as e:
            logger.warning(f"[SoulMoments] [{platform_id}] 获取人格失败: {e}")
        return ""

    # ============================================================
    # 用户命令
    # ============================================================

    @filter.command("说说", alias={"moments", "ss"})
    async def cmd_moments(self, event: AstrMessageEvent):
        """查看/手动操作说说"""
        # CommandFilter 不会修改 event.message_str，需要手动去掉命令名
        raw = event.message_str.strip()
        for prefix in ("说说", "moments", "ss"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        args = raw

        if args == "状态" or args == "status":
            await self._cmd_status(event)

        elif args == "发布" or args == "post":
            await self._cmd_post(event)

        elif args == "扫描" or args == "scan":
            await self._cmd_scan(event)

        elif args == "好友" or args == "friends":
            await self._cmd_friends(event)

        elif args == "关注" or args == "watch":
            await self._cmd_watch(event)

        else:
            yield MessageEventResult().message(
                "📢 说说命令\n"
                "/说说 状态 - 查看今日说说计划和发布记录\n"
                "/说说 发布 - 手动触发发布一条说说\n"
                "/说说 扫描 - 手动触发社交扫描（检查评论+好友空间）\n"
                "/说说 好友 - 查看 Bot 好友列表\n"
                "/说说 关注 - 查看关注好友状态"
            )

    async def _cmd_status(self, event: AstrMessageEvent):
        """查看状态"""
        platform_ids = self._get_platform_ids()
        if not platform_ids:
            await event.send(MessageEventResult().message("📢 未配置任何平台 ID"))
            return

        tz = self._get_cfg("moments_settings", "timezone", default="Asia/Shanghai")
        now = _now_tz(tz)
        now_ts = now.timestamp()
        lines = ["📢 说说状态"]

        for pid in platform_ids:
            st = self._states.get(pid)
            if not st:
                lines.append(f"\n【{pid}】未初始化")
                continue

            remaining = max(0, st.today_plan_count - st.today_posted_count)
            next_ts_list = [t for t in sorted(st.today_plan_times) if t > now_ts]
            next_str = datetime.fromtimestamp(next_ts_list[0]).strftime("%H:%M") if next_ts_list else "无"
            last = st.last_post_content[:30] + "..." if len(st.last_post_content) > 30 else st.last_post_content or "(无)"
            scan_ago = int((now_ts - st.last_scan_ts) / 60) if st.last_scan_ts else -1
            scan_str = f"{scan_ago}分钟前" if scan_ago >= 0 else "从未"

            lines.append(f"\n【{pid}】")
            lines.append(f"  今日计划: {st.today_plan_count} 条，已发 {st.today_posted_count} 条")
            lines.append(f"  下一条: {next_str}")
            lines.append(f"  上一条: {last}")
            lines.append(f"  累计发布: {st.total_posts} 条")
            lines.append(f"  上次扫描: {scan_str}")
            lines.append(f"  累计回复: {st.total_replies} 条")

            # 好友监测统计
            if st.friend_watch:
                total_likes = sum(fw.total_likes for fw in st.friend_watch.values())
                total_comments = sum(fw.total_comments for fw in st.friend_watch.values())
                total_chats = sum(fw.total_chats for fw in st.friend_watch.values())
                lines.append(f"  关注好友: {len(st.friend_watch)} 人")
                lines.append(f"  好友互动: 赞{total_likes} 评论{total_comments} 私聊{total_chats}")

        await event.send(MessageEventResult().message("\n".join(lines)))

    async def _cmd_post(self, event: AstrMessageEvent):
        """手动发布说说"""
        platform_ids = self._get_platform_ids()
        if not platform_ids:
            await event.send(MessageEventResult().message("❌ 未配置任何平台 ID"))
            return

        tz = self._get_cfg("moments_settings", "timezone", default="Asia/Shanghai")
        now = _now_tz(tz)

        await event.send(MessageEventResult().message(f"正在为 {len(platform_ids)} 个 Bot 生成并发布说说..."))

        results = []
        for pid in platform_ids:
            await self._do_post(pid, now)
            st = self._states.get(pid)
            if st and st.last_post_content:
                results.append(f"✅ {pid}: {st.last_post_content[:40]}...")
            else:
                results.append(f"❌ {pid}: 发布失败")

        await event.send(MessageEventResult().message("\n".join(results)))

    async def _cmd_scan(self, event: AstrMessageEvent):
        """手动触发社交扫描"""
        platform_ids = self._get_platform_ids()
        if not platform_ids:
            await event.send(MessageEventResult().message("❌ 未配置任何平台 ID"))
            return

        tz = self._get_cfg("moments_settings", "timezone", default="Asia/Shanghai")
        now = _now_tz(tz)

        # 检查配置
        reply_enabled = self._get_cfg("social_scan_settings", "reply_to_comments", default=True)
        max_posts = self._get_cfg("social_scan_settings", "max_check_posts", default=5)
        await event.send(MessageEventResult().message(
            f"🔍 社交扫描开始\n"
            f"平台: {', '.join(platform_ids)}\n"
            f"回复评论: {'✅开启' if reply_enabled else '❌关闭'}\n"
            f"检查最近: {max_posts} 条说说"
        ))

        results = []
        for pid in platform_ids:
            await self._do_scan(pid, now)
            friend_result = await self._do_friend_scan(pid, now)

            st = self._states.get(pid)

            # 评论回复结果
            parts = []
            parts.append(f"📋 {pid}:")

            # 好友空间监测结果（总是显示，让用户知道监测在工作）
            watch_raw = self._get_cfg("social_scan_settings", "watch_friends", default="")
            watch_list = _parse_watch_friends(watch_raw)
            if watch_list:
                parts.append(
                    f"  👥 好友空间: 检查 {friend_result['scanned']} 人, "
                    f"新说说 {friend_result['new_moments']} 条, "
                    f"旧说说 {friend_result['skipped_old']} 条(已跳过)"
                )
                if friend_result["actions"] > 0:
                    # 统计具体互动类型
                    action_details = []
                    new_likes = sum(fw.total_likes for fw in st.friend_watch.values()) if st else 0
                    new_comments = sum(fw.total_comments for fw in st.friend_watch.values()) if st else 0
                    new_chats = sum(fw.total_chats for fw in st.friend_watch.values()) if st else 0
                    parts.append(f"  ✨ 互动: 点赞{new_likes} 评论{new_comments} 私聊{new_chats}")
                if friend_result["sign_changes"] > 0:
                    parts.append(f"  📝 发现 {friend_result['sign_changes']} 个签名变化")
            else:
                parts.append("  👥 好友空间: 未配置关注好友")

            results.append("\n".join(parts))

        await event.send(MessageEventResult().message(
            "🔍 扫描完成\n" + "\n".join(results) + "\n\n💡 详细日志请查看 Docker logs"
        ))

    async def _cmd_friends(self, event: AstrMessageEvent):
        """查看 Bot 好友列表"""
        platform_ids = self._get_platform_ids()
        if not platform_ids:
            await event.send(MessageEventResult().message("❌ 未配置任何平台 ID"))
            return

        lines = ["👥 Bot 好友列表"]
        for pid in platform_ids:
            try:
                platform = self.context.get_platform_inst(pid)
                if not platform:
                    lines.append(f"\n【{pid}】找不到平台")
                    continue

                bot = platform.get_client()
                if not bot:
                    lines.append(f"\n【{pid}】获取客户端失败")
                    continue

                friend_list = await bot.call_action(action="get_friend_list")
                if not friend_list:
                    lines.append(f"\n【{pid}】好友列表为空")
                    continue

                lines.append(f"\n【{pid}】共 {len(friend_list)} 个好友:")
                for f in friend_list[:50]:  # 最多显示 50 个
                    qq = f.get("user_id", f.get("uin", "?"))
                    nick = f.get("nickname", f.get("nick", "?"))
                    remark = f.get("remark", "")
                    display = f"  {qq} - {nick}"
                    if remark and remark != nick:
                        display += f" ({remark})"
                    lines.append(display)

                if len(friend_list) > 50:
                    lines.append(f"  ... 还有 {len(friend_list) - 50} 个")

            except Exception as e:
                lines.append(f"\n【{pid}】获取好友失败: {e}")

        await event.send(MessageEventResult().message("\n".join(lines)))

    async def _cmd_watch(self, event: AstrMessageEvent):
        """查看关注好友状态"""
        platform_ids = self._get_platform_ids()
        if not platform_ids:
            await event.send(MessageEventResult().message("❌ 未配置任何平台 ID"))
            return

        watch_raw = self._get_cfg("social_scan_settings", "watch_friends", default="")
        watch_list = _parse_watch_friends(watch_raw)
        if not watch_list:
            await event.send(MessageEventResult().message(
                "👀 未配置关注好友\n\n"
                "在插件配置的 social_scan_settings → watch_friends 中添加好友 QQ 号。\n"
                "格式: QQ号:等级，多个用逗号隔开\n"
                "等级: close(亲密) / normal(普通) / casual(随缘)\n"
                "示例: 273845408:close, 123456789:normal"
            ))
            return

        tz = self._get_cfg("moments_settings", "timezone", default="Asia/Shanghai")
        now = _now_tz(tz)
        now_ts = now.timestamp()

        level_emoji = {"close": "❤️", "normal": "👤", "casual": "💤"}
        level_name = {"close": "亲密", "normal": "普通", "casual": "随缘"}

        lines = ["👀 关注好友列表"]

        for pid in platform_ids:
            st = self._states.get(pid)
            lines.append(f"\n【{pid}】")

            for friend_qq, level in watch_list:
                emoji = level_emoji.get(level, "👤")
                lname = level_name.get(level, "普通")
                fw = st.friend_watch.get(friend_qq) if st else None

                if fw:
                    check_ago = int((now_ts - fw.last_check_ts) / 60) if fw.last_check_ts else -1
                    check_str = f"{check_ago}分钟前" if check_ago >= 0 else "从未"
                    sign_str = f"「{fw.last_seen_sign[:20]}...」" if len(fw.last_seen_sign) > 20 else f"「{fw.last_seen_sign}」" if fw.last_seen_sign else "（无）"
                    lines.append(f"  {emoji} {friend_qq} ({lname})")
                    lines.append(f"     签名: {sign_str}")
                    lines.append(f"     上次检查: {check_str}")
                    lines.append(f"     点赞: {fw.total_likes} | 评论: {fw.total_comments} | 私聊: {fw.total_chats}")
                else:
                    lines.append(f"  {emoji} {friend_qq} ({lname}) - 尚未监测")

        await event.send(MessageEventResult().message("\n".join(lines)))
