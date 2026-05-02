# Soul Moments 灵魂说说 - 变更日志

> 维护者：bk的殿下

---

## v2.0.0 (2026-05-02)

### Phase 2：好友空间监测 + 个性化互动

**新功能**
- 自动监测好友 QQ 空间说说：Bot 在扫描时段内自动获取关注好友的最新说说
- LLM 驱动的互动决策：发现新说说后，由 LLM 根据角色人格 + 关系亲密度判断是忽略/点赞/评论/私聊
- 好友签名变化监测：检测好友个性签名变化，LLM 决定是否私聊关心
- 关注等级系统：支持 close（亲密）/ normal（普通）/ casual（随缘）三种关注等级
- 点赞好友说说：调用 QZone `internal_dolike_app` API
- 评论好友说说：复用 `emotion_cgi_re_feeds` API，支持评论好友的说说
- 私聊好友：通过 AstrBot 消息系统直接发送私聊
- `/说说 关注` 命令：查看当前关注的好友列表和互动统计
- `/说说 扫描` 升级：同时扫描自己评论 + 好友空间，反馈详细结果
- `/说说 状态` 升级：显示好友监测统计数据

**核心设计**
- "看过/没看过"判断逻辑：用 `interacted_tids` 列表记录已看过的说说 ID，不依赖时间判断
- 首次扫描建立基线：第一次扫描好友空间时，标记所有现有说说为"已读"，后续只对真正的新说说做出反应
- 关注等级不控制扫描频率，而是传给 LLM 作为判断依据
- 所有好友统一跟随 `scan_schedule` 扫描时段

**QZone API 新增**
- 获取好友说说：`emotion_cgi_msglist_v6`，`uin` 和 `hostuin` 都填好友 QQ 号（关键发现！）
- 点赞说说：`internal_dolike_app`，`unikey` 格式为 `http://user.qzone.qq.com/{好友QQ}/mood/{tid}`
- 好友签名：NapCat `get_stranger_info` → `longNick` 字段

**关键 Bug 修复**
- 修复 `uin` 参数错误导致 API 返回缓存/过期数据的问题（`uin` 必须填目标好友的 QQ 号，不是 Bot 自己的）
- 修复首次扫描将所有旧说说误判为"新说说"的问题（首次扫描现在只建立基线，不触发互动）
- 修复扫描反馈只显示评论回复数、不显示好友扫描详情的问题

**配置项新增**
- `watch_friends`：关注好友列表，支持 `QQ号:等级` 格式（如 `273845408:close, 123456789:normal`）
- `watch_max_posts`：每个好友检查最近几条说说（默认 3）
- `watch_signature`：是否监测好友签名变化（默认开启）

**数据模型**
- 新增 `FriendWatchState` 数据类：记录每个好友的监测状态
- `MomentsState` 新增 `friend_watch` 字段（`Dict[str, FriendWatchState]`）
- 状态持久化完全向下兼容

### 涉及文件
- `main.py`：好友空间 API、LLM 判断 prompt、互动执行、命令扩展（~1860 行）
- `_conf_schema.json`：新增 `watch_friends`、`watch_max_posts`、`watch_signature`
- `metadata.yaml`：版本号 1.1.0 → 2.0.0

---

## v1.1.0 (2026-04-28)

### 社交扫描：回复自己说说的评论

**新功能**
- 时段式社交扫描：像真人一样只在特定时段刷空间（如晚上每10分钟扫一次）
- 自动回复说说评论：发现新评论 → 调 LLM 以角色身份生成回复 → 发布
- 评论回复完全由角色人格驱动，态度和语气由 LLM 自然决定
- `/说说 扫描` 命令手动触发社交扫描（调试用）
- `/说说 好友` 命令查看 Bot 好友列表（为 Phase 2 做准备）

**省 token 设计**
- 非扫描时段 = 0 token
- 扫描到 0 条新评论 = 不调 LLM = 0 token
- Bot 自己的评论自动跳过（防止自言自语无限循环）
- 回复失败也标记为已处理，不重试不浪费 token
- 已回复评论 ID 持久化，重启不会重复回复

**配置项**
- `scan_schedule`：扫描时段，格式 `HH:MM-HH:MM/分钟`，多段逗号隔开
- `reply_to_comments`：回复评论开关
- `max_check_posts`：每次扫描检查最近几条说说
- `watch_friends`：关注好友 QQ 号（Phase 2 预留）

**Bug 修复**
- 修复命令 handler 使用 `MessageChain` 导致 LLM 继续处理消息的问题（改用 `MessageEventResult`）
- 修复 `event.message_str` 未去掉命令名导致子命令无法匹配的问题

**架构改动**
- QZone 认证逻辑抽取为 `_get_qzone_auth()` 复用
- 命令处理拆分为独立方法（`_cmd_status`/`_cmd_post`/`_cmd_scan`/`_cmd_friends`）
- MomentsState 新增字段：`last_scan_ts`、`replied_comment_ids`、`total_replies`
- 心跳日志增加扫描状态

### 涉及文件
- `main.py`：社交扫描核心逻辑、QZone 评论 API、命令重构
- `_conf_schema.json`：新增 `social_scan_settings` 配置区块
- `metadata.yaml`：版本号 1.0.0 → 1.1.0

---

## v1.0.0 (2026-04-28)

### 初始发布：QQ 说说自动发布

**核心功能**
- 每天自动规划发布计划：随机 0-N 条，时间随机分布在活跃时段
- 说说内容完全由角色人格驱动，调用 LLM 生成
- 通过 QZone API (`get_cookies` + HTTP POST) 自动发布
- 只在发布时调一次 LLM，极致省 token
- 多 Bot 支持：`platform_id` 逗号分隔，各自独立状态
- 发布失败自动跳过，不重试不浪费 token

**用户命令**
- `/说说 状态` - 查看今日计划和发布记录
- `/说说 发布` - 手动触发发布

**配置项**
- 平台 ID（多个逗号隔开）、每天条数区间、活跃时段
- 时区、说说最大字数
- 可选专用模型 + 自定义 prompt

**架构**
- 60 秒轮询调度器 + 15 秒启动延迟
- 每日计划系统：每天零点（或首次启动）制定计划
- 状态持久化（JSON 文件，per-platform 字典格式）
- 10 分钟心跳日志

### 涉及文件
- `main.py`：完整的调度、每日计划、LLM 调用、QZone API 发布逻辑
- `metadata.yaml`：插件元信息
- `_conf_schema.json`：WebUI 配置 schema
- `requirements.txt`：无额外依赖（aiohttp 由 AstrBot 环境提供）
- `README.md`：说明文档
- `CHANGELOG.md`：本文件

---

## 技术笔记（给 AI 参考）

### 关键调用链
- `self.context.get_platform_inst("平台ID")` → AiocqhttpAdapter 实例
- `adapter.get_client()` → CQHttp (bot) 对象
- `bot.call_action("get_cookies", domain="qzone.qq.com")` → cookies
- 从 cookies 提取 `skey`/`p_skey`/`uin` → 计算 `g_tk`
- HTTP POST → `https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6`

### g_tk 算法
```python
def _gtk(skey):
    h = 5381
    for c in skey:
        h += (h << 5) + ord(c)
    return h & 0x7FFFFFFF
```

### 文件路径
- 插件目录：`/AstrBot/data/plugins/astrbot_plugin_soul_moments/`（Docker 内）
- 宿主机：`/Users/mzy/astrbot/data/plugins/astrbot_plugin_soul_moments/`
- 状态文件：`plugin_data/astrbot_plugin_soul_moments/astrbot_plugin_soul_moments/moments_states.json`

### 与其他 Soul 系列插件的区别
- **Soulmate** 灵魂伴侣：主动发消息给用户
- **Soul Sign** 灵魂签名：更新自己的 QQ 签名
- **Soul Moments** 灵魂说说：发布 QQ 空间说说/动态
- 三者完全独立，互不依赖
