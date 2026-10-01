from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")
REPOSITORY = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
LEADERBOARD_ISSUE = int(os.environ.get("LEADERBOARD_ISSUE", "2"))

PR_COMMENT_MARKER = "<!-- wind-grader-comment -->"
SUBMISSION_MARKER_RE = re.compile(
    r"<!-- wind-forecast-submission:([A-Za-z0-9_-]+={0,2}) -->"
)


def api_request(method: str, path: str, payload: dict | None = None):
    url = f"{API_URL}{path}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "wind-power-forecasting-grader",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API 요청 실패: {method} {path} -> HTTP {exc.code}: {body}"
        ) from exc


def get_all_issue_comments(issue_number: int) -> list[dict]:
    comments: list[dict] = []
    page = 1

    while True:
        batch = api_request(
            "GET",
            f"/repos/{REPOSITORY}/issues/{issue_number}/comments"
            f"?per_page=100&page={page}",
        )
        if not batch:
            break
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    return comments


def update_latest_pr_comment(pr_number: int, body: str) -> None:
    comments = get_all_issue_comments(pr_number)

    existing = [
        comment
        for comment in comments
        if PR_COMMENT_MARKER in (comment.get("body") or "")
    ]

    if existing:
        comment_id = existing[-1]["id"]
        api_request(
            "PATCH",
            f"/repos/{REPOSITORY}/issues/comments/{comment_id}",
            {"body": body},
        )
    else:
        api_request(
            "POST",
            f"/repos/{REPOSITORY}/issues/{pr_number}/comments",
            {"body": body},
        )


def encode_submission_marker(record: dict) -> str:
    raw = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii")
    return f"<!-- wind-forecast-submission:{encoded} -->"


def decode_submission_marker(body: str) -> dict | None:
    match = SUBMISSION_MARKER_RE.search(body or "")
    if not match:
        return None

    try:
        raw = base64.urlsafe_b64decode(match.group(1).encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(data, dict):
        return None
    return data


def escape_table_cell(value: object) -> str:
    text = html.escape(str(value))
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def submission_comment(record: dict) -> str:
    marker = encode_submission_marker(record)

    return f"""{marker}
### 채점 완료 ✅

| 제출시각 | 이름 | 모델명 | NMAE |
| --- | --- | --- | ---: |
| {escape_table_cell(record["graded_at"])} | {escape_table_cell(record["name"])} | {escape_table_cell(record["model_name"])} | **{record["nmae"]:.4f}%** |
"""


def upsert_submission_comment(record: dict) -> list[dict]:
    comments = get_all_issue_comments(LEADERBOARD_ISSUE)
    parsed: list[tuple[dict, dict]] = []

    for comment in comments:
        submission = decode_submission_marker(comment.get("body") or "")
        if submission is not None:
            parsed.append((comment, submission))

    target = None
    for comment, submission in parsed:
        if (
            submission.get("pr_number") == record["pr_number"]
            and submission.get("head_sha") == record["head_sha"]
        ):
            target = comment
            break

    body = submission_comment(record)

    if target is None:
        api_request(
            "POST",
            f"/repos/{REPOSITORY}/issues/{LEADERBOARD_ISSUE}/comments",
            {"body": body},
        )
        records = [submission for _, submission in parsed]
        records.append(record)
    else:
        api_request(
            "PATCH",
            f"/repos/{REPOSITORY}/issues/comments/{target['id']}",
            {"body": body},
        )
        records = []
        for comment, submission in parsed:
            if comment["id"] == target["id"]:
                records.append(record)
            else:
                records.append(submission)

    return records


def render_leaderboard(records: list[dict]) -> str:
    valid_records = [
        record
        for record in records
        if isinstance(record.get("nmae"), (int, float))
        and record.get("github_user")
        and record.get("graded_at")
    ]

    best_by_user: dict[str, dict] = {}
    for record in valid_records:
        user = record["github_user"]
        current = best_by_user.get(user)
        if current is None or record["nmae"] < current["nmae"]:
            best_by_user[user] = record

    best = sorted(
        best_by_user.values(),
        key=lambda item: (item["nmae"], item["github_user"].lower()),
    )
    history = sorted(
        valid_records,
        key=lambda item: item["graded_at"],
        reverse=True,
    )

    lines = [
        "# Wind Power Forecasting Leaderboard",
        "",
        "자동 채점이 완료된 제출 결과입니다. **NMAE는 낮을수록 좋습니다.**",
        "",
        "> 제출 파일의 직접 접근을 줄이기 위해 Leaderboard에는 Pull Request 링크를 표시하지 않습니다.",
        "",
        "## Best Leaderboard",
        "",
    ]

    if best:
        lines.extend(
            [
                "| 순위 | 이름 | 모델명 | NMAE |",
                "| ---: | --- | --- | ---: |",
            ]
        )
        for rank, record in enumerate(best, start=1):
            lines.append(
                f"| {rank} | {escape_table_cell(record['name'])} | "
                f"{escape_table_cell(record['model_name'])} | "
                f"**{record['nmae']:.4f}%** |"
            )
    else:
        lines.append("아직 채점된 제출이 없습니다.")

    lines.extend(["", "## Submission History", ""])

    if history:
        lines.extend(
            [
                "| 제출시각 | 이름 | 모델명 | NMAE |",
                "| --- | --- | --- | ---: |",
            ]
        )

        rendered_history: list[str] = []
        for record in history:
            rendered_history.append(
                f"| {escape_table_cell(record['graded_at'])} | "
                f"{escape_table_cell(record['name'])} | "
                f"{escape_table_cell(record['model_name'])} | "
                f"{record['nmae']:.4f}% |"
            )

        for row in rendered_history:
            tentative = "\n".join(
                lines
                + [
                    row,
                    "",
                    "> 전체 제출 이력은 이 이슈의 자동 생성 댓글에도 보존됩니다.",
                ]
            )
            if len(tentative.encode("utf-8")) > 58_000:
                lines.append("")
                lines.append(
                    "> 표의 길이가 커져 최신 제출 이력만 표시합니다. "
                    "전체 이력은 아래 자동 채점 댓글에 보존됩니다."
                )
                break
            lines.append(row)
    else:
        lines.append("아직 채점된 제출이 없습니다.")

    lines.extend(["", "> 학생 제출 Pull Request는 Merge하지 않습니다."])
    return "\n".join(lines)


def update_leaderboard_issue(records: list[dict]) -> None:
    body = render_leaderboard(records)
    api_request(
        "PATCH",
        f"/repos/{REPOSITORY}/issues/{LEADERBOARD_ISSUE}",
        {"body": body},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--comment", required=True, type=Path)
    args = parser.parse_args()

    result = json.loads(args.result.read_text(encoding="utf-8"))
    comment_body = args.comment.read_text(encoding="utf-8")

    pr_number = int(result["pr_number"])
    if pr_number <= 0:
        raise RuntimeError("PR 번호를 확인할 수 없습니다.")

    update_latest_pr_comment(pr_number, comment_body)

    if result.get("valid") is True:
        records = upsert_submission_comment(result)
        update_leaderboard_issue(records)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"결과 게시 실패: {exc}", file=sys.stderr)
        raise
