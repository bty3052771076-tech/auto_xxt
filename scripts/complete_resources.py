#!/usr/bin/env python
"""Seek course videos to the end and scroll courseware, without touching quizzes."""

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
    raise SystemExit("未找到 Python Playwright；本脚本不会安装依赖。") from exc

try:
    from .lesson_inventory import (
        DEFAULT_OUTPUT,
        DEFAULT_PROFILE,
        VIDEO_LOCK_RE,
        classify_video_marker_context,
        find_target_course_page,
        frame_kind,
        frame_resource_id,
        is_login_page,
        normalize_section_url,
        query_value,
        safe_url,
        wait_for_frames_to_stabilize,
        wait_for_manual_login,
    )
except ImportError:  # pragma: no cover - direct script execution
    from lesson_inventory import (
        DEFAULT_OUTPUT,
        DEFAULT_PROFILE,
        VIDEO_LOCK_RE,
        classify_video_marker_context,
        find_target_course_page,
        frame_kind,
        frame_resource_id,
        is_login_page,
        normalize_section_url,
        query_value,
        safe_url,
        wait_for_frames_to_stabilize,
        wait_for_manual_login,
    )


DEFAULT_INVENTORY = DEFAULT_OUTPUT / "lesson-inventory.json"
DEFAULT_COMPLETION_DIR = DEFAULT_OUTPUT / "completion"
END_RATIO = 0.995
SCROLL_LIMIT = 80


def build_resource_plan(report: dict[str, Any]) -> list[dict[str, Any]]:
    course = report.get("course") or {}
    entry_url = str(course.get("entry_url") or "")
    if not entry_url:
        raise ValueError("清单缺少 course.entry_url")
    raw_sections = report.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ValueError("清单没有小节数据")

    plan = []
    seen_ids: set[str] = set()
    for section in sorted(raw_sections, key=lambda item: int(item.get("order", 0))):
        section_id = str(section.get("section_id") or "")
        if not section_id:
            raise ValueError("小节缺少 section_id")
        if section_id in seen_ids:
            raise ValueError(f"目录中存在重复 section_id：{section_id}")
        seen_ids.add(section_id)
        evidence = section.get("evidence") or []
        video_count = sum(item.get("kind") == "video" for item in evidence)
        courseware_count = sum(item.get("kind") == "courseware" for item in evidence)
        if section.get("video_total") is not None and video_count != section["video_total"]:
            raise ValueError(f"{section.get('title', section_id)} 的视频数与 evidence 不一致")
        if section.get("courseware_total") is not None and courseware_count != section["courseware_total"]:
            raise ValueError(f"{section.get('title', section_id)} 的课件数与 evidence 不一致")
        plan.append(
            {
                "section_id": section_id,
                "order": int(section.get("order", len(plan) + 1)),
                "title": str(section.get("title") or section_id),
                "section_url": normalize_section_url(entry_url, section_id),
                "expected_video_count": video_count,
                "expected_courseware_count": courseware_count,
            }
        )
    return plan


def is_progress_complete(
    current_time: float | None,
    duration: float | None,
    slider_value: float | None = None,
    slider_max: float | None = None,
) -> bool:
    if current_time is not None and duration is not None and duration > 0:
        return current_time / duration >= END_RATIO
    if slider_value is not None and slider_max is not None and slider_max > 0:
        return slider_value / slider_max >= END_RATIO
    return False


def resource_counts_match(
    expected_videos: int,
    expected_courseware: int,
    found_videos: int,
    found_courseware: int,
) -> bool:
    return expected_videos == found_videos and expected_courseware == found_courseware


def load_inventory(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report = json.loads(path.read_text(encoding="utf-8"))
    plan = build_resource_plan(report)
    return report, plan


def visible_resource_frames(page: Page) -> tuple[list[Frame], list[Frame]]:
    videos: list[Frame] = []
    courseware: list[Frame] = []
    for frame in page.frames:
        try:
            body_text = frame.locator("body").inner_text(timeout=1000)
        except Exception:
            body_text = ""
        kind = frame_kind(frame.url, body_text)
        try:
            if not frame.frame_element().is_visible():
                continue
        except Exception:
            continue
        if kind == "video":
            videos.append(frame)
        elif kind == "courseware":
            courseware.append(frame)
    return videos, courseware


VIDEO_PROGRESS_SCRIPT = r"""() => {
  const video = [...document.querySelectorAll('video')].find(el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }) || document.querySelector('video');
  const selectors = [
    'input[type="range"]', '[role="slider"]', '[aria-label*="进度"]',
    '[title*="进度"]', '[class*="progress"]', '[class*="seek"]'
  ].join(',');
  const matches = [...document.querySelectorAll(selectors)].map(el => {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const text = [el.className, el.getAttribute('aria-label'), el.title,
      el.getAttribute('role'), el.parentElement?.className].join(' ').toLowerCase();
    const visible = r.width >= 80 && r.height >= 3 && style.display !== 'none' &&
      style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
    const timeline = /progress|seek|time|slider|进度|播放位置/.test(text);
    const excluded = /volume|sound|音量|倍速|speed|quality|清晰度/.test(text);
    const explicitValue = el.getAttribute('aria-valuenow') !== null ||
      el.matches('input[type="range"]');
    return {el, r, text, visible, timeline, excluded, explicitValue};
  }).filter(item => item.visible && item.timeline && !item.excluded)
    .sort((a, b) => (Number(b.explicitValue) - Number(a.explicitValue)) || (b.r.width - a.r.width));
  const slider = matches[0];
  return {
    video: video ? {
      currentTime: Number.isFinite(video.currentTime) ? video.currentTime : null,
      duration: Number.isFinite(video.duration) ? video.duration : null,
      paused: video.paused
    } : null,
    slider: slider ? {
      width: slider.r.width,
      height: slider.r.height,
      value: Number(slider.el.getAttribute('aria-valuenow') ?? slider.el.value) || null,
      min: Number(slider.el.getAttribute('aria-valuemin') ?? slider.el.min) || 0,
      max: Number(slider.el.getAttribute('aria-valuemax') ?? slider.el.max) || null,
      tag: slider.el.tagName,
      label: slider.el.getAttribute('aria-label') || slider.el.title || slider.text.slice(0, 160),
      rect: {x: slider.r.x, y: slider.r.y}
    } : null
  };
}"""


def video_progress(frame: Frame) -> dict[str, Any]:
    return frame.evaluate(VIDEO_PROGRESS_SCRIPT)


def select_timeline(frame: Frame) -> dict[str, Any] | None:
    return frame.evaluate(
        r"""() => {
          const selector = [
            'input[type="range"]', '[role="slider"]', '[aria-label*="进度"]',
            '[title*="进度"]', '[class*="progress"]', '[class*="seek"]'
          ].join(',');
          const candidates = [...document.querySelectorAll(selector)].map(el => {
            const r = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            const text = [el.className, el.getAttribute('aria-label'), el.title,
              el.getAttribute('role'), el.parentElement?.className].join(' ').toLowerCase();
            const visible = r.width >= 80 && r.height >= 3 && style.display !== 'none' &&
              style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
            const timeline = /progress|seek|time|slider|进度|播放位置/.test(text);
            const excluded = /volume|sound|音量|倍速|speed|quality|清晰度/.test(text);
            const explicit = el.getAttribute('aria-valuenow') !== null || el.matches('input[type="range"]');
            return {el, r, text, visible, timeline, excluded, explicit};
          }).filter(x => x.visible && x.timeline && !x.excluded)
            .sort((a, b) => (Number(b.explicit) - Number(a.explicit)) || (b.r.width - a.r.width));
          const target = candidates[0];
          if (!target) return null;
          target.el.setAttribute('data-codex-seek-target', 'true');
          return {width: target.r.width, height: target.r.height, label: target.text.slice(0, 160)};
        }"""
    )


def seek_video_to_end(frame: Frame) -> dict[str, Any]:
    try:
        frame.locator("body").hover(timeout=3000)
    except Exception:
        pass
    before = video_progress(frame)
    if is_progress_complete(
        (before.get("video") or {}).get("currentTime"),
        (before.get("video") or {}).get("duration"),
        (before.get("slider") or {}).get("value"),
        (before.get("slider") or {}).get("max"),
    ):
        return {"status": "already_at_end", "before": before, "after": before}

    timeline = select_timeline(frame)
    if timeline is None:
        return {"status": "no_seek_control", "before": before, "after": before}
    try:
        frame.locator('[data-codex-seek-target="true"]').click(
            position={"x": max(1, timeline["width"] - 2), "y": timeline["height"] / 2},
            timeout=5000,
        )
        frame.wait_for_timeout(1200)
    except Exception as exc:
        return {
            "status": "seek_click_failed",
            "before": before,
            "after": video_progress(frame),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }

    after = video_progress(frame)
    complete = is_progress_complete(
        (after.get("video") or {}).get("currentTime"),
        (after.get("video") or {}).get("duration"),
        (after.get("slider") or {}).get("value"),
        (after.get("slider") or {}).get("max"),
    )
    return {
        "status": "complete" if complete else "seek_not_verified",
        "before": before,
        "after": after,
    }


SCROLL_STATE_SCRIPT = r"""() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 20 && r.height > 20 && s.display !== 'none' &&
      s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
  };
  const nodes = [document.scrollingElement, ...document.querySelectorAll('*')]
    .filter((el, i, all) => el && all.indexOf(el) === i && visible(el) &&
      el.scrollHeight > el.clientHeight + 8)
    .map(el => ({
      top: Math.round(el.scrollTop),
      max: Math.max(0, Math.round(el.scrollHeight - el.clientHeight)),
      left: Math.round(el.scrollLeft),
      maxLeft: Math.max(0, Math.round(el.scrollWidth - el.clientWidth))
    }));
  const bodyText = (document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 3000);
  const counters = [...document.querySelectorAll('[class*="page"], [class*="slide"], [aria-label]')]
    .filter(visible).map(el => (el.innerText || el.getAttribute('aria-label') || '').trim())
    .filter(t => /\b\d+\s*(?:\/|of|页，共|共)\s*\d+\b/i.test(t)).slice(0, 12);
  return {nodes, bodyText, counters};
}"""


def courseware_scroll_state(frame: Frame) -> dict[str, Any]:
    return frame.evaluate(SCROLL_STATE_SCRIPT)


def is_courseware_at_bottom(state: dict[str, Any]) -> bool:
    nodes = state.get("nodes") or []
    if nodes and all(node["top"] >= node["max"] - 4 for node in nodes):
        return True
    for counter in state.get("counters") or []:
        match = re.search(r"\b(\d+)\s*/\s*(\d+)\b", counter)
        if match and int(match.group(1)) >= int(match.group(2)):
            return True
    return False


def scroll_courseware_to_bottom(frame: Frame) -> dict[str, Any]:
    try:
        frame.locator("body").hover(timeout=3000)
    except Exception:
        pass
    before = courseware_scroll_state(frame)
    if is_courseware_at_bottom(before):
        return {"status": "already_at_bottom", "steps": 0, "before": before, "after": before}

    unchanged_rounds = 0
    previous_signature = json.dumps(before, ensure_ascii=False, sort_keys=True)
    after = before
    steps = 0
    for _ in range(SCROLL_LIMIT):
        frame.page.mouse.wheel(0, 900)
        frame.wait_for_timeout(120)
        steps += 1
        after = courseware_scroll_state(frame)
        if is_courseware_at_bottom(after):
            return {"status": "complete", "steps": steps, "before": before, "after": after}
        signature = json.dumps(after, ensure_ascii=False, sort_keys=True)
        if signature == previous_signature:
            unchanged_rounds += 1
            if unchanged_rounds >= 4:
                break
        else:
            unchanged_rounds = 0
            previous_signature = signature
    return {"status": "scroll_not_verified", "steps": steps, "before": before, "after": after}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_progress(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按已有清单将视频进度定位到末尾、把课件滚动到底；不操作测验"
    )
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_COMPLETION_DIR)
    parser.add_argument("--login-timeout", type=int, default=600)
    parser.add_argument("--execute", action="store_true", help="实际执行进度定位和课件滚动；默认只打印计划")
    parser.add_argument("--close-when-done", action="store_true", help="完成后不等待，立即关闭本脚本创建的 Chrome 窗口")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report, plan = load_inventory(args.inventory)
    video_count = sum(item["expected_video_count"] for item in plan)
    courseware_count = sum(item["expected_courseware_count"] for item in plan)
    print(
        f"清单 {len(plan)} 节：视频 {video_count} 项、课件 {courseware_count} 项；测验项会忽略。",
        flush=True,
    )
    if not args.execute:
        for section in plan:
            print(
                f"[{section['order']}/{len(plan)}] {section['title']}: "
                f"视频 {section['expected_video_count']}，课件 {section['expected_courseware_count']}",
                flush=True,
            )
        print("计划预览完成；未打开页面或修改学习进度。执行时加 --execute。", flush=True)
        return 0

    course_id = str((report.get("course") or {}).get("course_id") or "")
    course_name = "科技文献检索与利用"
    entry_url = str((report.get("course") or {}).get("entry_url") or "")
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / "resource-completion.json"
    result: dict[str, Any] = {
        "course": {"course_id": course_id, "entry_url": safe_url(entry_url)},
        "started_at": now_iso(),
        "method": "seek-video-to-end-and-scroll-courseware; quizzes untouched",
        "sections": [],
    }

    with sync_playwright() as playwright:
        context: BrowserContext = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.profile_dir.resolve()),
            channel="chrome",
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(entry_url, wait_until="domcontentloaded", timeout=45000)
            course_page = find_target_course_page(context.pages, course_name, course_id or None)
            if course_page is None and is_login_page(page):
                print("需要在打开的 Chrome 窗口手动完成登录；脚本不会填写凭据。", flush=True)
                wait_for_manual_login(page, args.login_timeout, context, course_name, course_id or None)
                course_page = find_target_course_page(context.pages, course_name, course_id or None)
            if course_page is None:
                deadline = time.monotonic() + args.login_timeout
                while time.monotonic() < deadline:
                    course_page = find_target_course_page(context.pages, course_name, course_id or None)
                    if course_page:
                        break
                    page.wait_for_timeout(1000)
            if course_page is None:
                raise TimeoutError("没有识别到目标课程页；未执行任何小节操作")

            page = course_page
            if course_id and query_value(page.url, ("courseId", "courseid")) != course_id:
                raise RuntimeError("当前页面课程 ID 与清单不一致；停止以避免操作其他课程")
            result["course"]["entry_url"] = safe_url(page.url)

            for section in plan:
                item_result: dict[str, Any] = {
                    "section_id": section["section_id"],
                    "order": section["order"],
                    "title": section["title"],
                    "section_url": safe_url(section["section_url"]),
                    "video_results": [],
                    "courseware_results": [],
                }
                try:
                    page.goto(section["section_url"], wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(900)
                    wait_for_frames_to_stabilize(page)
                    if is_login_page(page):
                        item_result["status"] = "login_required"
                        item_result["error"] = "登录状态失效；后续小节未操作"
                        result["sections"].append(item_result)
                        write_progress(output_path, result)
                        break

                    videos, courseware = visible_resource_frames(page)
                    item_result["found_video_count"] = len(videos)
                    item_result["found_courseware_count"] = len(courseware)
                    if not resource_counts_match(
                        section["expected_video_count"],
                        section["expected_courseware_count"],
                        len(videos),
                        len(courseware),
                    ):
                        item_result["status"] = "inventory_mismatch_skipped"
                        item_result["error"] = "页面实时资源数与清单不一致；本节未触碰视频或课件"
                    else:
                        for index, frame in enumerate(videos, start=1):
                            lock_state, marker_text = (None, "")
                            try:
                                body_text = frame.locator("body").inner_text(timeout=1000)
                            except Exception:
                                body_text = ""
                            try:
                                element = frame.frame_element()
                                contexts = element.evaluate(
                                    """el => {
                                      const selector = 'video, iframe[src*="/video/"], iframe[src*="modules/video"]';
                                      const contexts = [];
                                      for (let node = el.parentElement, d = 0; node && d < 8; node = node.parentElement, d++) {
                                        const videos = [...node.querySelectorAll(selector)];
                                        if (videos.length > 1) break;
                                        if (videos.length === 1) contexts.push((node.innerText || '').slice(0, 1200));
                                      }
                                      return contexts;
                                    }"""
                                )
                                lock_state, marker_text = classify_video_marker_context(
                                    body_text, [str(text) for text in contexts], bool(contexts)
                                )
                            except Exception:
                                lock_state = True if VIDEO_LOCK_RE.search(body_text) else None
                            video_result = seek_video_to_end(frame)
                            video_result.update(
                                {
                                    "ordinal": index,
                                    "resource_id": frame_resource_id(frame),
                                    "locked_marker_found": lock_state is True,
                                }
                            )
                            if marker_text and VIDEO_LOCK_RE.search(marker_text):
                                video_result["marker_excerpt"] = VIDEO_LOCK_RE.search(marker_text).group(0)
                            item_result["video_results"].append(video_result)
                            write_progress(output_path, result | {"sections": result["sections"] + [item_result]})

                        for index, frame in enumerate(courseware, start=1):
                            document_result = scroll_courseware_to_bottom(frame)
                            document_result.update(
                                {"ordinal": index, "resource_id": frame_resource_id(frame)}
                            )
                            item_result["courseware_results"].append(document_result)
                            write_progress(output_path, result | {"sections": result["sections"] + [item_result]})

                        statuses = [
                            asset["status"]
                            for asset in item_result["video_results"] + item_result["courseware_results"]
                        ]
                        item_result["status"] = "complete" if all(
                            status in {"complete", "already_at_end", "already_at_bottom"}
                            for status in statuses
                        ) else "partial"
                except PlaywrightTimeoutError as exc:
                    item_result["status"] = "navigation_timeout"
                    item_result["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                except Exception as exc:
                    item_result["status"] = "failed"
                    item_result["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                result["sections"].append(item_result)
                write_progress(output_path, result)
                print(
                    f"[{section['order']}/{len(plan)}] {section['title']}: "
                    f"视频 {len(item_result['video_results'])}/{section['expected_video_count']}，"
                    f"课件 {len(item_result['courseware_results'])}/{section['expected_courseware_count']}，"
                    f"状态 {item_result.get('status', 'unknown')}",
                    flush=True,
                )
                if item_result.get("status") in {"login_required", "failed", "navigation_timeout"}:
                    break

            result["finished_at"] = now_iso()
            write_progress(output_path, result)
            print(f"结果已写入：{output_path.resolve()}", flush=True)
            if not args.close_when_done:
                print("Chrome 保持开启；关闭该窗口即可退出脚本。", flush=True)
                while any(not open_page.is_closed() for open_page in context.pages):
                    time.sleep(1)
            return 0 if all(s.get("status") == "complete" for s in result["sections"]) else 2
        except Exception as exc:
            print(f"执行错误：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            result["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
            write_progress(output_path, result)
            while any(not open_page.is_closed() for open_page in context.pages):
                time.sleep(1)
            return 1
        finally:
            context.close()


if __name__ == "__main__":
    sys.exit(main())
