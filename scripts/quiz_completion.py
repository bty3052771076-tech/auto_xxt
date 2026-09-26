#!/usr/bin/env python3
"""Complete permitted Chaoxing choice quizzes and archive page-verified answers.

Only a displayed 100 score with a complete answer key enters verified-answers.json.
Pending human review and incomplete answer keys are never called complete.
"""

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

from playwright.sync_api import BrowserContext, Frame, Page, sync_playwright

try:
    from . import lesson_completion, lesson_inventory
except ImportError:
    import lesson_completion  # type: ignore[no-redef]
    import lesson_inventory  # type: ignore[no-redef]


DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "output" / "quiz-completion"
ANSWER_MARKER = re.compile(r"正确答案\s*[：:]\s*(.*?)(?=我的答案\s*[：:]|得分\s*[：:]|$)", re.S)
SUBMITTED_MARKER = re.compile(r"我的答案\s*[：:]\s*(.*?)(?=得分\s*[：:]|正确答案\s*[：:]|$)", re.S)
SCORE_MARKER = re.compile(r"本次成绩\s*[：:]\s*(\d+(?:\.\d+)?)")
QUESTION_SCORE_MARKER = re.compile(r"(?:本题)?得分\s*[：:]\s*(\d+(?:\.\d+)?)\s*分?")
RETRY_MARKER = re.compile(r"还可以重做\s*(\d+)\s*次")
PRIVATE_USE_MARKER = re.compile(r"[\ue000-\uf8ff]")


def classify_quiz_state(header_text: str) -> str:
    """Classify only from a quiz's status header, never from an answer alone."""
    if "待批阅" in header_text:
        return "pending_review"
    if "待完成" in header_text:
        return "not_started"
    match = SCORE_MARKER.search(header_text)
    if match:
        return "fully_correct" if float(match.group(1)) == 100 else "reviewed_incorrect"
    return "unknown"


def extract_correct_answer(answer_text: str) -> str | None:
    return extract_answer_with_marker(answer_text, ANSWER_MARKER)


def extract_submitted_answer(answer_text: str) -> str | None:
    return extract_answer_with_marker(answer_text, SUBMITTED_MARKER)


def extract_question_score(answer_text: str) -> float | None:
    match = QUESTION_SCORE_MARKER.search(answer_text)
    return float(match.group(1)) if match else None


def make_review_observation(snapshot: dict[str, Any], origin: str) -> dict[str, Any]:
    return {
        "origin": origin,
        "state": snapshot["state"],
        "score": snapshot.get("score"),
        "questions": [
            {key: question.get(key) for key in ("id", "submitted_answer", "correct_answer", "awarded_score")}
            for question in snapshot["questions"]
        ],
    }


def extract_answer_with_marker(answer_text: str, marker: re.Pattern[str]) -> str | None:
    match = marker.search(answer_text)
    if not match:
        return None
    content = match.group(1).strip()
    first_line = content.splitlines()[0].strip() if content else ""
    choice = re.match(r"^([A-Z]+)(?=\s|:|$)", first_line)
    if choice:
        return choice.group(1)
    if first_line in {"对", "错"}:
        return first_line
    return content or None


def answer_text_from_parts(answer_box: str, full_question: str) -> str:
    return answer_box if "正确答案" in answer_box and "我的答案" in answer_box else full_question


def answer_for_input(answer: str, input_type: str, values: list[str]) -> list[str] | None:
    if answer in {"对", "错"}:
        selected = "true" if answer == "对" else "false"
        return [selected] if input_type == "radio" and selected in values else None
    if not re.fullmatch(r"[A-Z]+", answer):
        return None
    selected = list(answer)
    if len(set(selected)) != len(selected) or any(value not in values for value in selected):
        return None
    if input_type == "radio" and len(selected) == 1:
        return selected
    if input_type == "checkbox":
        return selected
    return None


def retry_key_is_complete(questions: list[dict[str, Any]], key: dict[str, str], remaining: int) -> bool:
    if remaining < 1 or not questions or len({str(q.get("id")) for q in questions}) != len(questions):
        return False
    return all(
        q.get("id") in key
        and answer_for_input(key[q["id"]], q.get("input_type", ""), q.get("input_values", [])) is not None
        for q in questions
    )


def review_key_is_complete(questions: list[dict[str, Any]], key: dict[str, str], remaining: int) -> bool:
    if remaining < 1 or not questions or len({str(q.get("id")) for q in questions}) != len(questions):
        return False
    return all(q.get("id") in key and bool(key[q["id"]].strip()) for q in questions)


def merge_page_and_manual_key(
    reviewed_questions: list[dict[str, Any]], manual_key: dict[str, str] | None
) -> dict[str, str] | None:
    merged = dict(manual_key or {})
    for question in reviewed_questions:
        page_answer = question.get("correct_answer")
        if not page_answer:
            continue
        question_id = question["id"]
        if question_id in merged and merged[question_id] != page_answer:
            return None
        merged[question_id] = page_answer
    return merged


def answers_equivalent(left: str, right: str) -> bool:
    if re.fullmatch(r"[A-Z]+", left) and re.fullmatch(r"[A-Z]+", right):
        return sorted(left) == sorted(right)
    return left == right


def make_verified_record(
    section_id: str, state: str, score: float | None, questions: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if state != "fully_correct" or score != 100 or not questions:
        return None
    if len({q["id"] for q in questions}) != len(questions):
        return None
    archived_questions = []
    for question in questions:
        if not question.get("id"):
            return None
        page_answer = question.get("correct_answer")
        submitted_answer = question.get("submitted_answer")
        if page_answer and submitted_answer and not answers_equivalent(page_answer, submitted_answer):
            return None
        final_answer = page_answer or submitted_answer
        if not final_answer:
            return None
        copy = dict(question)
        copy["correct_answer"] = final_answer
        copy["answer_source"] = "page_correct_answer" if page_answer else "score_100_user_answer"
        archived_questions.append(copy)
    return {
        "section_id": section_id,
        "score": 100,
        "verification": "quiz_page_displays_this_attempt_score_100_and_every_question_has_page_key_or_submitted_answer",
        "questions": archived_questions,
    }


def quiz_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        section for section in sections
        if section.get("quiz_count", 0) > 0
        or any(item.get("kind") == "quiz" for item in section.get("evidence", []))
    ]


def is_work_frame_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.startswith("/mooc-ans/work/") and path.rsplit("/", 1)[-1] in {
        "dohomeworknew", "selectworkquestionyipiyue"
    }


def current_quiz_frame(page: Page, timeout_seconds: int = 15) -> Frame | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        frames = [frame for frame in page.frames if is_work_frame_url(frame.url)]
        if len(frames) == 1:
            return frames[0]
        page.wait_for_timeout(350)
    return None


def read_quiz(frame: Frame) -> dict[str, Any]:
    body_text = frame.locator("body").inner_text(timeout=5000)
    header = body_text[:500]
    state = classify_quiz_state(header)
    score_match = SCORE_MARKER.search(header)
    score = float(score_match.group(1)) if score_match else None
    retry_match = RETRY_MARKER.search(header)
    remaining = int(retry_match.group(1)) if retry_match else 0
    raw_questions = frame.locator(".TiMu.singleQuesId").evaluate_all(
        """nodes => nodes.map(node => {
          const title = node.querySelector('.Zy_TItle');
          const answer = node.querySelector('.Py_answer');
          const inputs = [...node.querySelectorAll('input[type="radio"],input[type="checkbox"]')];
          const kinds = [...new Set(inputs.map(el => el.type))];
          const options = [...node.querySelectorAll('.Zy_ulTop li')].map(el => {
            const label = el.querySelector('input')?.value || el.querySelector('i')?.innerText || '';
            return {label: label.trim().replace(/[^A-Z]/g, ''), text: (el.innerText || '').trim()};
          });
          return {
            id: node.getAttribute('data') || '',
            prompt: (title?.innerText || '').trim(),
            answer_text: (answer?.innerText || '').trim(),
            full_text: (node.innerText || '').trim(),
            input_type: kinds.length === 1 ? kinds[0] : '',
            input_values: inputs.map(el => el.value),
            options
          };
        })"""
    )
    questions: list[dict[str, Any]] = []
    for question in raw_questions:
        prompt = re.sub(r"^\s*\d+\s*", "", question["prompt"]).strip()
        type_match = re.search(r"【([^】]+)】", prompt)
        questions.append({
            "id": str(question["id"]),
            "type": type_match.group(1) if type_match else "unknown",
            "prompt": prompt,
            "prompt_contains_obfuscated_glyphs": bool(PRIVATE_USE_MARKER.search(prompt)),
            "options": question["options"],
            "correct_answer": extract_correct_answer(
                answer_text_from_parts(question["answer_text"], question["full_text"])
            ),
            "submitted_answer": extract_submitted_answer(
                answer_text_from_parts(question["answer_text"], question["full_text"])
            ),
            "awarded_score": extract_question_score(question["answer_text"] or question["full_text"]),
            "input_type": question["input_type"],
            "input_values": question["input_values"],
        })
    return {
        "state": state,
        "score": score,
        "remaining_redos": remaining,
        "questions": questions,
        "work_id": frame.locator("#workId").input_value() if frame.locator("#workId").count() else None,
        "frame_path": urlsplit(frame.url).path,
        "header_excerpt": header.splitlines()[0][:100] if header else "",
    }


def editable_inputs(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [q for q in snapshot["questions"] if q["input_type"] in {"radio", "checkbox"}]


def choose_answers(frame: Frame, questions: list[dict[str, Any]], key: dict[str, str] | None) -> None:
    for question in questions:
        values = question["input_values"]
        selected = answer_for_input(key[question["id"]], question["input_type"], values) if key else values[:1]
        if not selected:
            raise RuntimeError(f"question {question['id']} has no supported answer selection")
        for value in values if question["input_type"] == "checkbox" else selected:
            locator = frame.locator(
                f'.TiMu[data="{question["id"]}"] '
                f'input[type="{question["input_type"]}"][value="{value}"]'
            )
            if value in selected:
                locator.check(timeout=8000)
            else:
                locator.uncheck(timeout=8000)


def selection_is_synced(checked_values: list[str], hidden_value: str | None, expected_values: list[str]) -> bool:
    if sorted(checked_values) != sorted(expected_values):
        return False
    if hidden_value is not None:
        if len(expected_values) == 1 and hidden_value == expected_values[0]:
            return True
        if not all(len(value) == 1 for value in expected_values) or sorted(hidden_value) != sorted(expected_values):
            return False
    return True


def wait_for_selected_answers(
    frame: Frame, questions: list[dict[str, Any]], key: dict[str, str] | None, timeout_seconds: int = 8
) -> None:
    expected = {
        question["id"]: answer_for_input(key[question["id"]], question["input_type"], question["input_values"])
        if key else question["input_values"][:1]
        for question in questions
    }
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        all_synced = True
        for question in questions:
            state = frame.locator(f'.TiMu[data="{question["id"]}"]').evaluate(
                """node => {
                  const id = node.getAttribute('data');
                  const checked = [...node.querySelectorAll('input[type="radio"],input[type="checkbox"]')]
                    .filter(input => input.checked).map(input => input.value);
                  const hidden = node.querySelector('input[type="hidden"][id="answer' + id + '"]');
                  return {checked, hidden: hidden ? hidden.value : null};
                }"""
            )
            if not selection_is_synced(state["checked"], state["hidden"], expected[question["id"]] or []):
                all_synced = False
                break
        if all_synced:
            return
        frame.page.wait_for_timeout(160)
    raise RuntimeError("selected answers did not synchronize with the quiz form; submission stopped")


def fill_answers(frame: Frame, questions: list[dict[str, Any]], key: dict[str, str] | None) -> None:
    choose_answers(frame, questions, key)
    wait_for_selected_answers(frame, questions, key)


def submit_quiz(page: Page, frame: Frame) -> Frame:
    frame.locator('a[onclick*="btnBlueSubmit"]').click(timeout=8000)
    confirm = frame.locator('a[onclick*="submitCheckTimes"]')
    confirm.wait_for(state="visible", timeout=5000)
    confirm.click(timeout=8000)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        result_frame = current_quiz_frame(page, timeout_seconds=1)
        if result_frame is not None and classify_quiz_state(result_frame.locator("body").inner_text(timeout=3000)[:500]) in {
            "fully_correct", "reviewed_incorrect", "pending_review"
        }:
            return result_frame
        page.wait_for_timeout(500)
    raise RuntimeError("submission did not show a graded/reviewed quiz page")


def redo_quiz(page: Page, frame: Frame) -> Frame:
    frame.locator('span[onclick*="redoWin"]', has_text="重做").click(timeout=8000)
    frame.locator('#redoWin a[onclick*="retest()"]:visible').click(timeout=8000)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result_frame = current_quiz_frame(page, timeout_seconds=1)
        if result_frame is not None and classify_quiz_state(result_frame.locator("body").inner_text(timeout=3000)[:500]) == "not_started":
            return result_frame
        page.wait_for_timeout(500)
    raise RuntimeError("redo did not open editable quiz")


def process_section(
    page: Page, section: dict[str, Any], complete: bool, manual_key: dict[str, str] | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    section_id = str(section["section_id"])
    frame = current_quiz_frame(page)
    if frame is None:
        raise RuntimeError("quiz work frame did not load")
    snapshot = read_quiz(frame)
    submissions = 0
    observations: list[dict[str, Any]] = []
    if snapshot["state"] in {"fully_correct", "reviewed_incorrect", "pending_review"}:
        observations.append(make_review_observation(snapshot, "existing_review"))
    if complete and snapshot["state"] == "not_started":
        questions = editable_inputs(snapshot)
        limit = int(frame.locator("#limitWorkSubmitTimes").input_value() or "0")
        used = int(frame.locator("#addTimes").input_value() or "0")
        first_key = manual_key if retry_key_is_complete(questions, manual_key or {}, 1) else None
        needed_submissions = 1 if first_key else 2
        if len(questions) != len(snapshot["questions"]) or limit - used < needed_submissions:
            raise RuntimeError("quiz has unsupported questions or fewer than two permitted submissions")
        # First attempt uses the first visible option; the platform's graded
        # answer key is required before any automatic retry.
        fill_answers(frame, questions, key=first_key)
        frame = submit_quiz(page, frame)
        submissions += 1
        snapshot = read_quiz(frame)
        observations.append(make_review_observation(snapshot, "submission"))

    if complete and snapshot["state"] == "reviewed_incorrect":
        key = merge_page_and_manual_key(snapshot["questions"], manual_key)
        if key is None:
            raise RuntimeError("provided answer key conflicts with the quiz page's correct answers")
        if review_key_is_complete(snapshot["questions"], key, snapshot["remaining_redos"]):
            frame = redo_quiz(page, frame)
            editable = read_quiz(frame)
            questions = editable_inputs(editable)
            if retry_key_is_complete(questions, key, 1) and len(questions) == len(editable["questions"]):
                fill_answers(frame, questions, key)
                frame = submit_quiz(page, frame)
                submissions += 1
                snapshot = read_quiz(frame)
                observations.append(make_review_observation(snapshot, "submission"))
            else:
                raise RuntimeError("question IDs/options changed on redo; stopped without second submission")

    verified = make_verified_record(section_id, snapshot["state"], snapshot["score"], snapshot["questions"])
    if verified:
        verified.update({
            "section_path": section.get("section_path"),
            "title": section.get("title"),
            "work_id": snapshot["work_id"],
            "verified_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "source_frame_path": snapshot["frame_path"],
        })
        for question in verified["questions"]:
            question.pop("input_type", None)
            question.pop("input_values", None)
    progress = {
        "section_id": section_id,
        "section_path": section.get("section_path"),
        "title": section.get("title"),
        "state": snapshot["state"],
        "score": snapshot["score"],
        "question_count": len(snapshot["questions"]),
        "questions_with_visible_key": sum(q["correct_answer"] is not None for q in snapshot["questions"]),
        "submissions_this_run": submissions,
        "review_observations": observations,
        "verified_archive": verified is not None,
        "checked_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    return progress, verified


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def merge_progress_records(prior: list[dict[str, Any]], updated: dict[str, Any]) -> list[dict[str, Any]]:
    merged = {str(item["section_id"]): item for item in prior}
    merged[str(updated["section_id"])] = updated
    return list(merged.values())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="核验并归档课程测验答案；可选完成待作答的选择/判断题")
    parser.add_argument("--inventory", type=Path, default=lesson_completion.DEFAULT_INVENTORY)
    parser.add_argument("--url", help="已登录课程中的任意小节 URL")
    parser.add_argument("--profile-dir", type=Path, default=lesson_completion.DEFAULT_PROFILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--section-id", help="只处理指定小节")
    parser.add_argument("--complete", action="store_true", help="允许填写、提交、按批阅答案重做；默认只读归档")
    parser.add_argument("--answer-key", type=Path, help="可选的人工核对答案 JSON；页面答案优先，冲突即停止")
    parser.add_argument("--assume-authenticated", action="store_true")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    data = lesson_completion.load_inventory(args.inventory)
    entry_url = args.url or data["course"]["entry_url"]
    course_id = str(data["course"].get("course_id") or "")
    sections = quiz_sections(sorted(data["sections"], key=lambda item: item.get("order", 0)))
    if args.section_id:
        sections = [s for s in sections if str(s["section_id"]) == str(args.section_id)]
        if not sections:
            raise ValueError(f"unknown or quiz-free section ID {args.section_id}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    progress_file = args.output_dir / "quiz-progress.json"
    answers_file = args.output_dir / "verified-answers.json"
    previous = json.loads(answers_file.read_text(encoding="utf-8")) if answers_file.exists() else {}
    verified_sections = previous.get("sections", {})
    prior_progress = json.loads(progress_file.read_text(encoding="utf-8")) if progress_file.exists() else {}
    if prior_progress and str(prior_progress.get("course_id")) != course_id:
        raise ValueError("existing quiz progress belongs to a different course")
    manual_keys: dict[str, dict[str, str]] = {}
    if args.answer_key:
        supplied = json.loads(args.answer_key.read_text(encoding="utf-8"))
        if str(supplied.get("course_id")) != course_id:
            raise ValueError("answer-key course ID differs from inventory")
        manual_keys = supplied.get("sections", {})
    progress: list[dict[str, Any]] = prior_progress.get("sections", [])

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
                print("请在 Chrome 窗口手动登录课程；脚本不读取密码。", flush=True)
                lesson_inventory.wait_for_manual_login(page, 600, context, "科技文献检索与利用", course_id or None)
            actual_id = lesson_inventory.query_value(page.url, ("courseId", "courseid"))
            if course_id and actual_id and actual_id != course_id:
                raise RuntimeError(f"opened course {actual_id}, expected {course_id}")

            for index, section in enumerate(sections, 1):
                section_id = str(section["section_id"])
                target_url = lesson_completion.section_url_for_id(entry_url, section_id)
                try:
                    response = page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                    if response is not None and response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}")
                    if lesson_inventory.is_login_page(page):
                        raise RuntimeError("login expired")
                    record, verified = process_section(
                        page, section, complete=args.complete, manual_key=manual_keys.get(section_id)
                    )
                    if verified:
                        verified_sections[section_id] = verified
                except Exception as exc:
                    record = {
                        "section_id": section_id,
                        "section_path": section.get("section_path"),
                        "title": section.get("title"),
                        "state": "error",
                        "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                    }
                progress = merge_progress_records(progress, record)
                write_json(progress_file, {"course_id": course_id, "sections": progress})
                write_json(answers_file, {
                    "course_id": course_id,
                    "verification_rule": "100 score on reviewed page and an answer for every rendered question",
                    "sections": verified_sections,
                })
                print(f"[{index}/{len(sections)}] {section.get('section_path')}: {record['state']}, "
                      f"{record.get('question_count', '?')} 题, 新提交 {record.get('submissions_this_run', 0)} 次, "
                      f"归档={record.get('verified_archive', False)}", flush=True)

            print(f"进度：{progress_file.resolve()}\n已核对答案：{answers_file.resolve()}", flush=True)
            print("Chrome 保持开启供核查；关闭窗口后脚本退出。", flush=True)
            while any(not open_page.is_closed() for open_page in context.pages):
                time.sleep(1)
            return 0 if all(item["state"] != "error" for item in progress) else 2
        except Exception as exc:
            print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            print("Chrome 保持开启供核查；关闭窗口后脚本退出。", flush=True)
            while any(not open_page.is_closed() for open_page in context.pages):
                time.sleep(1)
            return 2


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
