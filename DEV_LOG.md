# Soul Moments 灵魂说说 - 开发日志

> 版本: v2.0.0 | 作者: bk的殿下 | 最后更新: 2026-05-02

---

## 一、插件定位

**核心理念**: 说说就是朋友圈，Bot 也要有社交生活。

Soul Moments 是 Soul 系列中功能最复杂的插件，分三大模块:
1. **自动发说说**: Bot 每天随机发 0-N 条 QQ 空间说说，内容由角色人格驱动
2. **社交扫描**: Bot 像真人一样"刷空间"，检查自己说说下的评论并以角色身份回复
3. **好友空间监测**: Bot 自动刷好友空间，发现新说说/签名变化后，由 LLM 决定是否点赞/评论/私聊

## 二、技术架构

### 文件结构
```
astrbot_plugin_soul_moments/
├── main.py              # 主插件代码（~1860行）
├── metadata.yaml        # 插件元信息
├── _conf_schema.json    # 配置项定义
├── logo.png             # 插件图标
├── requirements.txt     # 依赖: aiohttp
├── CHANGELOG.md         # 变更日志
├── DEV_LOG.md           # 本文件（开发日志）
└── README.md            # 使用说明
```

### 外部依赖
- **aiohttp**: HTTP 客户端，用于直接调用 QZone Web API（NapCat 不提供 QZone 相关的 action）

### 核心流程图

#### 自动发说说
```
插件启动 → initialize()
  ├── 为每个 platform_id 制定今日计划
  │    ├── 随机决定今天发几条（daily_range: "0-2"）
  │    ├── 在活跃时段内随机选择发布时间点
  │    └── 存入 today_plan_times[]
  └── 启动 _scheduler_loop()

每 60 秒 _tick()
  └── today_plan_times 中有到达的时间点？
       └── 是 → _do_post()
            1. 获取角色人格
            2. 构建发说说 prompt
            3. 调 LLM 生成内容
            4. 获取 QZone 认证（cookies → g_tk）
            5. 调 QZone publish API 发布
            6. 更新状态
```

#### 社交扫描
```
每 60 秒 _tick()
  └── _should_scan()? 当前时间在扫描时段内 & 间隔已到?
       └── 是 → _do_scan()
            1. 获取 QZone 认证
            2. 调 QZone msglist API 获取最近 N 条说说（含评论）
            3. 过滤: 去掉自己的评论 + 去掉已回复的评论
            4. 对每条新评论:
               a. 构建回复 prompt（带说说内容 + 评论内容 + 角色人格）
               b. 调 LLM 生成回复
               c. 调 QZone re_feeds API 发布回复
            5. 更新 replied_comment_ids（标记已处理）
```

#### 好友空间监测（Phase 2 新增）
```
每 60 秒 _tick()
  └── _should_scan()? 当前时间在扫描时段内 & 间隔已到?
       └── 是 → _do_friend_scan()
            1. 获取 QZone 认证
            2. 解析 watch_friends 配置（QQ号:关注等级）
            3. 遍历每个关注好友:
               ├── 获取好友最近 N 条说说（_fetch_my_moments 复用，uin=好友QQ）
               ├── 获取好友当前签名（NapCat get_stranger_info）
               ├── 首次扫描? → 建立基线（标记所有现有说说为已读），不触发互动
               ├── 非首次 → 对比 interacted_tids:
               │    ├── tid 在列表中 → 跳过（已看过）
               │    └── tid 不在列表中 → 新说说！
               │         └── 调 LLM 判断: ignore/like/comment/chat
               │              ├── like → 调 QZone 点赞 API
               │              ├── comment → 调 QZone 评论 API
               │              └── chat → 发私聊消息
               └── 签名变化? → 调 LLM 判断是否私聊关心
            4. 更新 FriendWatchState 状态
```

### 关键设计决策

1. **QZone API 而非 NapCat action**
   - NapCat 没有发说说/读说说/回复评论的内置 action
   - 必须直接调用 QZone 的 Web API，通过 NapCat 获取认证 cookies
   - 认证链: `bot.call_action("get_cookies", domain="qzone.qq.com")` → 解析 cookies → 计算 g_tk

2. **多域名容错**
   - QZone API 有多个代理域名，不同域名的限流策略不同
   - `_fetch_my_moments()` 会依次尝试:
     - `https://user.qzone.qq.com/proxy/domain/taotao.qq.com/...`
     - `https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/...`
   - 第一个失败就自动尝试下一个

3. **扫描时段式调度**
   - 不是全天 7×24 扫描，而是模拟真人"刷空间"的习惯
   - 配置格式: `"08:00-09:00/30, 12:00-13:00/15, 19:00-21:00/60"`
   - 含义: 早上每 30 分钟扫一次，午休每 15 分钟，晚上每 60 分钟
   - 非扫描时段完全不检查 → 省 token + 省 API 调用

4. **评论去重机制**
   - 每条评论的唯一 ID = `{说说tid}_{评论者uin}_{评论时间戳}`
   - 已回复的 ID 存入 `replied_comment_ids` 列表（最多保留 500 条）
   - 失败的回复也标记为已处理 → 避免反复重试浪费 token
   - 清理: 如果 API 参数错了导致批量失败，需要手动删 states.json 中的对应 ID

5. **每日计划制**
   - 不是随到随发，而是每天零点制定计划
   - 例如今天决定发 2 条，在 10:23 和 16:45
   - 到了 10:23 才触发 LLM 生成 → 只在需要时才消耗 token
   - 计划为 0 条的日子 = 完全不调 LLM

## 三、QZone API 详解（最关键的技术细节）

### 3.1 认证流程

```python
# 1. 通过 NapCat 获取 cookies
cookies_data = await bot.call_action(action="get_cookies", domain="qzone.qq.com")
cookies_str = cookies_data["cookies"]

# 2. 解析关键字段
# cookies 长这样: "skey=xxx; p_skey=xxx; uin=o1234567890; p_uin=o1234567890; ..."
skey = "xxx"
p_skey = "xxx"    # 优先用 p_skey
uin = "1234567890" # 注意要去掉 "o" 前缀

# 3. 计算 g_tk (CSRF Token)
def _gtk(skey: str) -> int:
    h = 5381
    for c in skey:
        h += (h << 5) + ord(c)
    return h & 0x7FFFFFFF
g_tk = _gtk(p_skey or skey)
```

### 3.2 发布说说 API

```
POST https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6?g_tk={g_tk}

Content-Type: application/x-www-form-urlencoded

表单参数:
  con: "说说内容"          ← 注意: 发布用 "con"
  syn_tweet_verson: "1"
  paramstr: "1"
  who: "1"
  feedversion: "1"
  ver: "1"
  ugc_right: "1"
  to_sign: "0"
  hostuin: "123456789"
  code_version: "1"
  format: "json"
  qzreferrer: "https://user.qzone.qq.com/123456789"

注意:
  - 发布 API 用 taotao.qzone.qq.com 或 taotao.qq.com 都行
  - 内容字段名是 "con"（不是 "content"）
```

### 3.3 获取说说列表 API（⚠️ 踩坑最多的 API）

```
GET https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6?g_tk={g_tk}

⚠️ 域名必须用 taotao.qq.com，不能用 taotao.qzone.qq.com！
⚠️ 必须用 GET 方法 + JSONP 格式！

查询参数:
  uin: "123456789"
  hostuin: "123456789"      ← 必须加
  ftype: "0"
  sort: "0"
  pos: "0"
  num: "5"                  ← 获取条数
  replynum: "100"           ← 每条说说最多返回多少条评论
  callback: "_preloadCallback"
  code_version: "1"
  format: "jsonp"           ← 必须是 jsonp，不是 json
  need_private_comment: "1"
  g_tk: "12345678"          ← 查询参数里也要带

请求头:
  Cookie: (完整 cookies)
  Referer: https://user.qzone.qq.com/{uin}
  User-Agent: (Chrome 浏览器 UA)
  Origin: https://user.qzone.qq.com
  Accept: */*

响应格式 (JSONP):
  _preloadCallback({"code":0,"subcode":0,"message":"succ","msglist":[...]})

解析方法:
  用正则 re.search(r"\{.*\}", text, re.DOTALL) 提取 JSON
```

### 3.4 回复评论 API（⚠️ 第二个大坑）

```
POST https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_re_feeds?g_tk={g_tk}

⚠️ 域名必须用 taotao.qq.com！
⚠️ 内容字段名是 "content"（不是 "con"）！
   → 发布说说用 "con"，回复评论用 "content"，不一样！

Content-Type: application/x-www-form-urlencoded

表单参数:
  topicId: "{owner_uin}_{说说tid}"    ← 格式: 说说所有者QQ号_说说ID
  content: "回复内容"                  ← 注意: 回复用 "content"
  feedsType: "100"
  hostUin: "{owner_uin}"
  uin: "{owner_uin}"                  ← 自己的 QQ 号
  replyUin: "{被回复人uin}"            ← 评论者的 QQ 号
  format: "json"
  ref: "feeds"
  qzreferrer: "https://user.qzone.qq.com/{owner_uin}"
```

### 3.5 获取好友说说 API（Phase 2 新增，⚠️ 最大的坑）

```
GET https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6?g_tk={g_tk}

⚠️ 关键发现: uin 参数必须填目标好友的 QQ 号，不是 Bot 自己的！
⚠️ hostuin 也填好友的 QQ 号！
⚠️ Referer 要改成好友的空间 URL！

查询参数:
  uin: "{好友QQ号}"          ← 不是 Bot 的 QQ！这是最大的坑！
  hostuin: "{好友QQ号}"
  ftype: "0"
  sort: "0"
  pos: "0"
  num: "10"
  replynum: "100"
  callback: "_preloadCallback"
  code_version: "1"
  format: "jsonp"
  need_private_comment: "1"
  g_tk: "{g_tk}"

请求头:
  Cookie: (Bot 的完整 cookies)
  Referer: https://user.qzone.qq.com/{好友QQ号}    ← 好友的空间 URL
  User-Agent: (Chrome 浏览器 UA)
  Origin: https://user.qzone.qq.com

踩坑记录:
  - 最初 uin 填了 Bot 自己的 QQ → API 返回缓存数据（总是 3 条旧说说，4242 字节）
  - 改成好友 QQ 后 → 返回完整数据（7 条说说，11796 字节）
  - 参考了 huanxin996/qzone_api 和 wwwpf/QzoneExporter 两个开源项目确认
```

### 3.6 点赞好友说说 API（Phase 2 新增）

```
POST https://w.qzone.qq.com/cgi-bin/likes/internal_dolike_app?g_tk={g_tk}

Content-Type: application/x-www-form-urlencoded

表单参数:
  unikey: "http://user.qzone.qq.com/{好友QQ}/mood/{说说tid}"
  curkey: "http://user.qzone.qq.com/{好友QQ}/mood/{说说tid}"
  appid: "311"
  typeid: "0"
  fid: "{说说tid}"
  from: "1"
  opuin: "{Bot自己的QQ}"
  format: "json"
```

### 3.7 获取好友签名（Phase 2 新增）

```
通过 NapCat 的 get_stranger_info action:
  bot.call_action("get_stranger_info", user_id=int(好友QQ), no_cache=True)

返回数据中的 longNick 字段就是个性签名。
注意 no_cache=True 确保获取实时数据。
```

### 3.8 域名对照表（核心知识）

| 功能 | 正确域名 | 错误域名 | 错误后果 |
|------|---------|---------|---------|
| 发布说说 | taotao.qzone.qq.com ✅ / taotao.qq.com ✅ | - | 都能用 |
| 获取说说列表 | **taotao.qq.com** ✅ | taotao.qzone.qq.com ❌ | 返回"使用人数过多" |
| 回复评论 | **taotao.qq.com** ✅ | taotao.qzone.qq.com ❌ | 返回各种错误 |
| 点赞说说 | **w.qzone.qq.com** ✅ | - | 专用域名 |

### 3.9 字段名对照表（另一个核心知识）

| 功能 | 内容字段名 | 错误写法 | 错误后果 |
|------|-----------|---------|---------|
| 发布说说 | **con** | content | 说说内容为空 |
| 回复评论 | **content** | con | 返回"您未填入内容"(-10005) |

## 四、配置项说明

| 配置路径 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `enable` | bool | true | 插件总开关 |
| `moments_settings.platform_id` | string | "" | 平台 ID，逗号隔开多个 |
| `moments_settings.daily_range` | string | 0-2 | 每天发几条说说（0-0=不发） |
| `moments_settings.active_hours` | string | 08:00-23:00 | 说说发布的活跃时段 |
| `moments_settings.timezone` | string | Asia/Shanghai | 时区 |
| `moments_settings.max_length` | int | 200 | 说说最大字数 |
| `moments_settings.provider_id` | string | "" | 指定模型 |
| `moments_settings.prompt_override` | text | "" | 自定义 prompt |
| `social_scan_settings.scan_schedule` | string | "" | 扫描时段，留空=不扫描 |
| `social_scan_settings.reply_to_comments` | bool | true | 回复评论开关 |
| `social_scan_settings.max_check_posts` | int | 5 | 每次扫描检查几条说说 |
| `social_scan_settings.watch_friends` | string | "" | 关注好友列表，支持关注等级 |
| `social_scan_settings.watch_max_posts` | int | 3 | 每个好友检查最近几条说说 |
| `social_scan_settings.watch_signature` | bool | true | 是否监测好友签名变化 |

### watch_friends 配置格式详解（Phase 2 新增）

```
格式: QQ号 或 QQ号:关注等级，多个用逗号隔开

关注等级:
  close   = 亲密关注（传给 LLM，影响互动倾向）
  normal  = 普通关注（默认）
  casual  = 随缘关注（LLM 倾向于忽略）

示例:
  "273845408:close, 123456789:normal, 999999:casual"
  "273845408, 123456789"    ← 不写等级默认 normal

注意:
  - 关注等级不控制扫描频率，所有好友统一跟随 scan_schedule
  - 等级只是传给 LLM 作为判断依据，影响"注意到"的概率和反应方式
  - 可用 /说说 好友 查看 Bot 的好友列表和 QQ 号
```

### scan_schedule 配置格式详解

```
格式: "开始时间-结束时间/间隔分钟, ..."

示例:
  "08:00-09:00/30, 12:00-13:00/15, 19:00-21:00/60, 22:00-24:00/30"

含义:
  08:00-09:00 每 30 分钟扫一次（早上起来刷一下空间）
  12:00-13:00 每 15 分钟扫一次（午休时间频繁刷）
  19:00-21:00 每 60 分钟扫一次（晚上闲逛）
  22:00-24:00 每 30 分钟扫一次（睡前刷一波）
```

## 五、所有快捷命令

### /说说 (别名: /moments, /ss)

不带参数时显示命令帮助。

| 子命令 | 作用 | 示例 |
|--------|------|------|
| `/说说 状态` | 查看今日计划、已发数量、下一条时间、累计数据、好友监测统计 | `/ss status` |
| `/说说 发布` | 手动触发发布一条说说（立即调 LLM 生成并发 QZone） | `/moments post` |
| `/说说 扫描` | 手动触发一次完整扫描（评论回复 + 好友空间监测） | `/ss 扫描` |
| `/说说 好友` | 查看 Bot 的 QQ 好友列表（方便配置 watch_friends） | `/ss friends` |
| `/说说 关注` | 查看当前关注的好友列表和互动统计（Phase 2 新增） | `/ss watch` |

### 命令回复示例

**`/说说 状态`**:
```
📢 说说状态

【宋志豪】
  今日计划: 2 条，已发 1 条
  下一条: 16:45
  上一条: 丧B又俾人扣咗，要我去捞佢。
  累计发布: 5 条
  上次扫描: 23分钟前
  累计回复: 3 条
  关注好友: 2 人，累计点赞 5 次，评论 3 次
```

**`/说说 扫描`**:
```
🔍 社交扫描开始
平台: 宋志豪
回复评论: ✅开启
检查最近: 5 条说说
好友监测: ✅开启（2 人）

🔍 扫描完成
✅ 宋志豪: 回复了 1 条评论
👥 好友空间: 发现 1 条新说说，点赞 1，评论 0，私聊 0

💡 详细日志请查看 Docker logs
```

**`/说说 好友`**:
```
👥 Bot 好友列表

【宋志豪】共 12 个好友:
  273845408 - BK的殿下 (小弟)
  123456789 - 张三
  ...
```

**`/说说 关注`**（Phase 2 新增）:
```
👀 好友关注列表

【宋志豪】关注 2 人:
  273845408 (BK的殿下) - 🔥亲密
    已看 5 条说说，点赞 3，评论 1
    上次检查: 15分钟前
  123456789 (张三) - 普通
    已看 2 条说说，点赞 0，评论 0
    上次检查: 15分钟前
```

## 六、状态持久化

**文件路径**: `data/plugin_data/astrbot_plugin_soul_moments/astrbot_plugin_soul_moments/moments_states.json`

```json
{
  "宋志豪": {
    "today_date": "2026-05-02",
    "today_plan_count": 2,
    "today_plan_times": [1777380180.0, 1777401900.0],
    "today_posted_count": 1,
    "total_posts": 5,
    "last_post_content": "丧B又俾人扣咗，要我去捞佢。",
    "last_post_ts": 1777380180.0,
    "last_scan_ts": 1777394283.0,
    "replied_comment_ids": [
      "c55fc6e1465cf069cbce0800_273845408_1777360426",
      "c55fc6e1465cf069cbce0800_273845408_1777393744"
    ],
    "total_replies": 2,
    "friend_watch": {
      "273845408": {
        "qq": "273845408",
        "last_seen_tid": "abc123def456",
        "last_seen_sign": "今天也要加油鸭",
        "last_check_ts": 1777394283.0,
        "interacted_tids": ["abc123def456", "xyz789ghi012"],
        "total_likes": 3,
        "total_comments": 1
      },
      "123456789": {
        "qq": "123456789",
        "last_seen_tid": "tid999888",
        "last_seen_sign": "",
        "last_check_ts": 1777394283.0,
        "interacted_tids": ["tid999888"],
        "total_likes": 0,
        "total_comments": 0
      }
    }
  }
}
```

### replied_comment_ids 格式
```
{说说tid}_{评论者QQ号}_{评论时间戳}
```
最多保留 500 条。超过 500 条时，`to_dict()` 方法会自动截取最后 500 条。

### interacted_tids 格式（Phase 2 新增）
```
好友说说的 tid（说说唯一 ID）
```
- 首次扫描时，所有现有说说的 tid 都会被加入（建立基线，不触发互动）
- 后续扫描只对不在列表中的 tid 触发 LLM 判断
- 最多保留 100 条，超出时自动截取最新的

### FriendWatchState 字段说明（Phase 2 新增）

| 字段 | 类型 | 说明 |
|------|------|------|
| `qq` | string | 好友 QQ 号 |
| `last_seen_tid` | string | 上次看到的最新说说 ID |
| `last_seen_sign` | string | 上次看到的个性签名 |
| `last_check_ts` | float | 上次检查时间戳 |
| `interacted_tids` | list | 已看过/互动过的说说 ID 列表 |
| `total_likes` | int | 累计点赞次数 |
| `total_comments` | int | 累计评论次数 |

## 七、开发中遇到的问题和修复

### Bug 1: 获取说说列表返回 "使用人数过多"（code=-10000, subcode=-2）

**发现过程**:
1. 发布说说功能正常（用 `taotao.qzone.qq.com`）
2. 获取说说列表（同一域名）始终报错 "使用人数过多"
3. 先尝试换 POST 方法 + `format=json` → 无效
4. 先尝试加 `hostuin` 参数 → 无效

**根因排查**:
- 在 GitHub 搜索了 [GetQzonehistory](https://github.com/LibraHp/GetQzonehistory) 项目
- 发现该项目获取说说用的域名是 `taotao.qq.com`，不是 `taotao.qzone.qq.com`
- `taotao.qzone.qq.com` 是旧域名，QQ 对其有严格限流
- `taotao.qq.com` 是正确的域名，可以正常请求

**修复方案**:
1. 域名改为 `taotao.qq.com`
2. 请求方法改回 GET + JSONP 格式
3. 加了多域名容错（`user.qzone.qq.com/proxy/...` 和 `h5.qzone.qq.com/proxy/...`）
4. 加了更真实的请求头（完整 Chrome UA、Accept: */* 等）

**教训**: 发布和读取 API 虽然路径类似，但域名限制不同！不能因为发布用 A 域名成功，就以为读取也能用 A 域名。

### Bug 2: 回复评论返回 "您未填入内容"（code=-10005, subcode=-4004）

**发现过程**:
1. 获取说说列表修好后，扫描功能可以正确识别新评论
2. LLM 成功生成了回复内容
3. 调用回复 API 报错 "您未填入内容"

**根因排查**:
- 错误信息明确说"未填入内容"，但代码里明明传了 `con: content`
- 经过对比分析发现两个问题:
  1. **域名错误**: 回复 API 也用了 `taotao.qzone.qq.com`，需要改成 `taotao.qq.com`
  2. **字段名错误**: 发布说说的内容字段是 `con`，但回复评论的内容字段是 `content`
  - 发布: `{"con": "内容"}` ← 这个 API 叫 publish_v6
  - 回复: `{"content": "内容"}` ← 这个 API 叫 re_feeds

**修复方案**:
```python
# 修复前:
api_url = ".../taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds?..."
form_data = {"con": content, ...}

# 修复后:
api_url = ".../taotao.qq.com/cgi-bin/emotion_cgi_re_feeds?..."
form_data = {"content": content, "uin": owner_uin, ...}
```

**额外操作**: 失败的回复已被标记为 `replied_comment_ids`，需要手动编辑 `moments_states.json` 删掉那些 ID 才能重新触发回复。

### Bug 3: 插件根本没加载（零日志）

**发现过程**:
1. 把插件文件放入 Docker 的 `data/plugins/` 目录
2. Docker 日志里完全没有 `[SoulMoments]` 或 `[SoulSign]` 任何内容
3. 检查了 AstrBot 数据库 `inactivated_plugins` → 没有被禁用
4. 检查了 `cmd_config.json` 的 `plugin_set` → 是 `["*"]`

**根因排查**:
- 阅读 AstrBot 源码 `star_manager.py` 的 `_get_modules()` 方法
- 发现 AstrBot **只在启动时**扫描 `data/plugins/` 目录
- 我们是在容器运行后才把文件放进去的，所以从未被扫描到

**修复方案**:
```bash
# 1. 清除 __pycache__
find /Users/mzy/astrbot/data/plugins/ -type d -name "__pycache__" -exec rm -rf {} +

# 2. 重启容器
docker restart astrbot
```

### Bug 4: 所有 debug 日志都看不到

**发现过程**: 代码里写了很多 `logger.debug(f"[SoulMoments] ...")` 但 Docker 日志里完全看不到

**根因**: AstrBot 默认 `log_level: "INFO"`，debug 级别的日志不会输出

**修复方案**: 把所有关键诊断日志从 `logger.debug()` 改为 `logger.info()`

**教训**: 在生产环境中，关键的诊断信息（认证结果、API 响应、过滤统计）必须用 `info` 级别。`debug` 级别只用于开发时的细节追踪。

### Bug 5: QQ UIN 格式不一致

**发现过程**: 过滤"自己的评论"时，有时 Bot 自己的评论没被过滤掉

**根因**: QQ cookies 中的 uin 可能带 "o" 前缀（如 `o1234567890`），而评论中的 uin 可能是纯数字 `1234567890`

**修复方案**:
```python
bot_uin_clean = bot_uin.lstrip("o0") if bot_uin.startswith("o") else bot_uin
cmt_uin_clean = cmt_uin.lstrip("o0") if cmt_uin.startswith("o") else cmt_uin
if cmt_uin == bot_uin or cmt_uin_clean == bot_uin_clean:
    # 跳过自己的评论
```

### Bug 6: 获取好友说说返回缓存数据（Phase 2）

**发现过程**:
1. 使用 `_fetch_my_moments()` 获取好友说说，`uin` 填了 Bot 自己的 QQ
2. API 返回的数据始终是 3 条旧说说，响应体固定 4242 字节
3. 无论好友发了多少新说说，返回的数据都不变

**根因排查**:
- 参考了 [huanxin996/qzone_api](https://github.com/huanxin996/qzone_api) 和 [wwwpf/QzoneExporter](https://github.com/wwwpf/QzoneExporter) 两个开源项目
- 发现 `uin` 参数必须填**目标好友的 QQ 号**，不是 Bot 自己的
- `hostuin` 也要填好友的 QQ 号
- `Referer` 头要改成好友的空间 URL

**修复方案**:
```python
# 修复前:
params["uin"] = bot_uin      # ← 错！返回缓存数据
params["hostuin"] = bot_uin

# 修复后:
params["uin"] = friend_qq    # ← 对！返回好友的真实数据
params["hostuin"] = friend_qq
headers["Referer"] = f"https://user.qzone.qq.com/{friend_qq}"
```

**教训**: QZone API 的 `uin` 参数含义不统一。发布说说时 `uin` 是自己，获取别人说说时 `uin` 是目标人。

### Bug 7: 首次扫描将所有旧说说误判为"新说说"（Phase 2）

**发现过程**:
1. 配置好 `watch_friends` 后第一次扫描
2. Bot 把好友所有现有说说都当成"新说说"，对每条都调 LLM 判断
3. 导致大量不必要的互动（点赞/评论了好友一周前的旧说说）

**根因**: 首次扫描时 `interacted_tids` 为空，所有说说都不在列表中，全部被判定为"新"

**修复方案**:
```python
if not state.interacted_tids:
    # 首次扫描：建立基线，标记所有现有说说为"已读"
    for moment in moments:
        state.interacted_tids.append(moment["tid"])
    logger.info(f"首次扫描 {friend_qq}，建立基线: {len(moments)} 条说说标记为已读")
    return  # 不触发任何互动
```

**教训**: 任何"对比变化"的功能，首次运行时都需要一个"建立基线"的步骤，否则会把所有现有数据当成"新增"。

### Bug 8: 扫描反馈只显示评论回复数（Phase 2）

**发现过程**: `/说说 扫描` 命令执行后，只显示"回复了 X 条评论"，不显示好友空间扫描的结果

**根因**: `_cmd_scan()` 方法只收集了 `_do_scan()` 的结果，没有收集 `_do_friend_scan()` 的结果

**修复方案**: 让 `_do_friend_scan()` 返回扫描统计（发现新说说数、点赞数、评论数、私聊数），在 `_cmd_scan()` 中合并显示

## 八、调试指南

### 检查插件是否正常运行
```bash
docker logs -f astrbot --tail 200 | grep "\[SoulMoments\]"
```

### 正常的日志流程
```
[SoulMoments] 插件已启动 v2.0.0，管理 1 个 Bot: 宋志豪，扫描时段: 08:00-09:00/30, ...
[SoulMoments] 调度器已启动
[SoulMoments] [宋志豪] 今日计划: 1 条说说，时间: 14:23

--- 心跳（每 10 分钟）---
[SoulMoments] ❤️ 心跳 | 宋志豪: 剩1条, 下次14:23, 扫描从未, 关注2人

--- 发布说说 ---
[SoulMoments] [宋志豪] 到达发布时间，开始生成说说...
[SoulMoments] [宋志豪] QZone 认证成功: uin=123456789, g_tk=12345678
[SoulMoments] [宋志豪] ✅ 说说已发布: 今日份嘅廢話...

--- 社交扫描（评论回复）---
[SoulMoments] [宋志豪] 到达扫描时间，开始社交扫描...
[SoulMoments] [宋志豪] QZone 认证成功: uin=123456789, g_tk=12345678
[SoulMoments] [宋志豪] 开始获取最近 5 条说说...
[SoulMoments] [宋志豪] 说说列表响应(...): HTTP 200, 长度 8234
[SoulMoments] [宋志豪] API 返回 5 条说说原始数据
[SoulMoments] [宋志豪] 获取到 5 条说说, 共 3 条评论
[SoulMoments] [宋志豪] Bot UIN: 123456789, 已回复 2 条历史评论
[SoulMoments] [宋志豪] 评论过滤: 新评论 1, 跳过自己 0, 已回复 2
[SoulMoments] [宋志豪] 发现 1 条新评论，开始处理...
[SoulMoments] [宋志豪] 回复评论: to=273845408, 内容=你搞乜啊？...
[SoulMoments] [宋志豪] 回复评论响应: HTTP 200, {"code":0,...}
[SoulMoments] [宋志豪] ✅ 回复 BK的殿下: 你搞乜啊？...
[SoulMoments] [宋志豪] 扫描完成: 1 条回复成功, 0 条失败

--- 好友空间监测（Phase 2 新增）---
[SoulMoments] [宋志豪] 开始好友空间扫描，关注 2 人...
[SoulMoments] [宋志豪] 检查好友 273845408...
[SoulMoments] [宋志豪] 好友 273845408 说说列表: HTTP 200, 长度 11796
[SoulMoments] [宋志豪] 好友 273845408: 获取到 7 条说说，其中 1 条是新的
[SoulMoments] [宋志豪] 首次扫描 273845408，建立基线: 7 条说说标记为已读
[SoulMoments] [宋志豪] 好友 273845408 签名: "今天也要加油鸭"（未变化）
[SoulMoments] [宋志豪] LLM 判断好友 273845408 新说说: action=like, reason=随手点个赞
[SoulMoments] [宋志豪] ✅ 点赞好友 273845408 说说 tid=abc123
[SoulMoments] [宋志豪] 好友空间扫描完成: 新说说 1, 点赞 1, 评论 0, 私聊 0
```

### 常见问题排查

| 现象 | 可能原因 | 检查方法 |
|------|---------|---------|
| 没有日志 | 插件没加载 | `docker restart astrbot` |
| 认证失败 | cookies 过期 | 检查 NapCat 是否正常登录 |
| "使用人数过多" | 域名错误 | 确认用的是 `taotao.qq.com` |
| "您未填入内容" | 字段名错误 | 确认回复 API 用 `content` 不是 `con` |
| 扫描不触发 | scan_schedule 配置空/格式错 | 检查配置格式 |
| 心跳正常但不发说说 | today_plan_count=0 | 看心跳日志的"剩X条" |
| 评论没被检测到 | replied_comment_ids 已包含 | 手动检查/清理状态文件 |
| 好友说说返回缓存数据 | uin 参数填了 Bot 自己 | 确认 uin 填的是好友 QQ 号 |
| 首次扫描大量互动 | 没有建立基线 | 检查 interacted_tids 是否为空 |
| 好友签名检测不到变化 | NapCat 返回缓存 | 确认 no_cache=True |
| watch_friends 不生效 | 配置格式错误 | 检查逗号分隔和等级拼写 |

### 手动调试 QZone API（不通过插件）

如果需要直接测试 API 是否通，可以在 Docker 容器里用 curl:
```bash
# 先进入容器
docker exec -it astrbot bash

# 测试获取说说列表
curl -v "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6?g_tk=YOUR_GTK&uin=YOUR_UIN&hostuin=YOUR_UIN&ftype=0&sort=0&pos=0&num=5&replynum=100&callback=_preloadCallback&code_version=1&format=jsonp&need_private_comment=1" \
  -H "Cookie: YOUR_COOKIES" \
  -H "Referer: https://user.qzone.qq.com/YOUR_UIN" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
```

## 九、依赖的 AstrBot / NapCat API

| API | 用途 |
|-----|------|
| `context.get_platform_inst(id)` | 获取平台实例 |
| `platform.get_client()` | 获取 NapCat bot 客户端 |
| `bot.call_action("get_cookies", domain="qzone.qq.com")` | 获取 QZone 认证 cookies |
| `bot.call_action("get_friend_list")` | 获取好友列表 |
| `bot.call_action("get_stranger_info", user_id=int(qq), no_cache=True)` | 获取好友签名（Phase 2） |
| `context.get_provider_by_id(id)` | 获取指定 LLM Provider |
| `context.get_using_provider(umo)` | 获取默认 LLM Provider |
| `context.persona_manager.get_default_persona_v3(umo)` | 获取角色人格设定 |

## 十、版本历史

- **v1.0.0**: 自动发说说功能（每日计划、时段控制、LLM 生成）
- **v1.1.0**: 社交扫描（获取评论、LLM 生成回复、多域名容错、扫描时段调度）
- **v2.0.0**: 好友空间监测（关注好友说说/签名变化、LLM 驱动互动决策、点赞/评论/私聊、关注等级系统）

## 十一、未来计划

- ~~**Phase 2**: 监测关注好友的说说/签名变化，LLM 决定是否点赞/评论/私聊~~ ✅ 已完成 (v2.0.0)
- **Phase 3**: 说说配图功能
  - 支持 LLM 生成图片描述 → 调图片生成 API → 带图发说说
- 图片下载和转发能力
- 群空间说说支持

## 十二、从零开始部署检查清单

如果要在新环境部署这个插件，按以下顺序检查:

1. ✅ AstrBot >= v4.18.0 (Docker)
2. ✅ NapCat 正常运行，QQ 已登录
3. ✅ `data/plugins/astrbot_plugin_soul_moments/` 目录下有 `main.py`
4. ✅ `requirements.txt` 中的 `aiohttp` 已安装（或在容器内 `pip install aiohttp`）
5. ✅ `docker restart astrbot` 确保插件被加载
6. ✅ AstrBot 后台配置中：
   - `moments_settings.platform_id` 填了正确的平台 ID
   - `social_scan_settings.scan_schedule` 填了扫描时段
   - `social_scan_settings.watch_friends` 填了关注好友（如需好友监测）
   - `social_scan_settings.watch_max_posts` 设置合理（默认 3）
   - `social_scan_settings.watch_signature` 按需开关（默认开启）
7. ✅ 检查日志: `docker logs -f astrbot --tail 100 | grep "\[SoulMoments\]"`
8. ✅ 手动测试: 在 QQ 对话中发送 `/说说 状态`
9. ✅ 手动测试: 发送 `/说说 扫描` 检查社交扫描 + 好友监测是否正常
10. ✅ 手动测试: 发送 `/说说 关注` 确认好友关注列表正确
