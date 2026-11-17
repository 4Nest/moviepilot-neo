"""工作流资源增量追新基线与发布时间解析工具。"""
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import hashlib
from typing import Iterable, List, Optional, Tuple

from app.core.context import Context, TorrentInfo
from app.db.systemconfig_oper import SystemConfigOper

MAX_SEEN_FINGERPRINTS = 3000
BASELINE_CLOCK_SKEW_MINUTES = 5


def parse_torrent_pubdate(pubdate_str: Optional[str]) -> Optional[datetime]:
    """
    解析种子发布时间字符串为本地 naive datetime。
    兼容标准日期时间、ISO 8601 与 RFC 2822 格式。
    """
    if not pubdate_str or not isinstance(pubdate_str, str):
        return None
    raw = pubdate_str.strip()
    if not raw:
        return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except ValueError:
        pass

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (TypeError, ValueError):
        return None


def build_scope_signature(*parts: object) -> str:
    """根据节点检索范围配置生成短签名，当检索目标变化时自动重建追新基线。"""
    normalized = "|".join(str(part or "").strip() for part in parts)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def torrent_fingerprints(torrent_info: Optional[TorrentInfo]) -> List[str]:
    """
    提取单条种子的稳定去重指纹列表（详情链接指纹 + 标题体积指纹）。
    任一指纹命中即视为已处理存量种子。
    """
    if not torrent_info:
        return []
    site_prefix = str(torrent_info.site or torrent_info.site_name or "rss").strip()
    fps: List[str] = []

    page_url = (torrent_info.page_url or "").strip()
    if page_url:
        fps.append(f"{site_prefix}|url:{page_url}")

    title = (torrent_info.title or "").strip()
    if title:
        size_val = int(torrent_info.size or 0)
        fps.append(f"{site_prefix}|title:{title}|{size_val}")

    if not fps and torrent_info.enclosure:
        fps.append(f"{site_prefix}|enc:{torrent_info.enclosure.strip()}")
    return fps


def filter_incremental_torrents(
    *,
    workflow_id: int,
    action_id: str,
    scope_signature: str,
    torrents: Iterable[Context],
    systemconfig: Optional[SystemConfigOper] = None,
) -> Tuple[List[Context], bool, int]:
    """
    对获取到的种子列表执行增量追新基线过滤，并自动更新持久化水位线。

    - 若当前动作尚未初始化基线（或检索范围签名变更）：将当前存量全部记入基线并返回空列表
      ``([], True, baseline_count)``，避免首次运行全量下载历史旧种。
    - 若已初始化基线：仅放行未见过且发布时间不早于基线时间（含容差）的新资源，
      返回 ``(new_torrents, False, skipped_count)``。
    """
    config_oper = systemconfig or SystemConfigOper()
    workflow_key = f"WorkflowCache-{workflow_id}"
    state_key = f"{action_id}__incremental"

    workflow_cache = config_oper.get(workflow_key)
    if not isinstance(workflow_cache, dict):
        workflow_cache = {}

    raw_list = list(torrents)
    state = workflow_cache.get(state_key)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 首次运行或查询范围（关键字/站点/RSS URL）变更：静默建立存量基线
    if not isinstance(state, dict) or state.get("scope_signature") != scope_signature:
        seen_list: List[str] = []
        seen_set = set()
        for ctx in raw_list:
            for fp in torrent_fingerprints(getattr(ctx, "torrent_info", None)):
                if fp not in seen_set:
                    seen_set.add(fp)
                    seen_list.append(fp)
        if len(seen_list) > MAX_SEEN_FINGERPRINTS:
            seen_list = seen_list[-MAX_SEEN_FINGERPRINTS:]

        workflow_cache[state_key] = {
            "scope_signature": scope_signature,
            "baseline_time": now_str,
            "seen": seen_list,
        }
        config_oper.set(workflow_key, workflow_cache)
        return [], True, len(raw_list)

    # 已存在基线：仅放行未在 seen 中且不早于 baseline_time 的增量种子
    seen_list = list(state.get("seen") or [])
    seen_set = set(seen_list)
    baseline_dt = parse_torrent_pubdate(state.get("baseline_time"))
    cutoff_dt = (
        baseline_dt - timedelta(minutes=BASELINE_CLOCK_SKEW_MINUTES)
        if baseline_dt is not None
        else None
    )

    fresh_torrents: List[Context] = []
    skipped_count = 0
    cache_dirty = False

    for ctx in raw_list:
        t_info = getattr(ctx, "torrent_info", None)
        fps = torrent_fingerprints(t_info)
        if fps and any(fp in seen_set for fp in fps):
            skipped_count += 1
            continue

        pub_dt = parse_torrent_pubdate(getattr(t_info, "pubdate", None))
        if cutoff_dt is not None and pub_dt is not None and pub_dt < cutoff_dt:
            skipped_count += 1
            for fp in fps:
                if fp not in seen_set:
                    seen_set.add(fp)
                    seen_list.append(fp)
                    cache_dirty = True
            continue

        fresh_torrents.append(ctx)
        for fp in fps:
            if fp not in seen_set:
                seen_set.add(fp)
                seen_list.append(fp)
                cache_dirty = True

    if cache_dirty:
        if len(seen_list) > MAX_SEEN_FINGERPRINTS:
            seen_list = seen_list[-MAX_SEEN_FINGERPRINTS:]
        state["seen"] = seen_list
        workflow_cache[state_key] = state
        config_oper.set(workflow_key, workflow_cache)

    return fresh_torrents, False, skipped_count
