# AstrBot 错误雷达（error_radar）

> 把 AstrBot 的报错翻译成人话，并在坏掉的时候主动告诉你。

AstrBot 的日志里其实什么都不缺：`astrbot.log` 会记下 429、`invalid session`、插件异常。
但这些信息对使用者不友好 —— 要么淹没在几百行 INFO 里，要么只留下一句英文异常。
错误雷达做的事只有三件：**采下来、归好类、说人话**。

## 它解决什么问题

| 你遇到的 | 日志里的样子 | 之前你看到的 |
| --- | --- | --- |
| 群里机器人不发表情了 | `error: invalid session: aiocqhttp:group:669407365` | 什么都没有，只是「不发」 |
| 回复时好时坏 | `429 - Channel rate limit reached` | 偶尔沉默一下 |
| 装完插件没反应 | `ModuleNotFoundError: No module named 'xxx'` | 插件面板里转圈 |

装上之后，同样的情况会变成一句中文：

```
【错误雷达】会话 ID 格式错误
会话字符串没按 <平台ID>:<FriendMessage|GroupMessage>:<会话ID> 三段式书写，投递被拒。
建议：改用本机平台 ID（不是平台类型）。用 /雷达 群 可以查看群号与正确写法。
编号 3f9a1c2b04｜/雷达 详情 3f9a1c2b04
```

## 三路采集

1. **插件异常** —— 通过 `on_plugin_error` 钩子，拿到插件名、处理器名、完整 traceback 和触发它的会话。
2. **工具失败** —— 通过 `on_llm_tool_respond` 钩子，识别 `error:` / `success:false` 这类「工具没干活但没报错」的静默失败。
3. **核心日志** —— 增量读取 `data/logs/astrbot.log`，只收 WARN/ERROR 及以上，跳过调试噪音。

三类记录会按「分类 + 归一化后的报错文本」去重，同一条错误重复出现只累加次数，
不会把日志刷成一片。记录同时落盘到 `data/plugin_data/astrbot_plugin_error_radar/errors.jsonl`。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/雷达` | 今日概览：按分类计数 + 最近三条 |
| `/雷达 列表 20` | 最近 20 条，带编号 |
| `/雷达 详情 <编号>` | 完整报错、解释、处置建议、触发会话 |
| `/雷达 群` | 列出 bot 所在群与**正确的会话字符串**（跨群发送必备） |
| `/雷达 测试` | 写一条自检告警，验证采集通路 |
| `/雷达 清空` | 清空记录 |

命令默认仅管理员可用（`PermissionType.ADMIN`）。

## 内置归因

目前覆盖：模型限流（429）、认证失败、**会话 ID 格式错误**、依赖缺失、请求超时、
上下文超长、数据库被锁、消息投递失败、文件缺失、图片处理失败。
每条都带「现象 → 解释 → 建议」，未覆盖的异常会标为「未归类」并保留原文。

## 主动推送

在配置里填 `推送目标会话` 后，**新出现**的错误分类会推一条中文摘要过去，同一分类有冷却时间：

```yaml
notify:
  push_to_admin: true
  admin_session: "snowluma:FriendMessage:123456789"
  push_cooldown_seconds: 300
```

`admin_session` 里的前半段是本机**平台 ID**，不是 `aiocqhttp` 这种类型名。
不知道写什么就先发 `/雷达 群`，照着输出抄。

## 会话 ID 提示（可选）

打开 `assist.inject_session_hint` 后，每轮对话会追加一小段规则，告诉模型本机平台 ID
和群号该怎么拼。它能显著减少「让它去群里发消息却报 invalid session」的情况。
不需要就关掉，省 token。

## 安装

```bash
cd AstrBot/data/plugins
git clone <this-repo> astrbot_plugin_error_radar
# 重启 AstrBot，或在 WebUI 插件页重载
```

无第三方依赖，不需要 `pip install`。

## 配置项

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `collect.capture_core_logs` | `true` | 是否监听核心日志 |
| `collect.min_level` | `WARN` | 最低记录等级 |
| `collect.poll_interval_seconds` | `5` | 日志轮询间隔 |
| `collect.backfill_on_start` | `false` | 重载时回读历史日志 |
| `collect.ignore_keywords` | 见默认 | 命中即丢弃，用来压噪音 |
| `notify.push_to_admin` | `false` | 是否主动推送 |
| `notify.admin_session` | 空 | 推送目标会话 |
| `assist.inject_session_hint` | `false` | 是否注入会话 ID 规则 |

## 兼容性

- AstrBot `>= 4.17.0`（依赖 `on_plugin_error` / `on_llm_tool_respond` 钩子）
- 平台无关；`/雷达 群` 的群列表依赖 aiocqhttp 的 `get_group_list`

## License

MIT
