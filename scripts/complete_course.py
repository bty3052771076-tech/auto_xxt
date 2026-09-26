#!/usr/bin/env python3
"""One-shot, resumable course runner: resources, quizzes, and verified answers.

For each section, seek skippable video, watch marked locked video at 1x,
scroll courseware, then complete/review its quiz. All work is checkpointed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, sync_playwright

try:
    from . import lesson_completion, lesson_inventory, quiz_completion
except ImportError:
    import lesson_completion  # type: ignore[no-redef]
    import lesson_inventory  # type: ignore[no-redef]
    import quiz_completion  # type: ignore[no-redef]


DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "output" / "complete-course"


def combined_section_status(resource_state: str, quiz_state: str) -> str:
    if resource_state == "failed":
        return "failed"
    if resource_state != "complete":
        return "partial"
    if quiz_state == "pending_review":
        return "pending_review"
    if quiz_state in {"fully_correct", "none"}:
        return "complete"
    return "partial"


def video_composition(resources: list[dict[str, Any]]) -> dict[str, int]:
    videos = [item for item in resources if item.get("kind") == "video"]
    return {
        "total": len(videos),
        "skippable": sum(item.get("classification") == "skippable" for item in videos),
        "unskippable": sum(item.get("classification") == "unskippable" for item in videos),
        "unknown": sum(item.get("classification") == "unknown" for item in videos),
    }


def can_skip_section_checkpoint(record: dict[str, Any] | None, restart: bool, skip_quizzes: bool) -> bool:
    if not record or restart or record.get("status") != "complete":
        return False
    if record.get("quiz", {}).get("state") == "skipped" and not skip_quizzes:
        return False
    return True


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_existing(path: Path, course_id: str) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if str(data.get("course_id")) != course_id:
        raise ValueError(f"existing report {path} belongs to a different course")
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="逐节完成视频、课件和测验，记录视频组成及经页面核验的答案")
    parser.add_argument("--inventory", type=Path, default=lesson_completion.DEFAULT_INVENTORY)
    parser.add_argument("--url", help="课程小节或目录 URL；默认使用清单入口")
    parser.add_argument("--profile-dir", type=Path, default=lesson_completion.DEFAULT_PROFILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--answers-file", type=Path, default=quiz_completion.DEFAULT_OUTPUT / "verified-answers.json")
    parser.add_argument("--answer-key", type=Path, help="批阅页不公开答案时使用的人工核对键；仍须页面显示 100 分才能归档")
    parser.add_argument("--course-name", default="科技文献检索与利用", help="登录后确认课程名称")
    parser.add_argument("--section-id", help="只处理指定小节，用于验证/修复")
    parser.add_argument("--start-section-id", help="从指定小节开始顺序处理")
    parser.add_argument("--stop-section-id", help="处理到指定小节为止")
    parser.add_argument("--assume-authenticated", action="store_true")
    parser.add_argument("--restart", action="store_true", help="忽略已有的逐节完成检查点，重新核查所选小节")
    parser.add_argument("--skip-restricted-video", action="store_true", help="不播放页面标为不可拖拽的视频")
    parser.add_argument("--skip-quizzes", action="store_true", help="仅处理视频和课件，不提交测验")
    parser.add_argument("--close-on-finish", action="store_true", help="任务结束时关闭专用 Chrome；默认保持打开")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    inventory_data = lesson_completion.load_inventory(args.inventory)
    entry_url = args.url or inventory_data["course"]["entry_url"]
    course_id = str(inventory_data["course"].get("course_id") or "")
    all_sections = sorted(inventory_data["sections"], key=lambda item: item.get("order", 0))
    selected = lesson_completion.sections_for_run(
        all_sections, args.section_id, start_section_id=args.start_section_id, stop_section_id=args.stop_section_id
    )
    allowed_ids = {str(section["section_id"]) for section in selected}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "complete-course.json"
    previous = read_existing(report_path, course_id)
    records_by_id: dict[str, dict[str, Any]] = {
        str(item["section_id"]): item for item in previous.get("sections", [])
    }
    selected_ids = {str(section["section_id"]) for section in selected}
    answer_data = read_existing(args.answers_file, course_id)
    verified_sections = answer_data.get("sections", {})
    manual_keys: dict[str, dict[str, str]] = {}
    if args.answer_key:
        supplied = json.loads(args.answer_key.read_text(encoding="utf-8"))
        if str(supplied.get("course_id")) != course_id:
            raise ValueError("answer-key course ID differs from inventory")
        manual_keys = supplied.get("sections", {})

    def checkpoint() -> None:
        ordered = sorted(records_by_id.values(), key=lambda item: item.get("order", 0))
        counts = {state: sum(item.get("status") == state for item in ordered)
                  for state in ("complete", "pending_review", "partial", "failed", "processing")}
        quiz_completion.write_json(report_path, {
            "course_id": course_id,
            "inventory_path": str(args.inventory.resolve()),
            "updated_at": now_iso(),
            "selected_section_count": len(selected),
            "counts": counts,
            "sections": ordered,
        })
        quiz_completion.write_json(args.answers_file, {
            "course_id": course_id,
            "verification_rule": "100 score on reviewed page and per-question page key or submitted answer",
            "sections": verified_sections,
        })

    with sync_playwright() as playwright:
        context: BrowserContext = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.profile_dir.resolve()), channel="chrome", headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            outline_url = lesson_completion.initial_course_page_url(entry_url)
            page.goto(outline_url, wait_until="domcontentloaded", timeout=45000)
            if lesson_inventory.is_login_page(page):
                if args.assume_authenticated:
                    raise RuntimeError("Chrome profile login is no longer valid")
                print("请在打开的 Chrome 窗口手动登录；脚本不读取凭据。", flush=True)
                lesson_inventory.wait_for_manual_login(page, 600, context, args.course_name, course_id or None)
                target = lesson_inventory.find_target_course_page(context.pages, args.course_name, course_id or None)
                if target is None:
                    raise RuntimeError("target course page not found after login")
                page = target
            actual_id = lesson_inventory.query_value(page.url, ("courseId", "courseid"))
            if course_id and actual_id and actual_id != course_id:
                raise RuntimeError(f"opened course {actual_id}, expected {course_id}")
            if lesson_inventory.find_target_course_page(context.pages, args.course_name, course_id or None) is None:
                raise RuntimeError("target course not confirmed in this Chrome context")

            for index, section in enumerate(selected, 1):
                section_id = str(section["section_id"])
                previous_record = records_by_id.get(section_id)
                if can_skip_section_checkpoint(previous_record, args.restart, args.skip_quizzes):
                    print(f"[{index}/{len(selected)}] {section.get('section_path')}: 已有完成检查点，跳过", flush=True)
                    continue
                target_url = lesson_completion.section_url_for_id(entry_url, section_id)
                resource_record = lesson_completion.new_resource_record(section, entry_url, index)
                record: dict[str, Any] = {
                    "section_id": section_id,
                    "order": section.get("order", index),
                    "section_path": section.get("section_path"),
                    "title": section.get("title"),
                    "status": "processing",
                    "resources": resource_record,
                    "quiz": {"state": "not_checked"},
                }
                records_by_id[section_id] = record
                checkpoint()
                try:
                    response = page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                    if response is not None and response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}")
                    page.wait_for_timeout(900)
                    if lesson_inventory.is_login_page(page):
                        raise RuntimeError("login expired")
                    resource_record = lesson_completion.process_resources_on_page(
                        page, resource_record, not args.skip_restricted_video, allowed_ids
                    )
                    record["resources"] = resource_record
                    record["video_composition"] = video_composition(resource_record["resources"])
                    checkpoint()

                    has_quiz = any(
                        item.get("kind") == "quiz" for item in section.get("evidence", [])
                    )
                    if not has_quiz:
                        quiz_record: dict[str, Any] = {"state": "none"}
                    elif args.skip_quizzes:
                        quiz_record = {"state": "skipped"}
                    else:
                        quiz_record, verified = quiz_completion.process_section(
                            page, section, complete=True, manual_key=manual_keys.get(section_id)
                        )
                        if verified:
                            verified_sections[section_id] = verified
                    record["quiz"] = quiz_record
                    record["status"] = combined_section_status(resource_record["status"], quiz_record["state"])
                except Exception as exc:
                    record["status"] = "failed"
                    record["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
                record["checked_at"] = now_iso()
                checkpoint()
                composition = record.get("video_composition", {"total": 0, "skippable": 0, "unskippable": 0, "unknown": 0})
                print(
                    f"[{index}/{len(selected)}] {section.get('section_path')}: {record['status']}; "
                    f"视频 {composition['total']}（可跳 {composition['skippable']} / 不可跳 {composition['unskippable']} / 未知 {composition['unknown']}），"
                    f"测验 {record['quiz']['state']}", flush=True,
                )

            checkpoint()
            print(f"逐节报告：{report_path.resolve()}\n已核验答案：{args.answers_file.resolve()}", flush=True)
            if args.close_on_finish:
                context.close()
            else:
                print("Chrome 保持开启供核查；关闭窗口后脚本退出。", flush=True)
                while any(not open_page.is_closed() for open_page in context.pages):
                    time.sleep(1)
            states = [records_by_id[section_id]["status"] for section_id in selected_ids]
            return 0 if all(state == "complete" for state in states) else (2 if "failed" in states else 1)
        except Exception as exc:
            print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if args.close_on_finish:
                context.close()
            else:
                print("Chrome 保持开启供核查；关闭窗口后脚本退出。", flush=True)
                while any(not open_page.is_closed() for open_page in context.pages):
                    time.sleep(1)
            return 2


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
