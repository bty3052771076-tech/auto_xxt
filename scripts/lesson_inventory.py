#!/usr/bin/env python
"""Read-only inventory of Chaoxing course sections using Playwright.

This script reads the course outline and embedded resource frames. It never
plays or seeks videos, opens quiz answers, or submits work.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

try:
    from playwright.sync_api import BrowserContext, Frame, Page, TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError as exc:  # pragma: no cover - exercised only without the dependency
    raise SystemExit(
        "未找到 Python Playwright。当前工作区没有安装依赖；请在非 C 盘的虚拟环境中配置后重试。"
    ) from exc


DEFAULT_URL = "https://mooc1.chaoxing.com/mycourse/studentcourse"
DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / ".private" / "chaoxing-profile"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "output" / "lesson-inventory"
VIDEO_LOCK_RE = re.compile(r"不可拖拽|不可跳过|不能拖拽|不允许拖拽|禁止拖拽|不可拖动")
VIDEO_DURATION_REQUIREMENT_RE = re.compile(
    r"观看时长\s*(?:需\s*)?[≥＞>]\s*总时长的\s*90\s*%|观看时长\s*需达到\s*90\s*%"
)
VIDEO_HINT_RE = re.compile(r"不可拖拽|不可跳过|不能拖拽|不允许拖拽|禁止拖拽|不可拖动|不可倍速")
QUESTION_COUNT_PATTERNS = (
    re.compile(r"题量\s*[:：]?\s*(\d+)"),
    re.compile(r"题目数量\s*[:：]?\s*(\d+)"),
    re.compile(r"共\s*(\d+)\s*题"),
)
ID_PARAM_NAMES = ("objectid", "objectId", "videoid", "videoId", "resid", "resId", "workid", "workId", "jobid", "jobId", "attachmentid", "attachmentId", "id")
SENSITIVE_QUERY_NAMES = {"enc", "token", "authorization", "ticket", "cpi", "userid", "user_id", "sign", "signature"}


@dataclass
class SectionRef:
    section_id: str
    title: str
    section_path: str
    order: int
    url: str
    source: str


@dataclass
class SectionResult:
    section_id: str
    section_path: str
    title: str
    order: int
    video_total: int | None = 0
    video_skippable: int | None = 0
    video_unskippable: int | None = 0
    video_classification_unknown: int = 0
    courseware_total: int | None = 0
    courseware_types: dict[str, int] = field(default_factory=dict)
    question_total: int | None = 0
    status: str = "complete"
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def safe_url(url: str) -> str:
    """Remove session and identity parameters from evidence URLs."""
    parts = urlsplit(url)
    clean_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in SENSITIVE_QUERY_NAMES
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(clean_query), ""))


def query_value(url: str, names: tuple[str, ...]) -> str | None:
    wanted = {name.lower() for name in names}
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key.lower() in wanted and value:
            return value
    return None


def is_login_page(page: Page) -> bool:
    url = page.url.lower()
    if any(part in url for part in ("/login", "/passport", "login.chaoxing")):
        return True
    try:
        passwords = page.locator('input[type="password"]')
        has_visible_password = any(passwords.nth(i).is_visible() for i in range(min(passwords.count(), 3)))
        title = page.title().lower()
        body = page.locator("body").inner_text(timeout=1500)[:500]
    except Exception:
        return False
    title_is_login = "login" in title or "登录" in title
    return title_is_login or (has_visible_password and ("登录" in body or "login" in title))


def wait_for_manual_login(
    page: Page,
    timeout_seconds: int,
    context: BrowserContext,
    course_name: str,
    expected_course_id: str | None = None,
) -> Page | None:
    target_page = find_target_course_page(context.pages, course_name, expected_course_id)
    if target_page is not None:
        return target_page
    if not is_login_page(page):
        return None
    print(
        "浏览器已打开课程登录页。请在浏览器中手动登录；脚本会在登录完成后继续。",
        flush=True,
    )
    deadline = time.monotonic() + timeout_seconds
    next_notice = time.monotonic() + 20
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        target_page = find_target_course_page(context.pages, course_name, expected_course_id)
        if target_page is not None:
            return target_page
        if not is_login_page(page):
            return None
        if time.monotonic() >= next_notice:
            print(f"仍在等待认证完成（页面标题：{page.title()[:80]}）。", flush=True)
            next_notice = time.monotonic() + 20
    raise TimeoutError(f"等待手动登录超过 {timeout_seconds} 秒")


def normalize_section_url(entry_url: str, section_id: str, candidate_url: str = "") -> str:
    if candidate_url and not candidate_url.lower().startswith(("javascript:", "#")):
        return urljoin(entry_url, candidate_url)
    parts = urlsplit(entry_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in {"chapterid", "knowledgeid", "knowledge_id"}]
    query.append(("chapterId", section_id))
    path = parts.path
    if "studentstudy" not in path.lower():
        path = "/mycourse/studentstudy"
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def collect_outline_candidates(page: Page) -> list[dict[str, Any]]:
    """Find page links and data attributes that identify course knowledge nodes."""
    return page.evaluate(
        """() => {
          const idKeys = ['chapterId', 'chapterid', 'knowledgeId', 'knowledgeid', 'knowledge_id'];
          const paramId = href => {
            try {
              const u = new URL(href, location.href);
              for (const [k, v] of u.searchParams.entries()) {
                if (idKeys.includes(k.toLowerCase()) && v) return v;
              }
            } catch (_) {}
            return '';
          };
          const directText = el => {
            const s = [...el.childNodes]
              .filter(n => n.nodeType === Node.TEXT_NODE)
              .map(n => n.textContent.trim()).filter(Boolean).join(' ');
            return s || (el.querySelector(':scope > a, :scope > span, :scope > div')?.innerText || '').trim();
          };
          const rows = [];
          const seen = new Set();
          const add = (el, href, id, source) => {
            if (!id) return;
            let title = (el.innerText || el.getAttribute('title') || el.getAttribute('aria-label') || '').trim();
            title = title.replace(/\\s+/g, ' ').slice(0, 180);
            if (!title) return;
            let item = el.closest('li, [role="treeitem"]') || el.parentElement;
            const path = [];
            for (let n = item, depth = 0; n && depth < 8; n = n.parentElement, depth++) {
              if (n.matches?.('li, [role="treeitem"]')) {
                const label = directText(n).replace(/\\s+/g, ' ').slice(0, 100);
                if (label && !path.includes(label)) path.unshift(label);
              }
            }
            const key = String(id);
            if (seen.has(key)) return;
            seen.add(key);
            rows.push({ id: key, title, href: href || '', path, source });
          };
          for (const el of document.querySelectorAll('a[href]')) {
            const href = el.getAttribute('href') || '';
            const id = paramId(href);
            const isStudyLink = /studentstudy/i.test(href) || /chapterid|knowledgeid/i.test(href);
            if (id && isStudyLink) add(el, href, id, 'link');
          }
          const attrNames = ['data-chapter-id', 'data-chapterid', 'data-knowledge-id', 'data-knowledgeid', 'chapterid', 'knowledgeid'];
          for (const el of document.querySelectorAll('[data-chapter-id], [data-chapterid], [data-knowledge-id], [data-knowledgeid], [chapterid], [knowledgeid]')) {
            let id = '';
            for (const name of attrNames) {
              id = el.getAttribute(name) || '';
              if (id) break;
            }
            const href = el.matches('a[href]') ? el.getAttribute('href') : (el.querySelector('a[href]')?.getAttribute('href') || '');
            add(el, href, id, 'data-attribute');
          }
          return rows;
        }"""
    )


def section_refs_from_page(page: Page, entry_url: str) -> list[SectionRef]:
    raw = collect_outline_candidates(page)
    refs: list[SectionRef] = []
    for item in raw:
        section_id = str(item["id"])
        title = re.sub(r"\s+", " ", str(item["title"])).strip()
        path = " / ".join(p for p in item.get("path", []) if p) or title
        candidate_url = str(item.get("href", ""))
        url = normalize_section_url(entry_url, section_id, candidate_url)
        refs.append(SectionRef(section_id, title, path, len(refs) + 1, url, str(item.get("source", ""))))

    if not refs and "studentstudy" in page.url.lower():
        current_id = query_value(page.url, ("chapterId", "knowledgeId", "knowledge_id"))
        if current_id:
            title = page.title().strip() or f"小节 {current_id}"
            refs.append(SectionRef(current_id, title, title, 1, page.url, "current-page"))
    return refs


def candidate_outline_url(entry_url: str) -> str:
    parts = urlsplit(entry_url)
    path = parts.path
    if "studentstudy" in path.lower():
        path = "/mycourse/studentcourse"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"chapterid", "knowledgeid", "knowledge_id"}
    ]
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def course_route_url(url: str, expected_course_id: str | None) -> bool:
    path = urlsplit(url).path.lower()
    course_id = query_value(url, ("courseId", "courseid"))
    return (
        expected_course_id is not None
        and course_id == expected_course_id
        and ("studentcourse" in path or "studentstudy" in path)
    )


def is_course_page_location(
    url: str,
    course_name: str,
    page_text: str,
    expected_course_id: str | None = None,
) -> bool:
    """Match by visible course name or a caller-supplied, exact course ID."""
    path = urlsplit(url).path.lower()
    if not any(route in path for route in ("studentcourse", "studentstudy")):
        return False
    normalized_name = re.sub(r"\s+", "", course_name)
    normalized_text = re.sub(r"\s+", "", page_text)
    if normalized_name and normalized_name in normalized_text:
        return True
    return bool(
        expected_course_id
        and query_value(url, ("courseId", "courseid")) == expected_course_id
    )


def find_target_course_page(
    pages: list[Page],
    course_name: str,
    expected_course_id: str | None = None,
) -> Page | None:
    for candidate_page in pages:
        candidate_path = urlsplit(candidate_page.url).path.lower()
        if not any(route in candidate_path for route in ("studentcourse", "studentstudy")):
            continue
        if is_login_page(candidate_page):
            continue
        try:
            page_text = candidate_page.title() + "\n" + candidate_page.locator("body").inner_text(timeout=1500)
        except Exception:
            page_text = candidate_page.title()
        if is_course_page_location(
            candidate_page.url, course_name, page_text, expected_course_id
        ):
            return candidate_page
    return None


def should_keep_browser_open_on_error(error: Exception | None, pages_open: bool) -> bool:
    return error is not None and pages_open


def wait_for_frames_to_stabilize(page: Page, timeout_ms: int = 12000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    last_signature = None
    stable_rounds = 0
    while time.monotonic() < deadline:
        signature = tuple(sorted((frame.url or "") for frame in page.frames))
        if signature == last_signature:
            stable_rounds += 1
            if stable_rounds >= 3:
                return
        else:
            stable_rounds = 0
            last_signature = signature
        page.wait_for_timeout(400)


def frame_kind(frame_url: str, body_text: str) -> str | None:
    parsed = urlsplit(frame_url.lower())
    path_query = f"{parsed.path}?{parsed.query}"
    if re.search(r"/modules/video(?:/|\.)|/(?:video|player)/|\.mp4(?:$|\?)", path_query):
        return "video"
    if re.search(r"/modules/(?:pdf|ppt|office|document)(?:/|\.)|\.(?:pdf|pptx?|docx?)(?:$|\?)|/(?:pdf|ppt|office)/", path_query):
        return "courseware"
    if re.search(r"/modules/work(?:/|\\.)|/work/|/exam/|/quiz/", path_query):
        return "quiz"
    if re.search(r"题量\s*[:：]?\s*\d+|共\s*\d+\s*题", body_text):
        return "quiz"
    return None


def frame_resource_id(frame: Frame) -> str | None:
    value = query_value(frame.url, ID_PARAM_NAMES)
    if value:
        return value
    try:
        element = frame.frame_element()
        attrs = element.evaluate(
            "el => ({id: el.id, name: el.name, dataId: el.getAttribute('data-id'), "
            "dataObjectid: el.getAttribute('data-objectid'), title: el.title})"
        )
        for key in ("dataObjectid", "dataId", "id", "name"):
            if attrs.get(key):
                return str(attrs[key])
    except Exception:
        pass
    return None


def video_marker_context(frame: Frame) -> tuple[bool | None, str]:
    """Read the video frame and its unique-video ancestors for restriction text."""
    body_text = ""
    try:
        body_text = frame.locator("body").inner_text(timeout=1500)
    except Exception:
        pass

    try:
        element = frame.frame_element()
        ancestor_contexts = element.evaluate(
            """el => {
              const selector = 'video, iframe[src*="/video/"], iframe[src*="modules/video"]';
              const contexts = [];
              let node = el.parentElement;
              // The iframe element itself has no surrounding label text. Start
              // at its parent and keep ascending while the container identifies
              // exactly one video, so a marker in an outer task wrapper is seen.
              for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
                const videos = [...node.querySelectorAll(selector)];
                if (videos.length > 1) break;
                if (videos.length === 1) contexts.push((node.innerText || '').slice(0, 1200));
              }
              return {associated: contexts.length > 0, contexts};
            }"""
        )
    except Exception:
        return classify_video_marker_context(body_text, [], False)

    wrapper_texts = [str(text) for text in ancestor_contexts.get("contexts", [])]
    return classify_video_marker_context(
        body_text,
        wrapper_texts,
        bool(ancestor_contexts.get("associated")),
    )


def classify_video_marker_context(
    body_text: str,
    wrapper_texts: list[str],
    associated_wrapper_found: bool,
) -> tuple[bool | None, str]:
    """Classify only from a marker or readable text tied to one video."""
    for text in [body_text, *wrapper_texts]:
        if VIDEO_LOCK_RE.search(text) or VIDEO_DURATION_REQUIREMENT_RE.search(text):
            return True, text

    if associated_wrapper_found:
        for text in wrapper_texts:
            if text.strip():
                return False, text
        if wrapper_texts:
            return False, ""
    return None, ""


def count_questions(frame: Frame, body_text: str) -> tuple[int | None, int | None, str | None]:
    header_count = None
    for pattern in QUESTION_COUNT_PATTERNS:
        match = pattern.search(body_text)
        if match:
            header_count = int(match.group(1))
            break

    dom_count = None
    try:
        dom_count = frame.locator("body").evaluate(
            """body => {
              const selectors = [
                '[data-question-id]', 'li.questionLi', '.questionLi', '.TiMu',
                '.question-item', '.questionItem'
              ];
              for (const selector of selectors) {
                const found = [...body.querySelectorAll(selector)];
                if (found.length) {
                  const ids = found.map(el => el.getAttribute('data-question-id') || el.id).filter(Boolean);
                  return ids.length ? new Set(ids).size : found.length;
                }
              }
              return 0;
            }"""
        )
        dom_count = int(dom_count)
    except Exception:
        dom_count = None

    if header_count is not None:
        if dom_count is not None and dom_count > header_count:
            return None, header_count, f"question_count_conflict: header={header_count}, rendered={dom_count}"
        warning = None
        if dom_count is not None and dom_count < header_count:
            warning = f"rendered_questions_below_header: header={header_count}, rendered={dom_count}"
        return header_count, dom_count, warning
    if dom_count is not None:
        return dom_count, dom_count, None
    return None, None, "question_count_unreadable"


def unique_assets(page: Page) -> tuple[list[dict[str, Any]], list[str]]:
    assets: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    for frame_index, frame in enumerate(page.frames):
        try:
            body_text = frame.locator("body").inner_text(timeout=1500)
        except Exception:
            body_text = ""
        kind = frame_kind(frame.url, body_text)
        if not kind:
            if "/modules/" in frame.url.lower():
                warnings.append(f"unsupported_embedded_module:{safe_url(frame.url)}")
            continue
        resource_id = frame_resource_id(frame)
        if resource_id:
            key = (kind, resource_id)
        else:
            key = (kind, f"frame-{frame_index}")
            warnings.append(f"{kind}_resource_id_missing:frame-{frame_index}")
        if key in seen:
            continue
        seen.add(key)

        asset: dict[str, Any] = {
            "kind": kind,
            "resource_id": resource_id,
            "source_url": safe_url(frame.url),
            "frame_index": frame_index,
        }
        if kind == "video":
            locked, marker_text = video_marker_context(frame)
            if locked is None:
                asset["classification"] = "unknown"
                asset["classification_evidence"] = "video_marker_region_unreadable"
                warnings.append(f"video_marker_region_unreadable:frame-{frame_index}")
            else:
                asset["classification"] = "unskippable" if locked else "skippable"
                asset["classification_evidence"] = "locking_hint_found" if locked else "no_locking_hint_in_loaded_video_container"
            if marker_text and VIDEO_HINT_RE.search(marker_text):
                asset["marker_excerpt"] = VIDEO_HINT_RE.search(marker_text).group(0)
        elif kind == "courseware":
            path = urlsplit(frame.url).path.lower()
            suffix = Path(path).suffix.lstrip(".")
            if suffix in {"pdf", "ppt", "pptx", "doc", "docx"}:
                asset["courseware_type"] = suffix
            elif "pdf" in path:
                asset["courseware_type"] = "pdf"
            elif "ppt" in path:
                asset["courseware_type"] = "ppt"
            elif "office" in path or "document" in path:
                asset["courseware_type"] = "office"
            else:
                asset["courseware_type"] = "other"
        elif kind == "quiz":
            total, rendered, warning = count_questions(frame, body_text)
            asset["question_total"] = total
            asset["question_rendered"] = rendered
            if warning:
                asset["warning"] = warning
                warnings.append(warning)
        assets.append(asset)
    return assets, warnings


def extract_section(page: Page, section: SectionRef) -> SectionResult:
    result = SectionResult(
        section_id=section.section_id,
        section_path=section.section_path,
        title=section.title,
        order=section.order,
    )
    response = page.goto(section.url, wait_until="domcontentloaded", timeout=45000)
    if response is not None and response.status >= 400:
        result.status = "failed"
        result.video_total = result.video_skippable = result.video_unskippable = None
        result.courseware_total = result.question_total = None
        result.warnings.append(f"http_status:{response.status}")
        return result
    page.wait_for_timeout(900)
    wait_for_frames_to_stabilize(page)
    if is_login_page(page):
        result.status = "failed"
        result.video_total = result.video_skippable = result.video_unskippable = None
        result.courseware_total = result.question_total = None
        result.warnings.append("login_required_during_collection")
        return result

    assets, warnings = unique_assets(page)
    result.warnings.extend(warnings)
    video_assets = [item for item in assets if item["kind"] == "video"]
    courseware_assets = [item for item in assets if item["kind"] == "courseware"]
    quiz_assets = [item for item in assets if item["kind"] == "quiz"]

    result.video_total = len(video_assets)
    result.video_skippable = sum(item.get("classification") == "skippable" for item in video_assets)
    result.video_unskippable = sum(item.get("classification") == "unskippable" for item in video_assets)
    result.video_classification_unknown = sum(item.get("classification") == "unknown" for item in video_assets)
    result.courseware_total = len(courseware_assets)
    for item in courseware_assets:
        kind = item.get("courseware_type", "other")
        result.courseware_types[kind] = result.courseware_types.get(kind, 0) + 1

    if quiz_assets:
        if any(item.get("question_total") is None for item in quiz_assets):
            result.question_total = None
            result.warnings.append("one_or_more_quiz_counts_unreadable")
        else:
            result.question_total = sum(int(item["question_total"]) for item in quiz_assets)

    result.evidence = assets
    if result.video_classification_unknown or result.question_total is None or result.warnings:
        result.status = "partial"
    return result


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, results: list[SectionResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "section_id", "section_path", "title", "order", "video_total", "video_skippable",
        "video_unskippable", "video_classification_unknown", "courseware_total", "courseware_types",
        "question_total", "status", "warnings",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in results:
            row = asdict(result)
            row["courseware_types"] = json.dumps(row["courseware_types"], ensure_ascii=False)
            row["warnings"] = " | ".join(row["warnings"])
            row.pop("evidence", None)
            writer.writerow({key: row.get(key) for key in fields})


def build_report(entry_url: str, sections: list[SectionRef], results: list[SectionResult]) -> dict[str, Any]:
    course_id = query_value(entry_url, ("courseId", "courseid"))
    clazz_id = query_value(entry_url, ("clazzid", "classId"))
    return {
        "course": {"course_id": course_id, "class_id": clazz_id, "entry_url": safe_url(entry_url)},
        "captured_at": now_iso(),
        "adapter": "chaoxing-v1",
        "section_count": len(sections),
        "complete_section_count": sum(result.status == "complete" for result in results),
        "partial_section_count": sum(result.status == "partial" for result in results),
        "failed_section_count": sum(result.status == "failed" for result in results),
        "sections": [asdict(result) for result in results],
    }


def save_progress(output_dir: Path, entry_url: str, sections: list[SectionRef], results: list[SectionResult]) -> None:
    report = build_report(entry_url, sections, results)
    write_json(output_dir / "lesson-inventory.partial.json", report)
    write_csv(output_dir / "lesson-inventory.partial.csv", results)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统计超星课程各小节的视频、课件和测验题量")
    parser.add_argument("--url", default=DEFAULT_URL, help="课程目录页或任意小节页 URL")
    parser.add_argument("--course-name", default="科技文献检索与利用", help="目标课程名称，用于确认打开的是正确课程")
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE, help="E 盘专用浏览器资料目录")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT, help="JSON/CSV 输出目录")
    parser.add_argument("--login-timeout", type=int, default=600, help="等待手动登录的秒数")
    parser.add_argument(
        "--assume-authenticated",
        action="store_true",
        help="复用资料目录中的登录态并直接尝试入口；入口仍要求登录时立即报告",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    entry_url = args.url
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context: BrowserContext = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.profile_dir.resolve()),
            channel="chrome",
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            print(f"打开课程入口：{safe_url(entry_url)}", flush=True)
            page.goto(entry_url, wait_until="domcontentloaded", timeout=45000)
            expected_course_id = query_value(entry_url, ("courseId", "courseid"))
            target_course_page: Page | None = None
            if args.assume_authenticated:
                print(f"尝试使用现有登录态：{safe_url(page.url)}", flush=True)
            else:
                target_course_page = wait_for_manual_login(
                    page, args.login_timeout, context, args.course_name, expected_course_id
                )
                if target_course_page is not None:
                    page = target_course_page
            print(f"登录页已离开，当前页面：{safe_url(page.url)}", flush=True)

            if args.assume_authenticated and is_login_page(page):
                raise RuntimeError("资料目录中的登录态未能通过课程入口认证")

            if target_course_page is None:
                print(
                    f"请在此 Chrome 窗口打开目标课程“{args.course_name}”；"
                    "脚本不会再强行重开未经验证的预设课程链接。",
                    flush=True,
                )
            course_deadline = time.monotonic() + args.login_timeout
            next_course_notice = time.monotonic() + 20
            while target_course_page is None and time.monotonic() < course_deadline:
                target_course_page = find_target_course_page(
                    context.pages, args.course_name, expected_course_id
                )
                if target_course_page is not None:
                    break
                if time.monotonic() >= next_course_notice:
                    current_urls = ", ".join(safe_url(candidate_page.url) for candidate_page in context.pages)
                    print(f"仍在等待目标课程页面（当前：{current_urls or '无打开页面'}）。", flush=True)
                    next_course_notice = time.monotonic() + 20
                page.wait_for_timeout(1000)
            if target_course_page is None:
                raise TimeoutError(f"等待目标课程页面超过 {args.login_timeout} 秒")

            page = target_course_page
            entry_url = page.url
            print(f"已确认目标课程页面：{safe_url(entry_url)}", flush=True)

            outline_url = candidate_outline_url(entry_url)
            if outline_url != entry_url:
                print("读取课程目录页。", flush=True)
                page.goto(outline_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(700)
                wait_for_frames_to_stabilize(page, timeout_ms=5000)
            sections = section_refs_from_page(page, entry_url)
            if not sections and "studentstudy" in entry_url.lower():
                page.goto(entry_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(700)
                sections = section_refs_from_page(page, entry_url)
            if not sections:
                diagnostic = {
                    "url": safe_url(page.url),
                    "title": page.title()[:180],
                    "anchor_count": page.locator("a[href]").count(),
                    "data_node_count": page.locator(
                        "[data-chapter-id], [data-chapterid], [data-knowledge-id], [data-knowledgeid], [chapterid], [knowledgeid]"
                    ).count(),
                    "frame_count": len(page.frames),
                }
                write_json(args.output_dir / "outline-diagnostic.json", diagnostic)
                raise RuntimeError(
                    "未能从课程目录识别小节。已保存脱敏诊断；请提供完整课程入口 URL 或检查适配器目录选择器。"
                )

            print(f"目录识别到 {len(sections)} 个小节，开始逐个只读统计。", flush=True)
            results: list[SectionResult] = []
            for section in sections:
                try:
                    result = extract_section(page, section)
                except PlaywrightTimeoutError as exc:
                    result = SectionResult(
                        section_id=section.section_id,
                        section_path=section.section_path,
                        title=section.title,
                        order=section.order,
                        video_total=None,
                        video_skippable=None,
                        video_unskippable=None,
                        courseware_total=None,
                        question_total=None,
                        status="failed",
                        warnings=[f"navigation_timeout:{type(exc).__name__}"],
                    )
                except Exception as exc:
                    result = SectionResult(
                        section_id=section.section_id,
                        section_path=section.section_path,
                        title=section.title,
                        order=section.order,
                        video_total=None,
                        video_skippable=None,
                        video_unskippable=None,
                        courseware_total=None,
                        question_total=None,
                        status="failed",
                        warnings=[f"collection_error:{type(exc).__name__}:{str(exc)[:240]}"],
                    )
                results.append(result)
                save_progress(args.output_dir, entry_url, sections, results)
                print(
                    f"[{section.order}/{len(sections)}] {section.section_path}: "
                    f"视频 {result.video_total}（可跳 {result.video_skippable}/不可跳 {result.video_unskippable}），"
                    f"课件 {result.courseware_total}，题目 {result.question_total}，状态 {result.status}",
                    flush=True,
                )

            report = build_report(entry_url, sections, results)
            write_json(args.output_dir / "lesson-inventory.json", report)
            write_csv(args.output_dir / "lesson-inventory.csv", results)
            print(f"采集完成：{args.output_dir.resolve()}", flush=True)
            print(
                f"总计 {len(results)} 节；完整 {report['complete_section_count']}，"
                f"部分 {report['partial_section_count']}，失败 {report['failed_section_count']}。",
                flush=True,
            )
            print("采集完成后 Chrome 保持开启；关闭浏览器窗口即可退出脚本。", flush=True)
            while any(not open_page.is_closed() for open_page in context.pages):
                time.sleep(1)
            return 0 if report["failed_section_count"] == 0 else 2
        except Exception as exc:
            print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            pages_open = any(not open_page.is_closed() for open_page in context.pages)
            if should_keep_browser_open_on_error(exc, pages_open):
                print("采集未完成；Chrome 保持开启以便检查。关闭窗口后脚本退出。", flush=True)
                while any(not open_page.is_closed() for open_page in context.pages):
                    time.sleep(1)
            return 1
        finally:
            context.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
