#!/usr/bin/env python
"""Seek permitted course videos and scroll courseware; never interact with quizzes."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from playwright.sync_api import BrowserContext, Frame, Page, TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError as exc:  # pragma: no cover
    raise SystemExit("未找到 Python Playwright；脚本不会自动安装依赖。") from exc

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from scripts import lesson_inventory as inventory  # noqa: E402


DEFAULT_INVENTORY = WORKSPACE / "output" / "lesson-inventory" / "lesson-inventory.json"
DEFAULT_PROFILE = WORKSPACE / ".private" / "chaoxing-profile"
DEFAULT_OUTPUT = WORKSPACE / "output" / "lesson-completion"
LOCK_MARKER_RE = inventory.VIDEO_DURATION_REQUIREMENT_RE

VIDEO_TRACK_SELECTORS = (
    ".vjs-progress-holder",
    ".xgplayer-progress-outer",
    ".xgplayer-progress",
    ".dplayer-bar-wrap",
    ".plyr__progress",
    ".jw-slider-time",
    ".video-progress",
    'input[type="range"]',
    '[role="slider"]',
)


def action_for_resource_kind(kind: str) -> str | None:
    return {"video": "seek", "courseware": "scroll"}.get(kind)


def seek_drag_points(box: dict[str, float], current_fraction: float) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Return safe centerline coordinates for a mouse drag across a seek track."""
    try:
        x, y = float(box["x"]), float(box["y"])
        width, height = float(box["width"]), float(box["height"])
        fraction = float(current_fraction)
    except (KeyError, TypeError, ValueError):
        return None
    if width < 40 or height < 2 or height > 64 or not 0 <= fraction <= 1:
        return None
    start_x = round(x + width * min(max(fraction, 0.02), 0.90)) + 1
    end_x = round(x + width - 2)
    center_y = round(y + height / 2)
    if end_x - start_x < 8:
        return None
    return (float(start_x), float(center_y)), (float(end_x), float(center_y))


def load_inventory(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("清单没有可处理的小节")
    if not data.get("course", {}).get("entry_url"):
        raise ValueError("清单缺少课程入口 URL")
    for section in sections:
        if not section.get("section_id"):
            raise ValueError("清单中存在缺少 section_id 的小节")
    return data


def timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def resource_url_for_report(url: str) -> str:
    """Omit query strings: viewer URLs can hide session tokens inside encoded values."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme and parts.netloc else parts.path


def initial_course_page_url(entry_url: str) -> str:
    """Keep a known lesson page; only derive the course list from non-lesson URLs."""
    if "studentstudy" in urlsplit(entry_url).path.lower():
        return entry_url
    return inventory.candidate_outline_url(entry_url)


def section_url_for_id(entry_url: str, section_id: str) -> str:
    """Construct a lesson URL from the inventory ID and the known course context."""
    return inventory.normalize_section_url(entry_url, section_id)


def sections_for_run(
    sections: list[dict[str, Any]],
    section_id: str | None,
    excluded_section_ids: set[str] | None = None,
    start_section_id: str | None = None,
    stop_section_id: str | None = None,
) -> list[dict[str, Any]]:
    if section_id is not None and (start_section_id is not None or stop_section_id is not None):
        raise ValueError("--section-id 不能与起止小节参数同时使用")
    selected = sections
    if section_id is not None:
        selected = [item for item in selected if str(item.get("section_id")) == str(section_id)]
        if not selected:
            raise ValueError(f"清单中找不到小节 ID {section_id}")
    elif start_section_id is not None or stop_section_id is not None:
        positions = {str(item.get("section_id")): index for index, item in enumerate(sections)}
        for bound in (start_section_id, stop_section_id):
            if bound is not None and str(bound) not in positions:
                raise ValueError(f"清单中找不到小节 ID {bound}")
        first = positions[str(start_section_id)] if start_section_id is not None else 0
        last = positions[str(stop_section_id)] if stop_section_id is not None else len(sections) - 1
        if first > last:
            raise ValueError("起始小节在终止小节之后")
        selected = sections[first:last + 1]
    excluded = excluded_section_ids or set()
    selected = [item for item in selected if str(item.get("section_id")) not in excluded]
    if not selected:
        raise ValueError("筛选后没有可处理的小节")
    return selected


def select_resources(resources: list[tuple[Frame, str]], courseware_only: bool) -> list[tuple[Frame, str]]:
    return [(frame, kind) for frame, kind in resources if kind == "courseware"] if courseware_only else resources


def video_classification(lock_marker: bool | None) -> str:
    if lock_marker is True:
        return "unskippable"
    if lock_marker is False:
        return "skippable"
    return "unknown"


def validate_restricted_play_scope(
    section_id: str, locked_count: int, allowed_section_ids: set[str]
) -> bool:
    if not section_id or str(section_id) not in allowed_section_ids:
        raise ValueError(f"小节 ID {section_id!r} 不在本次明确筛选的课程范围内")
    if locked_count < 0:
        raise ValueError("锁定视频数量不能为负数")
    return True


def may_attempt_video_seek(lock_marker: bool | None) -> bool:
    """Seek only when the page explicitly identifies the video as draggable."""
    return lock_marker is False


def is_normal_playback_rate(rate: float | None) -> bool:
    try:
        return abs(float(rate) - 1.0) < 0.001
    except (TypeError, ValueError):
        return False


def watch_threshold_reached(current: float | None, duration: float | None, threshold: float = 0.9) -> bool:
    try:
        return float(duration) > 0 and float(current) / float(duration) >= threshold
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def visible_track(frame: Frame) -> tuple[Any, dict[str, float], str] | None:
    for selector in VIDEO_TRACK_SELECTORS:
        try:
            locator = frame.locator(selector)
            for index in range(min(locator.count(), 8)):
                candidate = locator.nth(index)
                box = candidate.bounding_box(timeout=1200)
                if box and box["width"] >= 40 and 2 <= box["height"] <= 64:
                    return candidate, box, selector
        except Exception:
            continue
    try:
        candidate = frame.evaluate(
            r"""() => {
              const selector = [
                'input[type="range"]', '[role="slider"]', '[aria-label*="进度"]',
                '[title*="进度"]', '[class*="progress"]', '[class*="seek"]'
              ].join(',');
              const candidates = [...document.querySelectorAll(selector)].map(el => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                const hint = [el.className, el.id, el.getAttribute('aria-label'), el.title,
                  el.getAttribute('role'), el.parentElement?.className].join(' ').toLowerCase();
                const visible = rect.width >= 80 && rect.height >= 3 &&
                  style.display !== 'none' && style.visibility !== 'hidden' &&
                  Number(style.opacity || 1) > 0;
                const timeline = /progress|seek|time|slider|进度|播放位置/.test(hint);
                const excluded = /volume|sound|音量|倍速|speed|quality|清晰度/.test(hint);
                const explicit = el.getAttribute('aria-valuenow') !== null ||
                  el.matches('input[type="range"]');
                return {el, rect, hint, visible, timeline, excluded, explicit};
              }).filter(item => item.visible && item.timeline && !item.excluded)
                .sort((a, b) => Number(b.explicit) - Number(a.explicit) || b.rect.width - a.rect.width);
              const chosen = candidates[0];
              if (!chosen) return null;
              chosen.el.setAttribute('data-codex-seek-target', 'true');
              return {tag: chosen.el.tagName, hint: chosen.hint.slice(0, 100)};
            }"""
        )
        if candidate:
            locator = frame.locator('[data-codex-seek-target="true"]')
            box = locator.bounding_box(timeout=1200)
            if box:
                return locator, box, f"dom:{candidate['tag']}:{candidate['hint']}"
    except Exception:
        pass
    return None


def hover_video_controls(frame: Frame) -> bool:
    """Reveal hover-only controls without clicking or starting playback."""
    try:
        frame.locator("body").hover(timeout=3000)
        frame.page.wait_for_timeout(400)
        return True
    except Exception:
        return False


def current_progress_fraction(frame: Frame, track: Any) -> float:
    try:
        state = track.evaluate(
            """el => {
              const now = Number(el.getAttribute('aria-valuenow'));
              const max = Number(el.getAttribute('aria-valuemax'));
              if (Number.isFinite(now) && Number.isFinite(max) && max > 0) return now / max;
              const video = document.querySelector('video');
              if (video && Number.isFinite(video.duration) && video.duration > 0) return video.currentTime / video.duration;
              const fill = el.querySelector('.vjs-play-progress, .xgplayer-progress-inner, .dplayer-played, .plyr__progress__filled');
              if (fill && el.getBoundingClientRect().width > 0) return fill.getBoundingClientRect().width / el.getBoundingClientRect().width;
              return 0;
            }"""
        )
        fraction = float(state)
        if 0 <= fraction <= 1:
            return fraction
    except Exception:
        pass
    return 0.0


def media_progress(frame: Frame) -> tuple[float | None, float | None]:
    try:
        state = frame.locator("video").evaluate(
            """video => ({current: video.currentTime, duration: video.duration})"""
        )
        return state.get("current"), state.get("duration")
    except Exception:
        return None, None


def media_playback_state(frame: Frame) -> dict[str, Any]:
    return frame.locator("video").evaluate(
        """video => ({
          current: Number.isFinite(video.currentTime) ? video.currentTime : null,
          duration: Number.isFinite(video.duration) ? video.duration : null,
          paused: video.paused,
          ended: video.ended,
          rate: video.playbackRate,
          ready_state: video.readyState
        })"""
    )


def video_task_contexts(frame: Frame) -> list[str]:
    """Return only exact completion badges; never extract adjacent exercise text."""
    statuses: list[str] = []
    try:
        finished = frame.frame_element().evaluate(
            "el => !!el.closest('.videoContainer.ans-job-finished')"
        )
        if finished:
            statuses.append("任务点已完成")
    except Exception:
        pass
    badge_script = r"""el => {
      const accepted = new Set(['任务点已完成', '任务已完成']);
      const frame = el.getBoundingClientRect();
      const matches = [];
      for (const node of el.ownerDocument.querySelectorAll('*')) {
        const text = (node.textContent || '').replace(/\s+/g, '').trim();
        if (!accepted.has(text)) continue;
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        if (!rect.width || !rect.height || style.display === 'none' || style.visibility === 'hidden') continue;
        const horizontalOverlap = rect.right > frame.left - 24 && rect.left < frame.right + 24;
        const gap = frame.top >= rect.bottom ? frame.top - rect.bottom :
          (rect.top >= frame.bottom ? rect.top - frame.bottom : 0);
        if (horizontalOverlap && gap <= 180) matches.push({text, gap, area: rect.width * rect.height});
      }
      matches.sort((a, b) => a.gap - b.gap || a.area - b.area);
      return matches.length ? [matches[0].text] : [];
    }"""
    try:
        current: Frame | None = frame
        while current is not None:
            if current.parent_frame is None:
                break
            try:
                statuses.extend(current.frame_element().evaluate(badge_script))
            except Exception:
                pass
            current = current.parent_frame
    except Exception:
        pass
    return list(dict.fromkeys(statuses))


def task_point_completed(contexts: list[str]) -> bool:
    return any(re.search(r"任务点\s*已完成|任务已完成", text) for text in contexts)


def locked_video_ready_to_stop(current: float | None, duration: float | None, task_done: bool) -> bool:
    return watch_threshold_reached(current, duration) and task_done


def seek_result_status(fraction: float, task_done: bool) -> str:
    if fraction < 0.97:
        return "seek_rejected_or_incomplete"
    return "seeked_to_end" if task_done else "seeked_to_end_task_unconfirmed"


def finish_seeked_video_tail(page: Page, frame: Frame, timeout_seconds: int = 15) -> dict[str, Any]:
    """Let a successfully seeked video emit its natural end event at 1x."""
    state = media_playback_state(frame)
    if not watch_threshold_reached(state.get("current"), state.get("duration"), 0.97):
        return {"status": "seek_tail_position_not_verified", "task_point_completed": False}
    if task_point_completed(video_task_contexts(frame)):
        return {"status": "seeked_to_end", "task_point_completed": True}
    if not is_normal_playback_rate(state.get("rate")):
        frame.locator("video").evaluate("video => { video.playbackRate = 1.0; }")
    hover_video_controls(frame)
    for selector in ("button.vjs-play-control", "button.vjs-big-play-button"):
        button = frame.locator(selector)
        if button.count() and button.first.is_visible():
            button.first.click(timeout=8000)
            break
    else:
        return {"status": "seek_tail_play_button_not_found", "task_point_completed": False}

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if task_point_completed(video_task_contexts(frame)):
            return {"status": "seeked_to_end", "task_point_completed": True,
                    "verification": "natural_end_and_platform_task_marker"}
        state = media_playback_state(frame)
        if not watch_threshold_reached(state.get("current"), state.get("duration"), 0.95):
            return {"status": "seek_tail_restarted_from_earlier_position", "task_point_completed": False}
    return {"status": "seek_tail_played_task_unconfirmed", "task_point_completed": False}


def play_locked_video_to_threshold(
    page: Page,
    frame: Frame,
    timeout_seconds: int = 3600,
    threshold: float = 0.9,
    section_title: str = "课程小节",
) -> dict[str, Any]:
    """Use the visible Play button at 1x until the page's 90% watch threshold."""
    locked, context = inventory.video_marker_context(frame)
    if locked is not True or not LOCK_MARKER_RE.search(context or ""):
        return {"status": "restricted_marker_not_verified"}

    videos = frame.locator("video")
    if videos.count() != 1:
        return {"status": "video_element_count_unexpected", "count": videos.count()}
    play_button = frame.locator("button.vjs-big-play-button")
    if play_button.count() != 1 or not play_button.is_visible():
        return {"status": "visible_play_button_not_found"}

    initial = media_playback_state(frame)
    if not is_normal_playback_rate(initial.get("rate")):
        videos.evaluate("video => { video.playbackRate = 1.0; }")
    play_button.click(timeout=8000)
    started_at = time.monotonic()
    last_advance_at = started_at
    last_progress = initial.get("current") or 0.0
    threshold_since: float | None = None
    last_log_at = started_at
    paused_since: float | None = None
    ended_since: float | None = None

    while time.monotonic() - started_at < timeout_seconds:
        state = media_playback_state(frame)
        if not is_normal_playback_rate(state.get("rate")):
            videos.evaluate("video => { video.playbackRate = 1.0; }")
            state = media_playback_state(frame)

        current = state.get("current")
        duration = state.get("duration")
        now = time.monotonic()
        if current is not None and current > last_progress + 0.15:
            last_progress = current
            last_advance_at = now
            paused_since = None
        elif state.get("paused"):
            paused_since = paused_since or now
        else:
            paused_since = None

        if state.get("ended"):
            if ended_since is None:
                ended_since = now
        else:
            ended_since = None

        if watch_threshold_reached(current, duration, threshold):
            threshold_since = threshold_since or now
            if now - threshold_since >= 6 and task_point_completed(video_task_contexts(frame)):
                videos.evaluate("video => video.pause()")
                page.wait_for_timeout(2500)
                final_state = media_playback_state(frame)
                return {
                    "status": "watched_threshold_met"
                    if watch_threshold_reached(final_state.get("current"), final_state.get("duration"), threshold)
                    and task_point_completed(video_task_contexts(frame))
                    else "threshold_verification_failed",
                    "threshold": threshold,
                    "before": initial,
                    "after": final_state,
                    "normal_speed": is_normal_playback_rate(final_state.get("rate")),
                    "task_point_completed": task_point_completed(video_task_contexts(frame)),
                    "lock_marker_verified": True,
                }
        else:
            threshold_since = None

        if paused_since is not None and not state.get("ended") and now - paused_since >= 20:
            return {
                "status": "playback_paused_before_threshold",
                "before": initial,
                "after": state,
                "elapsed_seconds": round(now - started_at, 1),
                "normal_speed": is_normal_playback_rate(state.get("rate")),
            }
        if now - last_advance_at >= 45 and not watch_threshold_reached(current, duration, threshold):
            return {
                "status": "playback_stalled_before_threshold",
                "before": initial,
                "after": state,
                "elapsed_seconds": round(now - started_at, 1),
                "normal_speed": is_normal_playback_rate(state.get("rate")),
            }
        if now - last_log_at >= 15:
            ratio = current / duration if current is not None and duration else None
            print(
                f"[{section_title}] 锁定视频正常速度播放中：{current!r}/{duration!r} 秒，"
                f"进度={ratio:.1%}" if ratio is not None else
                f"[{section_title}] 锁定视频正在正常速度播放，等待读取时长（当前时间={current!r} 秒）",
                flush=True,
            )
            last_log_at = now
        if ended_since is not None and now - ended_since >= 30 and not task_point_completed(video_task_contexts(frame)):
            return {
                "status": "video_ended_without_task_completion",
                "before": initial,
                "after": state,
                "normal_speed": is_normal_playback_rate(state.get("rate")),
                "task_point_completed": False,
            }
        page.wait_for_timeout(1000)

    return {
        "status": "playback_timeout_before_threshold",
        "before": initial,
        "after": media_playback_state(frame),
        "normal_speed": is_normal_playback_rate(media_playback_state(frame).get("rate")),
    }


def activate_skippable_video_controls(frame: Frame, timeout_ms: int = 10000) -> dict[str, Any]:
    """Load a permitted player's timeline with a brief 1x start, then immediately pause."""
    videos = frame.locator("video")
    if videos.count() != 1:
        return {"status": "video_element_count_unexpected", "count": videos.count()}
    state = media_playback_state(frame)
    if state.get("duration") and state.get("ready_state", 0) >= 1:
        return {"status": "metadata_already_loaded", "state": state}

    play_button = frame.locator("button.vjs-big-play-button")
    if play_button.count() != 1 or not play_button.is_visible():
        return {"status": "visible_play_button_not_found", "before": state}
    videos.evaluate("video => { video.playbackRate = 1.0; }")
    play_button.click(timeout=8000)
    videos.evaluate("video => { video.playbackRate = 1.0; video.pause(); }")
    try:
        frame.wait_for_function(
            "() => { const v = document.querySelector('video'); return !!v && v.readyState >= 1 && Number.isFinite(v.duration) && v.duration > 0; }",
            timeout=timeout_ms,
        )
    except Exception:
        pass
    videos.evaluate("video => { video.playbackRate = 1.0; video.pause(); }")
    frame.page.wait_for_timeout(250)
    after = media_playback_state(frame)
    return {
        "status": "metadata_loaded" if after.get("duration") else "metadata_not_loaded",
        "state": after,
        "paused": bool(after.get("paused")),
        "normal_speed": is_normal_playback_rate(after.get("rate")),
    }


def seek_video_to_end(page: Page, frame: Frame) -> dict[str, Any]:
    """Drag a visible player seek bar; do not click play or set currentTime."""
    locked, marker_context = inventory.video_marker_context(frame)
    marker_found = bool(LOCK_MARKER_RE.search(marker_context or ""))
    if locked is True:
        return {
            "status": "restricted_marker",
            "marker": "90_percent_non_draggable" if marker_found else "video_lock_text_detected",
        }
    if locked is None:
        return {"status": "classification_unknown", "reason": "video_marker_context_unreadable"}
    if not may_attempt_video_seek(locked):
        return {"status": "seek_refused_without_skippable_marker"}

    hover_video_controls(frame)
    found = visible_track(frame)
    activation: dict[str, Any] | None = None
    if found is None:
        activation = activate_skippable_video_controls(frame)
        hover_video_controls(frame)
        found = visible_track(frame)
    if found is None:
        return {"status": "seek_bar_not_found", "activation": activation}
    track, box, selector = found
    points = seek_drag_points(box, current_progress_fraction(frame, track))
    if points is None:
        return {"status": "seek_bar_geometry_invalid", "selector": selector}

    before_current, before_duration = media_progress(frame)
    start, end = points
    page.mouse.move(*start)
    page.mouse.down()
    try:
        page.mouse.move(*end, steps=8)
    finally:
        page.mouse.up()
    page.wait_for_timeout(3000)
    after_current, after_duration = media_progress(frame)
    after_fraction = current_progress_fraction(frame, track)
    task_done = task_point_completed(video_task_contexts(frame))
    if after_current is not None and after_duration and after_duration > 0:
        ratio = after_current / after_duration
        status = seek_result_status(ratio, task_done)
        return {
            "status": status,
            "selector": selector,
            "before_seconds": before_current,
            "duration_seconds": after_duration,
            "after_seconds": after_current,
            "end_fraction": round(ratio, 4),
            "task_point_completed": task_done,
            "activation": activation,
        }
    if after_fraction >= 0.97:
        return {
            "status": seek_result_status(after_fraction, task_done),
            "selector": selector,
            "verification": "seek_control_at_end",
            "end_fraction": round(after_fraction, 4),
            "task_point_completed": task_done,
            "activation": activation,
        }
    return {
        "status": "drag_sent_unverified",
        "selector": selector,
        "end_fraction": round(after_fraction, 4),
        "before_seconds": before_current,
        "duration_seconds": before_duration,
        "after_seconds": after_current,
        "activation": activation,
    }


SCROLL_FRAME_JS = r"""() => {
  const root = document.scrollingElement || document.documentElement;
  const all = [root, ...document.querySelectorAll('*')];
  const targets = [...new Set(all)].filter(el => {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) return false;
    return el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA' &&
      el.tagName !== 'SELECT' && el.scrollHeight > el.clientHeight + 80;
  });
  const states = targets.map(el => {
    const rect = el.getBoundingClientRect();
    return {
      tag: el.tagName,
      id: (el.id || '').slice(0, 80),
      className: String(el.className || '').slice(0, 120),
      overflowY: getComputedStyle(el).overflowY,
      top: el.scrollTop,
      maxTop: Math.max(0, el.scrollHeight - el.clientHeight),
      rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
    };
  });
  return {count: targets.length, atEnd: states.every(s => s.top >= s.maxTop - 2), states};
}"""


SCROLL_TO_BOTTOM_JS = r"""() => {
  const root = document.scrollingElement || document.documentElement;
  const all = [root, ...document.querySelectorAll('*')];
  const targets = [...new Set(all)].filter(el => {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 &&
      el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA' && el.tagName !== 'SELECT' &&
      el.scrollHeight > el.clientHeight + 80;
  });
  for (const el of targets) {
    el.scrollTop = el.scrollHeight;
    el.scrollLeft = el.scrollWidth;
    el.dispatchEvent(new Event('scroll', {bubbles: true}));
  }
  window.scrollTo(0, root.scrollHeight);
  return targets.length;
}"""


def is_meaningful_scroll_container(tag: str, scroll_height: float, client_height: float) -> bool:
    """Ignore tiny layout/slider overflow; retain document or pane scroll surfaces."""
    try:
        delta = float(scroll_height) - float(client_height)
    except (TypeError, ValueError):
        return False
    return tag.upper() not in {"INPUT", "TEXTAREA", "SELECT"} and delta > 80


def scroll_state_at_bottom(states: list[dict[str, Any]], tolerance: float = 2) -> bool:
    return bool(states) and all(
        float(state.get("top", 0)) >= float(state.get("maxTop", 0)) - tolerance
        for state in states
    )


def scroll_state_progressed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return before.get("count") != after.get("count") or before.get("states") != after.get("states")


def scroll_courseware_to_end(frame: Frame, passes: int = 30) -> dict[str, Any]:
    last: dict[str, Any] = {"count": 0, "atEnd": True, "states": []}
    for _ in range(passes):
        last = frame.evaluate(SCROLL_FRAME_JS)
        if last.get("count", 0) == 0 or last.get("atEnd"):
            break
        try:
            frame.locator("body").hover(timeout=1200)
            frame.evaluate(SCROLL_TO_BOTTOM_JS)
        except Exception:
            pass
        frame.page.wait_for_timeout(450)
    if last.get("count", 0) and not last.get("atEnd"):
        unchanged_rounds = 0
        try:
            frame.locator("body").hover(timeout=1200)
        except Exception:
            pass
        for _ in range(500):
            before = last
            try:
                frame.page.mouse.wheel(0, 1200)
                frame.page.wait_for_timeout(100)
                last = frame.evaluate(SCROLL_FRAME_JS)
            except Exception:
                break
            if not last.get("count", 0) or last.get("atEnd"):
                break
            if scroll_state_progressed(before, last):
                unchanged_rounds = 0
            else:
                unchanged_rounds += 1
                if unchanged_rounds >= 8:
                    break
    if last.get("count", 0) and not last.get("atEnd"):
        try:
            last = frame.evaluate(SCROLL_FRAME_JS)
        except Exception:
            pass
    if last.get("count", 0) == 0:
        return {"status": "no_scrollable_overflow", "scrollable_count": 0}
    return {
        "status": "scrolled_to_end" if last.get("atEnd") else "bottom_not_confirmed",
        "scrollable_count": last.get("count", 0),
        "at_end": bool(last.get("atEnd")),
        "scrollables": last.get("states", []),
    }


def courseware_frame_tree(frame: Frame) -> list[Frame]:
    result: list[Frame] = []
    pending = list(frame.child_frames)
    while pending:
        child = pending.pop(0)
        result.append(child)
        pending.extend(child.child_frames)
    return result


def courseware_tree_status(results: list[dict[str, Any]]) -> str:
    statuses = {item.get("status") for item in results}
    if "scrolled_to_end" in statuses:
        return "scrolled_to_end"
    if "bottom_not_confirmed" in statuses:
        return "bottom_not_confirmed"
    if "error" in statuses:
        return "scroll_error"
    return "no_scrollable_overflow"


def scroll_courseware_tree(frame: Frame) -> dict[str, Any]:
    results = [{"frame": "resource", **scroll_courseware_to_end(frame)}]
    for index, child in enumerate(courseware_frame_tree(frame), 1):
        try:
            results.append({
                "frame": f"embedded_{index}",
                "frame_url": resource_url_for_report(child.url),
                **scroll_courseware_to_end(child),
            })
        except Exception as exc:
            results.append({"frame": f"embedded_{index}", "status": "error", "error": type(exc).__name__})
    return {"status": courseware_tree_status(results), "frame_results": results}


def wait_for_resource_frames(
    page: Page, expected_count: int = 0, timeout_ms: int = 15000
) -> list[tuple[Frame, str]]:
    deadline = time.monotonic() + timeout_ms / 1000
    previous: tuple[tuple[str, str], ...] | None = None
    stable_rounds = 0
    current: list[tuple[Frame, str]] = []
    while time.monotonic() < deadline:
        current = []
        for frame in page.frames:
            kind = inventory.frame_kind(frame.url, "")
            if kind in {"video", "courseware"}:
                current.append((frame, kind))
        signature = tuple((kind, frame.url) for frame, kind in current)
        if signature == previous:
            stable_rounds += 1
            if stable_rounds >= 3 and len(current) >= expected_count:
                break
        else:
            stable_rounds = 0
            previous = signature
        page.wait_for_timeout(350)
    return current


def count_quiz_frames_without_opening(page: Page) -> int:
    """Classify by frame URL only; never read quiz DOM or controls."""
    return sum(inventory.frame_kind(frame.url, "") == "quiz" for frame in page.frames)


def resource_evidence_by_kind(section: dict[str, Any], kind: str) -> int:
    return sum(item.get("kind") == kind for item in section.get("evidence", []))


def new_resource_record(
    section: dict[str, Any], entry_url: str, order: int, courseware_only: bool = False
) -> dict[str, Any]:
    section_id = str(section["section_id"])
    return {
        "section_id": section_id,
        "order": section.get("order", order),
        "title": section.get("title", ""),
        "url": inventory.safe_url(section_url_for_id(entry_url, section_id)),
        "expected_video_count": resource_evidence_by_kind(section, "video"),
        "expected_courseware_count": resource_evidence_by_kind(section, "courseware"),
        "expected_quiz_frame_count": resource_evidence_by_kind(section, "quiz"),
        "mode": "courseware_only" if courseware_only else "video_and_courseware",
        "observed_quiz_frame_count": 0,
        "resources": [],
    }


def process_resources_on_page(
    page: Page,
    record: dict[str, Any],
    play_restricted_video: bool,
    allowed_section_ids: set[str],
) -> dict[str, Any]:
    """Process the already-open lesson page; reusable by the one-shot runner."""
    section_id = record["section_id"]
    courseware_only = record["mode"] == "courseware_only"
    expected_total = record["expected_video_count"] + record["expected_courseware_count"]
    expected_selected = record["expected_courseware_count"] if courseware_only else expected_total
    all_resources = wait_for_resource_frames(page, expected_total)
    resources = select_resources(all_resources, courseware_only)
    record["observed_quiz_frame_count"] = count_quiz_frames_without_opening(page)
    if not resources and expected_selected:
        record["page_title"] = page.title()[:180]
        record["frame_urls"] = [resource_url_for_report(frame.url) for frame in page.frames]
        record["status"] = "failed"
        record["error"] = "expected resource frames did not load; stopped to avoid empty processing"
        return record

    restricted_videos: list[Frame] = []
    if play_restricted_video and not courseware_only:
        restricted_videos = [
            frame for frame, kind in resources
            if kind == "video"
            and inventory.video_marker_context(frame)[0] is True
            and LOCK_MARKER_RE.search(inventory.video_marker_context(frame)[1] or "")
        ]
        validate_restricted_play_scope(section_id, len(restricted_videos), allowed_section_ids)

    for resource_index, (frame, kind) in enumerate(resources, 1):
        resource_record: dict[str, Any] = {
            "kind": kind,
            "ordinal": resource_index,
            "frame_url": resource_url_for_report(frame.url),
        }
        try:
            if kind == "video":
                marker_state, _ = inventory.video_marker_context(frame)
                resource_record["classification"] = video_classification(marker_state)
            if kind == "video" and task_point_completed(video_task_contexts(frame)):
                result = {"status": "already_completed", "verified_by": "associated_task_point_marker"}
            elif kind == "video" and any(frame is target for target in restricted_videos):
                result = play_locked_video_to_threshold(page, frame, section_title=record["title"] or section_id)
            elif kind == "video":
                result = seek_video_to_end(page, frame)
                if result.get("status") == "seeked_to_end_task_unconfirmed":
                    tail = finish_seeked_video_tail(page, frame)
                    result["tail_playback"] = tail
                    result["status"] = tail["status"]
                    result["task_point_completed"] = tail["task_point_completed"]
                if result.get("status") == "classification_unknown" and task_point_completed(video_task_contexts(frame)):
                    result = {"status": "already_completed", "verified_by": "associated_task_point_marker"}
            else:
                result = scroll_courseware_tree(frame)
            resource_record.update(result)
        except Exception as exc:
            resource_record.update({"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
        record["resources"].append(resource_record)

    record["status"] = "complete" if all(
        resource.get("status") in {"seeked_to_end", "watched_threshold_met", "already_completed", "scrolled_to_end"}
        for resource in record["resources"]
    ) and len(resources) == expected_selected else "partial"
    return record


def save_report(path: Path, entry_url: str, report_sections: list[dict[str, Any]], expected_sections: int) -> dict[str, Any]:
    flat = [resource for section in report_sections for resource in section["resources"]]
    counts = {
        "videos_total": sum(item["kind"] == "video" for item in flat),
        "videos_skippable": sum(item["kind"] == "video" and item.get("classification") == "skippable" for item in flat),
        "videos_unskippable": sum(item["kind"] == "video" and item.get("classification") == "unskippable" for item in flat),
        "videos_unknown_classification": sum(item["kind"] == "video" and item.get("classification") == "unknown" for item in flat),
        "videos_seeked_to_end": sum(item["kind"] == "video" and item["status"] == "seeked_to_end" for item in flat),
        "videos_watched_to_threshold": sum(item["kind"] == "video" and item["status"] == "watched_threshold_met" for item in flat),
        "videos_already_completed": sum(item["kind"] == "video" and item["status"] == "already_completed" for item in flat),
        "videos_restricted": sum(item["kind"] == "video" and item["status"] == "restricted_marker" for item in flat),
        "videos_unavailable_or_unverified": sum(item["kind"] == "video" and item["status"] not in {"seeked_to_end", "watched_threshold_met", "already_completed", "restricted_marker"} for item in flat),
        "courseware_total": sum(item["kind"] == "courseware" for item in flat),
        "courseware_scrolled_to_end": sum(item["kind"] == "courseware" and item["status"] == "scrolled_to_end" for item in flat),
    }
    report = {
        "captured_at": timestamp(),
        "course_entry_url": inventory.safe_url(entry_url),
        "section_count_expected": expected_sections,
        "section_count_processed": len(report_sections),
        "counts": counts,
        "quiz_policy": "not_opened_not_read_not_answered_not_submitted",
        "sections": report_sections,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按课程清单将可拖视频定位到末尾、课件滚动到末尾；不操作测验")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY, help="lesson-inventory.json 路径")
    parser.add_argument("--url", help="可选的课程入口 URL；支持会话 enc 参数，但报告会自动脱敏")
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE, help="持久化 Chrome 登录资料目录")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT, help="逐节处理结果目录")
    parser.add_argument("--section-id", help="仅处理指定小节 ID；不打开或读取测验内容")
    parser.add_argument("--start-section-id", help="从指定小节开始顺序处理（含该小节）")
    parser.add_argument("--stop-section-id", help="处理到指定小节为止（含该小节）")
    parser.add_argument("--courseware-only", action="store_true", help="仅重试课件，不操作已完成视频")
    parser.add_argument(
        "--play-restricted-video",
        action="store_true",
        help="需用户明确授权；对本次选定小节中有 90%% 不可拖拽提示的视频保持正常 1x 播放直到阈值",
    )
    parser.add_argument(
        "--exclude-section-id",
        action="append",
        default=[],
        help="跳过指定小节 ID；可重复指定",
    )
    parser.add_argument("--login-timeout", type=int, default=600, help="等待手动登录的秒数")
    parser.add_argument("--assume-authenticated", action="store_true", help="若未登录则立即报错，不等待手动登录")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    data = load_inventory(args.inventory)
    entry_url = args.url or data["course"]["entry_url"]
    course_id = str(data["course"].get("course_id") or inventory.query_value(entry_url, ("courseId", "courseid")) or "")
    sections = sorted(data["sections"], key=lambda item: item.get("order", 0))
    sections = sections_for_run(
        sections, args.section_id, set(args.exclude_section_id), args.start_section_id, args.stop_section_id
    )
    if args.courseware_only and args.play_restricted_video:
        raise ValueError("仅课件模式不能同时要求播放受限视频")
    allowed_section_ids = {str(item["section_id"]) for item in sections}
    if args.play_restricted_video:
        for selected_id in allowed_section_ids:
            validate_restricted_play_scope(selected_id, 0, allowed_section_ids)
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"lesson-completion-{args.section_id}.json" if args.section_id else "lesson-completion.json"
    output_file = args.output_dir / output_name
    progress: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        context: BrowserContext = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.profile_dir.resolve()),
            channel="chrome",
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            expected_id = course_id or None
            outline_url = initial_course_page_url(entry_url)
            page.goto(outline_url, wait_until="domcontentloaded", timeout=45000)
            if inventory.is_login_page(page):
                if args.assume_authenticated:
                    raise RuntimeError("Chrome 资料目录中的登录态已失效")
                print("请在打开的 Chrome 窗口手动登录；脚本不会读取或填写凭据。", flush=True)
                inventory.wait_for_manual_login(page, args.login_timeout, context, "科技文献检索与利用", expected_id)
                target = inventory.find_target_course_page(context.pages, "科技文献检索与利用", expected_id)
                if target is None:
                    raise RuntimeError("登录后没有找到指定课程页面")
                page = target
                if "studentcourse" not in page.url.lower():
                    page.goto(outline_url, wait_until="domcontentloaded", timeout=45000)

            page.wait_for_timeout(700)
            actual_course = inventory.query_value(page.url, ("courseId", "courseid"))
            if course_id and actual_course and actual_course != course_id:
                raise RuntimeError(f"当前页面课程 ID {actual_course} 与清单 {course_id} 不一致")

            print(f"已确认课程 ID {actual_course or course_id}；按清单中的 {len(sections)} 个章节 ID 逐节访问。测验资源将跳过且不读取题目。", flush=True)
            for index, section in enumerate(sections, 1):
                section_id = str(section["section_id"])
                target_url = section_url_for_id(entry_url, section_id)
                record = new_resource_record(section, entry_url, index, args.courseware_only)
                try:
                    response = page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                    if response is not None and response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}")
                    page.wait_for_timeout(900)
                    if inventory.is_login_page(page):
                        raise RuntimeError("登录态失效，需要手动重新登录")
                    record = process_resources_on_page(page, record, args.play_restricted_video, allowed_section_ids)
                    if record["status"] == "failed" and record.get("error") == "expected resource frames did not load; stopped to avoid empty processing":
                        progress.append(record)
                        save_report(output_file, entry_url, progress, len(sections))
                        print(
                            f"[{index}/{len(sections)}] {record['title']}: 页面未载入预期资源框架，"
                            f"标题={record['page_title']!r}；已停止，未操作测验。",
                            flush=True,
                        )
                        print("Chrome 保持开启以便检查；关闭浏览器窗口后脚本退出。", flush=True)
                        while any(not open_page.is_closed() for open_page in context.pages):
                            time.sleep(1)
                        return 2
                except PlaywrightTimeoutError as exc:
                    record["status"] = "failed"
                    record["error"] = f"navigation_timeout: {type(exc).__name__}"
                except Exception as exc:
                    record["status"] = "failed"
                    record["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"

                progress.append(record)
                report = save_report(output_file, entry_url, progress, len(sections))
                video_states = [item["status"] for item in record["resources"] if item["kind"] == "video"]
                courseware_states = [item["status"] for item in record["resources"] if item["kind"] == "courseware"]
                print(
                    f"[{index}/{len(sections)}] {record['title']}: "
                    f"视频 {len(video_states)}（{', '.join(video_states) or '无'}），"
                    f"课件 {len(courseware_states)}（{', '.join(courseware_states) or '无'}），"
                    f"测验未触碰；本节 {record['status']}。",
                    flush=True,
                )

            report = save_report(output_file, entry_url, progress, len(sections))
            print(f"处理记录：{output_file.resolve()}", flush=True)
            print(f"汇总：{json.dumps(report['counts'], ensure_ascii=False)}", flush=True)
            print("任务结束，Chrome 保持开启；关闭浏览器窗口后脚本退出。", flush=True)
            while any(not open_page.is_closed() for open_page in context.pages):
                time.sleep(1)
            return 0 if all(section["status"] != "failed" for section in progress) else 2
        except Exception as exc:
            print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if any(not open_page.is_closed() for open_page in context.pages):
                print("Chrome 保持开启以便检查；关闭窗口后脚本退出。", flush=True)
                while any(not open_page.is_closed() for open_page in context.pages):
                    time.sleep(1)
            return 2


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
