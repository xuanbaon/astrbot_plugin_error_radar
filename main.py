"""错误雷达 —— 把 AstrBot 的报错翻译成人话。

三路采集：插件异常（on_plugin_error）、工具调用失败（on_llm_tool_respond）、
核心日志里的 WARN/ERROR。统一归类、去重、落盘，并提供 /雷达 命令与主动推送。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import traceback
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain

PLUGIN_NAME = "astrbot_plugin_error_radar"

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

# (正则, 分类, 中文解释, 处置建议)
RULES: list[tuple[str, str, str, str]] = [
    (
        r"429|rate.?limit|调用过于频繁|Channel rate limit",
        "模型限流",
        "模型通道被限流（429），请求被上游拒绝。",
        "换 Key / 降低并发；或添加备用 Provider 并在配置里开启自动轮询。",
    ),
    (
        r"401|403|Unauthorized|invalid api key|鉴权失败|api key.*invalid",
        "认证失败",
        "模型或平台鉴权失败，Key 无效或没有该模型权限。",
        "到「服务提供商」检查 Key 与 Base URL，确认账号有该模型额度。",
    ),
    (
        r"invalid session|不合法的 session|failed to find platform for session",
        "会话 ID 格式错误",
        "会话字符串没按 <平台ID>:<FriendMessage|GroupMessage>:<会话ID> 三段式书写，投递被拒。",
        "改用本机平台 ID（不是平台类型）。用 /雷达 群 可以查看群号与正确写法。",
    ),
    (
        r"No module named|ModuleNotFoundError|ImportError",
        "依赖缺失",
        "插件需要的 Python 包没装。",
        "在 AstrBot 的数据目录执行 pip 安装对应包，然后重载插件。",
    ),
    (
        r"Timeout|timed out|超时|ReadTimeout|ConnectTimeout",
        "请求超时",
        "上游响应太慢或网络不通。",
        "检查代理 / DNS；把超时时间调大；境外 API 建议走稳定代理。",
    ),
    (
        r"context_length|maximum context|token.*(exceed|limit)|超出上下文",
        "上下文超长",
        "本轮上下文超过模型窗口。",
        "减少注入的记忆/人格提示，或换长上下文模型。",
    ),
    (
        r"database is locked|sqlite3\.OperationalError",
        "数据库被锁",
        "SQLite 并发写入冲突。",
        "降低并发；把 data 目录放到本地 NTFS/EXT4，别放网络盘。",
    ),
    (
        r"failed to send|发送失败|send.*error|Not connected|websocket.*(closed|error)|连接.*断开",
        "消息投递失败",
        "消息没能发到 QQ / 平台侧。",
        "确认 OneBot 实现（如 SnowLuma/NapCat）在线；查群是否被禁言、机器人是否被踢。",
    ),
    (
        r"FileNotFoundError|no such file|文件不存在|路径不存在",
        "文件缺失",
        "引用的本地文件/素材不存在（常见于表情包、参考图被移动）。",
        "核对素材路径；重装对应素材包；回档到文件还在的快照。",
    ),
    (
        r"image|图片|media|媒体" ,  # 兜底图片相关，仅在没有更强分类时命中
        "图片处理失败",
        "图片下载/转换/上传环节出错。",
        "检查图片是否损坏、体积是否超限、图床是否可用。",
    ),
]

IGNORE_DEFAULT = [
    "has no speech-to-text provider",
    "DeprecationWarning",
    "Executed handler",
    "可忽略",
    "Provider 暂时不可用",
    "prometheus_client 不可用",
    "cachetools 未安装",
    "未指定任何Provider",
    "PostgreSQL 启动失败",
    "未配置 provider_id",
]

LOG_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[^\]]*)\]\s+"
    r"\[(?P<star>[^\]]*)\]\s+\[(?P<level>[A-Z]+)\]\s+"
    r"(?:\[[^\]]*\]\s+)?\[(?P<module>[^\]]*)\]:\s*(?P<message>.*)$"
)
NORMALIZE_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{20,}|\b\d{6,}\b|\d+\.\d+")


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _classify(text: str) -> tuple[str, str, str]:
    for pattern, category, explain, advice in RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return category, explain, advice
    return "未归类", "出现了一个未被规则覆盖的异常。", "把 /雷达 详情 里的内容发出来排查。"


def _record_id(category: str, summary: str) -> str:
    basis = f"{category}|{NORMALIZE_RE.sub('#', summary)[:200]}"
    return hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()[:10]


class ErrorRadar(Star):
    """把报错采集下来、翻译成人话，并在需要时主动告诉你。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        try:
            self.data_dir: Path = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.data_dir = Path(__file__).resolve().parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.data_dir / "errors.jsonl"
        self._records: OrderedDict[str, dict] = OrderedDict()
        self._lock = asyncio.Lock()
        self._tail_task: asyncio.Task | None = None
        self._log_offset = 0
        self._push_marks: dict[str, float] = {}
        self._group_cache: tuple[float, list[dict]] = (0.0, [])
        self._load_existing()
        # on_astrbot_loaded 只在进程启动时触发；插件被热重载时也要能起监听。
        self._start_tail()

    # ---------------- 配置读取 ----------------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        return default if value is None else value

    @property
    def _ignore_patterns(self) -> list[str]:
        raw = self._cfg("ignore_keywords", IGNORE_DEFAULT)
        return [str(item) for item in raw if str(item).strip()]

    @property
    def _min_level(self) -> int:
        level = str(self._cfg("min_level", "WARN")).upper()
        return LEVELS.get(level, 30)

    @property
    def _max_records(self) -> int:
        try:
            return max(20, int(self._cfg("max_records", 400)))
        except Exception:
            return 400

    # ---------------- 持久化 ----------------
    def _load_existing(self) -> None:
        if not self.jsonl_path.is_file():
            return
        try:
            lines = self.jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return
        for line in lines[-self._max_records * 2 :]:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict) and item.get("id"):
                self._records[item["id"]] = item
        while len(self._records) > self._max_records:
            self._records.popitem(last=False)

    def _append_disk(self, record: dict) -> None:
        try:
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("[error_radar] 写入错误日志失败", exc_info=True)

    # ---------------- 采集 ----------------
    async def _record(
        self,
        *,
        level: str,
        source: str,
        summary: str,
        detail: str = "",
        session: str = "",
        tool: str = "",
        plugin: str = "",
    ) -> dict | None:
        text = " ".join(str(summary or "").split())
        if not text:
            return None
        haystack = f"{text} {detail}"
        for keyword in self._ignore_patterns:
            if keyword and keyword.lower() in haystack.lower():
                return None
        category, explain, advice = _classify(haystack)
        record_id = _record_id(category, text)
        now = time.time()
        async with self._lock:
            existing = self._records.get(record_id)
            if existing:
                existing["count"] = int(existing.get("count", 1)) + 1
                existing["last_seen"] = now
                existing["last_session"] = session or existing.get("last_session", "")
                self._records.move_to_end(record_id)
                return None
            record = {
                "id": record_id,
                "level": level.upper(),
                "category": category,
                "explain": explain,
                "advice": advice,
                "source": source,
                "plugin": plugin,
                "tool": tool,
                "session": session,
                "summary": text[:600],
                "detail": str(detail or "")[:4000],
                "count": 1,
                "first_seen": now,
                "last_seen": now,
            }
            self._records[record_id] = record
            while len(self._records) > self._max_records:
                self._records.popitem(last=False)
            self._append_disk(record)
        await self._maybe_push(record)
        return record

    def _session_of(self, event: Any) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    @filter.on_plugin_error()
    async def on_plugin_error(
        self,
        event: AstrMessageEvent,
        plugin_name: str,
        handler_name: str,
        error: BaseException,
        traceback_text: str,
    ) -> None:
        try:
            message = f"{type(error).__name__}: {error}"
            await self._record(
                level="ERROR",
                source="plugin_error",
                summary=f"[{plugin_name}] {handler_name} 抛出异常 {message}",
                detail=traceback_text or "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
                session=self._session_of(event),
                plugin=str(plugin_name or ""),
            )
        except Exception:
            logger.debug("[error_radar] on_plugin_error 处理失败", exc_info=True)

    @filter.on_llm_tool_respond()
    async def on_tool_respond(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
        tool_result: Any,
    ) -> None:
        try:
            texts: list[str] = []
            content = getattr(tool_result, "content", None)
            if isinstance(content, list):
                for item in content:
                    chunk = getattr(item, "text", None)
                    if chunk is None and isinstance(item, dict):
                        chunk = item.get("text")
                    if chunk:
                        texts.append(str(chunk))
            blob = "\n".join(texts)
            if not blob:
                return
            lowered = blob.lower()
            failed = (
                lowered.startswith("error")
                or '"success": false' in lowered
                or '"ok": false' in lowered
                or "must_not_claim_sent\": true" in lowered and "\"sent\": false" in lowered
            )
            if not failed:
                return
            await self._record(
                level="WARN",
                source="tool_error",
                summary=f"工具 {getattr(tool, 'name', '?')} 返回失败：{blob[:300]}",
                detail=blob[:4000],
                session=self._session_of(event),
                tool=str(getattr(tool, "name", "") or ""),
            )
        except Exception:
            logger.debug("[error_radar] on_tool_respond 处理失败", exc_info=True)

    def _resolve_log_path(self) -> Path:
        configured = str(self._cfg("log_path", "") or "").strip()
        if configured:
            return Path(configured)
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "logs" / "astrbot.log"
        except Exception:
            return Path(__file__).resolve().parents[2] / "logs" / "astrbot.log"

    async def _tail_logs(self) -> None:
        log_path = self._resolve_log_path()
        interval = max(2, int(self._cfg("poll_interval_seconds", 5) or 5))
        logger.info(f"[error_radar] 监听日志文件：{log_path}")
        while True:
            try:
                if log_path.is_file():
                    size = log_path.stat().st_size
                    if self._log_offset == 0:
                        self._log_offset = size if not self._cfg("backfill_on_start", False) else 0
                    if size < self._log_offset:
                        self._log_offset = 0
                    if size > self._log_offset:
                        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                            handle.seek(self._log_offset)
                            chunk = handle.read()
                            self._log_offset = handle.tell()
                        for line in chunk.splitlines():
                            await self._ingest_log_line(line)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[error_radar] 日志轮询异常", exc_info=True)
            await asyncio.sleep(interval)

    async def _ingest_log_line(self, line: str) -> None:
        match = LOG_LINE_RE.match(line.strip())
        if not match:
            return
        level = match.group("level").upper()
        if LEVELS.get(level, 0) < self._min_level:
            return
        await self._record(
            level=level,
            source="core_log",
            summary=match.group("message")[:400],
            detail=line[:4000],
            plugin=match.group("star"),
            tool=match.group("module"),
        )

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        self._start_tail()

    def _start_tail(self) -> None:
        if not self._cfg("capture_core_logs", True):
            return
        if self._tail_task and not self._tail_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._tail_task = loop.create_task(self._tail_logs())
        logger.info("[error_radar] 已启动核心日志监听")

    # ---------------- 主动推送 ----------------
    async def _maybe_push(self, record: dict) -> None:
        if not self._cfg("push_to_admin", False):
            return
        target = str(self._cfg("admin_session", "") or "").strip()
        if not target or ":" not in target:
            return
        category = record.get("category", "")
        cooldown = max(60, int(self._cfg("push_cooldown_seconds", 300) or 300))
        now = time.time()
        if now - self._push_marks.get(category, 0.0) < cooldown:
            return
        self._push_marks[category] = now
        text = (
            f"【错误雷达】{record['category']}\n"
            f"{record.get('explain', '')}\n"
            f"建议：{record.get('advice', '')}\n"
            f"编号 {record['id']}｜/雷达 详情 {record['id']}"
        )
        try:
            await self.context.send_message(target, MessageChain([Comp.Plain(text)]))
        except Exception:
            logger.debug("[error_radar] 推送失败", exc_info=True)

    # ---------------- 命令 ----------------
    @filter.command("雷达")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def radar(self, event: AstrMessageEvent, sub: str = "", arg: str = ""):
        """错误雷达：查看、定位并解释 AstrBot 的报错。"""
        action = (sub or "").strip()
        if action in {"列表", "list"}:
            yield event.plain_result(self._render_list(arg))
        elif action in {"详情", "show"}:
            yield event.plain_result(self._render_detail(arg))
        elif action in {"群", "groups"}:
            yield event.plain_result(await self._render_groups())
        elif action in {"清空", "clear"}:
            yield event.plain_result(self._clear())
        elif action in {"测试", "test"}:
            yield event.plain_result(await self._self_test())
        else:
            yield event.plain_result(self._render_overview())

    def _render_overview(self) -> str:
        records = list(self._records.values())[-self._max_records :]
        if not records:
            return "错误雷达：暂无记录。用 /雷达 列表 看全部，/雷达 测试 验证通路。"
        today = datetime.now().strftime("%Y-%m-%d")
        todays = [r for r in records if datetime.fromtimestamp(r["last_seen"]).strftime("%Y-%m-%d") == today]
        counter: dict[str, int] = {}
        for record in todays:
            counter[record["category"]] = counter.get(record["category"], 0) + int(record.get("count", 1))
        ranked = sorted(counter.items(), key=lambda kv: -kv[1])[:5]
        lines = [f"错误雷达｜今日 {len(todays)} 类，累计 {sum(counter.values())} 次（库内共 {len(records)} 类）"]
        for category, count in ranked:
            lines.append(f"· {category} ×{count}")
        lines.append("最近：")
        for record in todays[-3:]:
            stamp = datetime.fromtimestamp(record["last_seen"]).strftime("%H:%M")
            lines.append(f"· {stamp} [{record['category']}] {record['summary'][:60]}（{record['id']}）")
        lines.append("详情：/雷达 详情 <编号>　全部：/雷达 列表 20")
        return "\n".join(lines)

    def _render_list(self, arg: str) -> str:
        try:
            limit = max(1, min(50, int(arg or 10)))
        except Exception:
            limit = 10
        records = list(self._records.values())[-limit:]
        if not records:
            return "暂无记录。"
        lines = [f"最近 {len(records)} 条错误（新的在后）："]
        for record in reversed(records):
            stamp = datetime.fromtimestamp(record["last_seen"]).strftime("%m-%d %H:%M")
            lines.append(
                f"{stamp} [{record['category']}]×{record.get('count', 1)} ({record['id']}) "
                f"{record['summary'][:70]}"
            )
        return "\n".join(lines)

    def _render_detail(self, arg: str) -> str:
        key = (arg or "").strip()
        record = self._records.get(key)
        if record is None:
            for item in reversed(list(self._records.values())):
                if key and key in item["summary"]:
                    record = item
                    break
        if record is None:
            return "没找到这条记录。用 /雷达 列表 查看编号。"
        stamp = datetime.fromtimestamp(record["last_seen"]).strftime("%Y-%m-%d %H:%M:%S")
        detail = (record.get("detail") or "").strip()
        if len(detail) > 1500:
            detail = detail[:1500] + "\n…（已截断）"
        return (
            f"编号：{record['id']}｜等级：{record['level']}｜来源：{record['source']}\n"
            f"分类：{record['category']}（累计 {record.get('count', 1)} 次，最后 {stamp}）\n"
            f"现象：{record['summary']}\n"
            f"解释：{record.get('explain', '')}\n"
            f"建议：{record.get('advice', '')}\n"
            f"会话：{record.get('session') or '—'}\n"
            f"——原始信息——\n{detail}"
        )

    def _clear(self) -> str:
        self._records.clear()
        try:
            self.jsonl_path.unlink(missing_ok=True)
        except Exception:
            pass
        return "已清空雷达记录。"

    async def _self_test(self) -> str:
        record = await self._record(
            level="WARN",
            source="self_test",
            summary="自检：这是一条测试告警（invalid session: demo:group:123）",
            detail="self test",
            session="radar:self_test",
        )
        if record:
            return f"已写入一条测试告警（{record['id']}），说明采集与去重正常。"
        return "这条测试告警被当成重复记录合并了，说明去重在工作。"

    async def _collect_groups(self) -> list[dict]:
        now = time.time()
        cached_at, cached = self._group_cache
        if cached and now - cached_at < 300:
            return cached
        groups: list[dict] = []
        try:
            platforms = self.context.platform_manager.platform_insts
        except Exception:
            platforms = []
        for platform in platforms:
            try:
                meta = platform.meta()
                platform_id = str(meta.id or "")
                client = platform.get_client() if hasattr(platform, "get_client") else None
                api = getattr(client, "api", None)
                if api is None or not hasattr(api, "call_action"):
                    continue
                data = await api.call_action("get_group_list")
                for row in data or []:
                    group_id = str(row.get("group_id") or "").strip()
                    if not group_id:
                        continue
                    groups.append(
                        {
                            "platform": platform_id,
                            "group_id": group_id,
                            "name": str(row.get("group_name") or ""),
                            "umo": f"{platform_id}:GroupMessage:{group_id}",
                        }
                    )
            except Exception:
                continue
        self._group_cache = (now, groups)
        return groups

    async def _render_groups(self) -> str:
        groups = await self._collect_groups()
        if not groups:
            return "没读到群列表：确认 QQ 适配器在线，或在配置里手动填 known_groups。"
        lines = [f"共 {len(groups)} 个群，跨会话发送要用最后那列字符串："]
        for item in groups[:40]:
            lines.append(f"· {item['group_id']} {item['name'][:16]} → {item['umo']}")
        lines.append("私聊：<平台ID>:FriendMessage:<QQ号>，平台ID 不是 aiocqhttp 这类类型名。")
        return "\n".join(lines)

    # ---------------- 提示词注入：会话 ID 规则 ----------------
    @filter.on_llm_request()
    async def inject_session_hint(self, event: AstrMessageEvent, req: Any) -> None:
        if not self._cfg("inject_session_hint", False):
            return
        try:
            platform_id = self._session_of(event).split(":", 1)[0] or "unknown"
            groups = await self._collect_groups()
            lines = [
                "【跨会话发送规则】",
                f"本机平台 ID：{platform_id}（不是 aiocqhttp 这种类型名）。",
                "会话字符串必须是 平台ID:FriendMessage|GroupMessage:会话ID，大小写敏感。",
                f"示例：{platform_id}:GroupMessage:<群号> / {platform_id}:FriendMessage:<QQ号>。",
                "把 aiocqhttp:group:xxx、platform:group:xxx 这类写法一律视为错误。",
            ]
            if groups:
                preview = "；".join(f"{item['group_id']}→{item['umo']}" for item in groups[:8])
                lines.append(f"已知群：{preview}。")
            lines.append("其它群号：把群号填进同一模板即可，不要自己发明格式。")
            req.system_prompt = f"{getattr(req, 'system_prompt', '') or ''}\n" + "\n".join(lines)
        except Exception:
            logger.debug("[error_radar] 注入会话提示失败", exc_info=True)

    async def terminate(self) -> None:
        if self._tail_task and not self._tail_task.done():
            self._tail_task.cancel()
