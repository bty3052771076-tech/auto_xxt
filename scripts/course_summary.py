#!/usr/bin/env python3
"""Conservative course-wide audit of resource reports and verified quiz answers."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VIDEO_DONE = {"seeked_to_end", "watched_threshold_met", "already_completed"}
COURSEWARE_DONE = {"scrolled_to_end"}
ROOT = Path(__file__).resolve().parents[1]


def classify_recorded_video(record: dict[str, Any], inventory_classification: str | None) -> str:
    status = record.get("status")
    if status == "watched_threshold_met":
        return "unskippable"
    if status == "seeked_to_end":
        return "skippable"
    recorded = record.get("classification")
    if recorded in {"skippable", "unskippable"}:
        return recorded
    if inventory_classification in {"skippable", "unskippable"}:
        return inventory_classification
    return "unknown"


def select_verified_resources(
    reports: list[dict[str, Any]], section_id: str, kind: str, expected_count: int | None
) -> list[dict[str, Any]] | None:
    if expected_count is None:
        return None
    if expected_count == 0:
        return []
    accepted = VIDEO_DONE if kind == "video" else COURSEWARE_DONE if kind == "courseware" else set()
    for report in reversed(reports):
        if str(report.get("section_id")) != section_id:
            continue
        resources = [item for item in report.get("resources", []) if item.get("kind") == kind]
        if len(resources) == expected_count and all(
            item.get("status") in accepted
            and not (kind == "video" and item.get("status") in {"watched_threshold_met", "seeked_to_end"}
                     and item.get("task_point_completed") is not True)
            for item in resources
        ):
            return resources
    return None


def summarize_course(
    inventory: dict[str, Any], reports: list[dict[str, Any]], verified_answers: dict[str, Any]
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    archived = verified_answers.get("sections", {})
    for section in sorted(inventory.get("sections", []), key=lambda item: item.get("order", 0)):
        section_id = str(section["section_id"])
        video_total = section.get("video_total")
        courseware_total = section.get("courseware_total")
        video_records = select_verified_resources(reports, section_id, "video", video_total)
        courseware_records = select_verified_resources(reports, section_id, "courseware", courseware_total)
        inventory_classes = [
            item.get("classification") for item in section.get("evidence", []) if item.get("kind") == "video"
        ]
        classification_records = video_records
        if classification_records is None and video_total is not None:
            for report in reversed(reports):
                if str(report.get("section_id")) != section_id:
                    continue
                candidates = [item for item in report.get("resources", []) if item.get("kind") == "video"]
                if len(candidates) == video_total:
                    classification_records = candidates
                    break
        if classification_records is not None:
            classifications = [
                classify_recorded_video(record, inventory_classes[index] if index < len(inventory_classes) else None)
                for index, record in enumerate(classification_records)
            ]
        else:
            classifications = inventory_classes[:]
            if isinstance(video_total, int) and len(classifications) < video_total:
                classifications += ["unknown"] * (video_total - len(classifications))
        if isinstance(video_total, int):
            for report in reports:
                if str(report.get("section_id")) != section_id:
                    continue
                recorded = [item for item in report.get("resources", []) if item.get("kind") == "video"]
                if len(recorded) != video_total:
                    continue
                for index, item in enumerate(recorded):
                    if (item.get("status") == "watched_threshold_met" and item.get("lock_marker_verified") is True
                            or item.get("status") == "restricted_marker"
                            and item.get("marker") == "90_percent_non_draggable"):
                        classifications[index] = "unskippable"

        observed_courseware = courseware_records
        if observed_courseware is None and courseware_total is not None:
            for report in reversed(reports):
                if str(report.get("section_id")) != section_id:
                    continue
                candidates = [item for item in report.get("resources", []) if item.get("kind") == "courseware"]
                if len(candidates) == courseware_total:
                    observed_courseware = candidates
                    break
        video_completed = len(video_records) if video_records is not None else sum(
            record.get("status") == "already_completed"
            or (record.get("status") in {"seeked_to_end", "watched_threshold_met"}
                and record.get("task_point_completed") is True)
            for record in (classification_records or [])
        )
        courseware_completed = len(courseware_records) if courseware_records is not None else sum(
            record.get("status") == "scrolled_to_end" for record in (observed_courseware or [])
        )

        archive = archived.get(section_id, {})
        questions = archive.get("questions", [])
        quiz_verified = (
            archive.get("score") == 100
            and bool(questions)
            and all(question.get("correct_answer") for question in questions)
        )
        question_total = len(questions) if quiz_verified else section.get("question_total")
        quiz_state = "fully_correct" if quiz_verified else "none" if question_total == 0 else "unverified"
        video_unknown = classifications.count("unknown")
        resources_complete = video_records is not None and courseware_records is not None and video_unknown == 0
        rows.append({
            "section_id": section_id,
            "order": section.get("order"),
            "section_path": section.get("section_path"),
            "video_total": video_total,
            "video_skippable": classifications.count("skippable"),
            "video_unskippable": classifications.count("unskippable"),
            "video_unknown": video_unknown,
            "video_completed": video_completed,
            "courseware_total": courseware_total,
            "courseware_completed": courseware_completed,
            "question_total": question_total,
            "quiz_state": quiz_state,
            "status": "complete" if resources_complete and quiz_state in {"fully_correct", "none"} else "partial",
        })
    return {
        "course_id": str(inventory.get("course", {}).get("course_id") or ""),
        "section_count": len(rows),
        "complete_count": sum(row["status"] == "complete" for row in rows),
        "sections": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总课程各节的视频、课件及已核验测验答案")
    default_inventory = ROOT / "output" / "lesson-inventory" / "lesson-inventory.json"
    default_one_shot = ROOT / "output" / "complete-course" / "complete-course.json"
    parser.add_argument("--inventory", type=Path, default=default_inventory)
    parser.add_argument("--reports-dir", type=Path, default=ROOT / "output" / "lesson-completion")
    parser.add_argument("--one-shot-report", type=Path)
    parser.add_argument("--answers", type=Path, default=ROOT / "output" / "quiz-completion" / "verified-answers.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "course-summary")
    args = parser.parse_args(argv)

    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    answers = json.loads(args.answers.read_text(encoding="utf-8"))
    course_id = str(inventory.get("course", {}).get("course_id") or "")
    if str(answers.get("course_id") or "") != course_id:
        raise ValueError("inventory and verified-answer archive belong to different courses")

    report_files = sorted(args.reports_dir.rglob("lesson-completion*.json"))
    loaded_reports = [json.loads(path.read_text(encoding="utf-8")) for path in report_files]
    loaded_reports.sort(key=lambda report: report.get("captured_at") or "")
    reports = []
    for report in loaded_reports:
        reports.extend(report.get("sections", []))
    one_shot_path = args.one_shot_report
    if one_shot_path is None and args.inventory.resolve() == default_inventory.resolve():
        one_shot_path = default_one_shot
    if one_shot_path is not None and one_shot_path.exists():
        one_shot = json.loads(one_shot_path.read_text(encoding="utf-8"))
        if str(one_shot.get("course_id") or "") != course_id:
            raise ValueError("one-shot report belongs to a different course")
        for section in one_shot.get("sections", []):
            resources = section.get("resources")
            if isinstance(resources, dict) and isinstance(resources.get("resources"), list):
                reports.append(resources)
    summary = summarize_course(inventory, reports, answers)
    summary["generated_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    summary["resource_report_files"] = len(report_files)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "course-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (args.output_dir / "course-summary.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(summary["sections"][0]) if summary["sections"] else [])
        writer.writeheader()
        writer.writerows(summary["sections"])
    print(f"{summary['complete_count']}/{summary['section_count']} sections verified complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
