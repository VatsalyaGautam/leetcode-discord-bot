#!/usr/bin/env python3
"""Daily LeetCode report for Discord via webhook."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    raise SystemExit("Python 3.9+ is required for timezone support.")

LEETCODE_GRAPHQL = "https://leetcode.com/graphql"
WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"
USERS_FILE_DEFAULT = "users.json"
TIMEZONE_ENV = "REPORT_TIMEZONE"
DEFAULT_TIMEZONE = "Asia/Kolkata"
REQUEST_DELAY_SECONDS = 0.3
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0
SUBMISSIONS_LIMIT = 20

GRAPHQL_QUERY = """query recentAcSubmissionList($username: String!, $limit: Int!) {
  recentAcSubmissionList(username: $username, limit: $limit) {
    title
    titleSlug
    timestamp
    lang
  }
}
"""


def normalize_username(value: str) -> str:
    """Accept a bare username or a LeetCode profile URL and return the username.

    Handles forms like:
      VatsalyaGautam
      https://leetcode.com/u/VatsalyaGautam/
      https://leetcode.com/VatsalyaGautam
      leetcode.com/u/VatsalyaGautam/?tab=...
    """
    value = value.strip()
    if "leetcode.com" not in value:
        return value.strip("/")

    # Drop scheme, query/fragment, then take the path after the domain.
    path = value.split("leetcode.com", 1)[1]
    path = path.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] == "u":  # /u/<username>/ profile form
        parts = parts[1:]
    return parts[0] if parts else ""


class LeetCodeError(Exception):
    """Raised when a single user's data cannot be fetched.

    Unlike SystemExit, this is recoverable: the run continues and the user is
    rendered with an error marker instead of aborting the whole report.
    """


@dataclass
class UserResult:
    display_name: str
    username: str
    solves: list[dict[str, str]] = field(default_factory=list)  # each: {"title", "slug"}
    error: str | None = None

    @property
    def count(self) -> int:
        return len(self.solves)


def load_users(path: str) -> dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise SystemExit(f"users file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"users file is not valid JSON: {exc}")

    if not isinstance(data, dict):
        raise SystemExit("users file must contain a JSON object of displayName->leetcodeUsername.")

    cleaned: dict[str, str] = {}
    for display_name, username in data.items():
        if not isinstance(display_name, str) or not isinstance(username, str):
            raise SystemExit("users.json must map string keys to string values.")
        username = normalize_username(username)
        if not username:
            raise SystemExit(f"leetcode username for '{display_name}' is empty")
        cleaned[display_name.strip()] = username

    if not cleaned:
        raise SystemExit("users.json must contain at least one user.")

    return cleaned


def _http_post(url: str, payload: bytes, headers: dict[str, str]) -> tuple[int, str]:
    """POST with bounded retries on transient (network / 5xx / 429) failures.

    Returns (status_code, body). Raises LeetCodeError if all attempts fail.
    """
    last_error = "unknown error"

    for attempt in range(1, MAX_RETRIES + 1):
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} {exc.reason}"
            # 4xx (except 429) are not worth retrying — the request itself is bad.
            if exc.code < 500 and exc.code != 429:
                raise LeetCodeError(last_error) from exc
        except urllib.error.URLError as exc:
            last_error = f"network error: {exc.reason}"
        except TimeoutError:
            last_error = "request timed out"

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    raise LeetCodeError(f"request failed after {MAX_RETRIES} attempts: {last_error}")


def graphql_post(query: str, variables: dict[str, object]) -> dict[str, object]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; LeetCodeReport/1.0)",
        "Referer": "https://leetcode.com",
    }

    _, body = _http_post(LEETCODE_GRAPHQL, payload, headers)

    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LeetCodeError(f"LeetCode API returned invalid JSON: {exc}") from exc

    if document.get("errors"):
        raise LeetCodeError(f"LeetCode API returned errors: {document['errors']}")

    return document.get("data", {})


def fetch_recent_accepts(username: str) -> list[dict[str, object]]:
    data = graphql_post(GRAPHQL_QUERY, {"username": username, "limit": SUBMISSIONS_LIMIT})
    submissions = data.get("recentAcSubmissionList")
    if submissions is None:
        raise LeetCodeError(f"missing recentAcSubmissionList (does user '{username}' exist?)")
    if not isinstance(submissions, list):
        raise LeetCodeError("recentAcSubmissionList is not a list")
    return submissions


def get_today_local(tz: ZoneInfo) -> datetime:
    return datetime.now(timezone.utc).astimezone(tz)


def solves_today(submissions: list[dict[str, object]], tz: ZoneInfo) -> list[dict[str, str]]:
    """Return the distinct problems a user accepted today, in solve order.

    Deduped by problem slug, so re-submitting the same problem counts once.
    """
    today = get_today_local(tz).date()
    seen: set[str] = set()
    solved: list[dict[str, str]] = []

    for item in submissions:
        timestamp = item.get("timestamp")
        if timestamp is None:
            continue
        try:
            timestamp = int(timestamp)
        except (TypeError, ValueError):
            continue

        solved_at = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(tz)
        if solved_at.date() != today:
            continue

        slug = str(item.get("titleSlug") or "")
        title = str(item.get("title") or slug or "Unknown problem")
        key = slug or title
        if key in seen:
            continue
        seen.add(key)
        solved.append({"title": title, "slug": slug})

    return solved


def count_today_solves(submissions: list[dict[str, object]], tz: ZoneInfo) -> int:
    return len(solves_today(submissions, tz))


def collect_user_results(users: dict[str, str], tz: ZoneInfo) -> list[UserResult]:
    """Fetch every user independently. A failure for one user never aborts the run."""
    results: list[UserResult] = []

    for display_name, username in users.items():
        try:
            submissions = fetch_recent_accepts(username)
            solved = solves_today(submissions, tz)
            results.append(UserResult(display_name, username, solves=solved))
        except LeetCodeError as exc:
            print(f"warning: failed to fetch '{username}' ({display_name}): {exc}", file=sys.stderr)
            results.append(UserResult(display_name, username, error=str(exc)))
        time.sleep(REQUEST_DELAY_SECONDS)

    return results


PROBLEM_URL = "https://leetcode.com/problems/{slug}/"
ZWSP = "​"  # zero-width space: lets us add visual breathing room in Discord


def _problem_line(problem: dict[str, str]) -> str:
    title = problem.get("title") or "Unknown problem"
    slug = problem.get("slug")
    if slug:
        return f"`›` [{title}]({PROBLEM_URL.format(slug=slug)})"
    return f"`›` {title}"


def _user_field(result: UserResult) -> dict[str, object]:
    """One spacious, non-inline field per user — name is the headline, value lists problems."""
    if result.error is not None:
        return {
            "name": f"⚠️  {result.display_name}",
            "value": "_Couldn't fetch data right now._\n" + ZWSP,
            "inline": False,
        }

    if result.count == 0:
        return {
            "name": f"😴  {result.display_name}  ·  0 solved",
            "value": "_No solves yet today._\n" + ZWSP,
            "inline": False,
        }

    problems = "\n".join(_problem_line(p) for p in result.solves)
    return {
        "name": f"✅  {result.display_name}  ·  {result.count} solved",
        "value": problems + "\n" + ZWSP,
        "inline": False,
    }


def build_report(user_results: list[UserResult], report_date: datetime) -> tuple[str, list[dict[str, object]]]:
    total = sum(r.count for r in user_results if r.error is None)
    succeeded = sum(1 for r in user_results if r.error is None)
    errored = len(user_results) - succeeded

    summary = f"**{total}** solve{'' if total == 1 else 's'} across **{succeeded}** member{'' if succeeded == 1 else 's'} today."
    if errored:
        summary += f"\n_{errored} member(s) couldn't be fetched._"

    embed = {
        "description": f"{summary}\n{ZWSP}",
        "color": 0x00A0FF,
        "fields": [_user_field(r) for r in user_results],
        "timestamp": report_date.astimezone(timezone.utc).isoformat(),
        "footer": {"text": "Accepted solves today · LeetCode"},
    }
    # The plain-text content is the single header line; the embed carries the detail.
    content = f"**Daily LeetCode Report — {report_date.strftime('%Y-%m-%d')}**"
    return content, [embed]


def send_discord_webhook(webhook_url: str, content: str, embeds: list[dict[str, object]]) -> None:
    payload = json.dumps({"content": content, "embeds": embeds}).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "DiscordBot (https://github.com)"}

    # _http_post raises on non-2xx (after retries on 5xx/429); Discord returns 204 on success.
    try:
        _http_post(webhook_url, payload, headers)
    except LeetCodeError as exc:
        raise SystemExit(f"Discord webhook failed: {exc}")


def main() -> None:
    webhook_url = os.environ.get(WEBHOOK_ENV)
    if not webhook_url:
        raise SystemExit(f"Missing environment variable: {WEBHOOK_ENV}")

    users_path = os.environ.get("USERS_FILE", USERS_FILE_DEFAULT)
    timezone_name = os.environ.get(TIMEZONE_ENV, DEFAULT_TIMEZONE)
    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise SystemExit(f"Invalid {TIMEZONE_ENV} '{timezone_name}': {exc}")

    users = load_users(users_path)
    user_results = collect_user_results(users, tz)

    if all(r.error is not None for r in user_results):
        raise SystemExit("All users failed to fetch; not posting an empty report.")

    report_date = get_today_local(tz)
    content, embeds = build_report(user_results, report_date)
    send_discord_webhook(webhook_url, content, embeds)
    print("Daily LeetCode report posted successfully.")


if __name__ == "__main__":
    main()
