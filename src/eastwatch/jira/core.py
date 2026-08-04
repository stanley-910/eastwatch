"""Read-only Jira helper core for eastwatch.

This module deliberately supports only non-mutating operations: probe, view,
prompt, and open. Do not add comments/transitions/assignment here without a
separate workflow design.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ESSENTIAL_FIELDS = [
    "summary",
    "status",
    "issuetype",
    "project",
    "description",
    "assignee",
    "reporter",
    "priority",
    "labels",
    "created",
    "updated",
    "resolution",
    "components",
]
DETAIL_FIELDS = ["fixVersions", "versions"]
CONTENT_FIELDS = ["comment", "attachment", "issuelinks"]
DEFAULT_FIELDS = ",".join(
    dict.fromkeys([*ESSENTIAL_FIELDS, *DETAIL_FIELDS, *CONTENT_FIELDS])
)
SEARCH_FIELDS = "summary,status,issuetype,project,assignee,priority,updated,resolution"
SECTIONS = [
    "essentials",
    "status",
    "details",
    "description",
    "comments",
    "links",
    "attachments",
    "custom",
    "development",
    "all",
]
DEFAULT_SECTIONS = ["essentials"]
ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
CUSTOM_FIELD_ID_RE = re.compile(r"customfield_\d+\Z")
SPRINT_NAME_RE = re.compile(r"(?:^|,)name=([^,\]]+)")


@dataclass
class HttpResult:
    ok: bool
    status: int | None
    final_url: str
    content_type: str | None
    body: str
    error: str | None = None


def normalize_base_url(raw: str) -> str:
    return raw.rstrip("/")


def parse_base_url(raw: str) -> str:
    if "`" in raw or any(character.isspace() for character in raw):
        raise argparse.ArgumentTypeError("base URL must be a safe HTTP(S) URL")
    parsed = urllib.parse.urlparse(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise argparse.ArgumentTypeError("base URL must be a safe HTTP(S) URL")
    return normalize_base_url(raw)


def issue_key(raw: str) -> str:
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme and parsed.netloc:
        m = ISSUE_KEY_RE.search(parsed.path)
    else:
        m = ISSUE_KEY_RE.search(raw.strip().upper())
    if not m:
        raise ValueError(f"could not find Jira issue key in: {raw!r}")
    return m.group(1).upper()


def browse_url(base_url: str, key: str) -> str:
    return f"{normalize_base_url(base_url)}/browse/{urllib.parse.quote(key)}"


def rest_url(base_url: str, path: str, params: dict[str, str] | None = None) -> str:
    url = f"{normalize_base_url(base_url)}/rest/api/2/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def issue_rest_url(base_url: str, key: str, fields: str) -> str:
    return rest_url(base_url, f"issue/{urllib.parse.quote(key)}", {"fields": fields})


DEFAULT_KEYCHAIN_SERVICE = "eastwatch-jira-pat"
LEGACY_KEYCHAIN_SERVICE = "board-watcher-jira-pat"


def keychain_token(service: str, account: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def bearer_header(token_or_header: str) -> str:
    value = token_or_header.strip()
    if re.match(r"(?i)^(bearer|basic)\s+", value):
        return value
    return f"Bearer {value}"


def auth_headers(args: argparse.Namespace) -> tuple[dict[str, str], str]:
    headers: dict[str, str] = {}
    sources: list[str] = []

    if os.environ.get("JIRA_AUTH_HEADER"):
        headers["Authorization"] = os.environ["JIRA_AUTH_HEADER"]
        sources.append("JIRA_AUTH_HEADER")

    if (
        args.keychain_service
        and args.keychain_account
        and "Authorization" not in headers
    ):
        services = [args.keychain_service]
        if args.keychain_service == DEFAULT_KEYCHAIN_SERVICE:
            services.append(LEGACY_KEYCHAIN_SERVICE)
        last_error = None
        for service in services:
            try:
                headers["Authorization"] = bearer_header(
                    keychain_token(service, args.keychain_account)
                )
            except subprocess.CalledProcessError as exc:
                last_error = exc
                continue
            sources.append(f"keychain:{service}/{args.keychain_account}")
            break
        else:
            raise SystemExit(
                "could not read Jira PAT from keychain: "
                f"services={services!r} account={args.keychain_account!r}"
            ) from last_error

    user = os.environ.get("JIRA_USER")
    token = os.environ.get("JIRA_TOKEN")
    if user and token and "Authorization" not in headers:
        raw = f"{user}:{token}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        sources.append("JIRA_USER/JIRA_TOKEN")

    if os.environ.get("JIRA_COOKIE"):
        headers["Cookie"] = os.environ["JIRA_COOKIE"]
        sources.append("JIRA_COOKIE")

    return headers, ", ".join(sources) if sources else "none"


def request(
    method: str, url: str, headers: dict[str, str], body_limit: int | None = 4000
) -> HttpResult:
    req_headers = {
        "Accept": "application/json,text/html;q=0.8,*/*;q=0.5",
        "User-Agent": "eastwatch-jira-board/0.2",
        **headers,
    }
    req = urllib.request.Request(url, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = ""
            if method != "HEAD":
                raw = resp.read() if body_limit is None else resp.read(body_limit)
                body = raw.decode("utf-8", "replace")
            return HttpResult(
                ok=200 <= resp.status < 300,
                status=resp.status,
                final_url=resp.geturl(),
                content_type=resp.headers.get("content-type"),
                body=body,
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read() if body_limit is None else exc.read(body_limit)
        body = raw.decode("utf-8", "replace")
        return HttpResult(
            ok=False,
            status=exc.code,
            final_url=exc.geturl(),
            content_type=exc.headers.get("content-type"),
            body=body,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — CLI helper should surface raw boundary failures.
        return HttpResult(
            ok=False,
            status=None,
            final_url=url,
            content_type=None,
            body="",
            error=f"{type(exc).__name__}: {exc}",
        )


def fetch_issue(
    base_url: str, key: str, fields: str, headers: dict[str, str]
) -> tuple[HttpResult, dict[str, Any] | None]:
    result = request(
        "GET", issue_rest_url(base_url, key, fields), headers, body_limit=None
    )
    if not result.ok:
        return result, None
    try:
        return result, json.loads(result.body)
    except json.JSONDecodeError as exc:
        result.ok = False
        result.error = f"JSONDecodeError: {exc}"
        return result, None


def fetch_remote_links(
    base_url: str, key: str, headers: dict[str, str]
) -> tuple[HttpResult, list[dict[str, Any]]]:
    result = request(
        "GET",
        rest_url(base_url, f"issue/{urllib.parse.quote(key)}/remotelink"),
        headers,
        body_limit=None,
    )
    if not result.ok:
        return result, []
    try:
        payload = json.loads(result.body)
    except json.JSONDecodeError as exc:
        result.ok = False
        result.error = f"JSONDecodeError: {exc}"
        return result, []
    return result, payload if isinstance(payload, list) else []


def fetch_search(
    base_url: str,
    jql: str,
    fields: str,
    max_results: int,
    headers: dict[str, str],
) -> tuple[HttpResult, dict[str, Any] | None]:
    result = request(
        "GET",
        rest_url(
            base_url,
            "search",
            {"jql": jql, "fields": fields, "maxResults": str(max_results)},
        ),
        headers,
        body_limit=None,
    )
    if not result.ok:
        return result, None
    try:
        return result, json.loads(result.body)
    except json.JSONDecodeError as exc:
        result.ok = False
        result.error = f"JSONDecodeError: {exc}"
        return result, None


def merge_fields(base: str, extras: Iterable[str]) -> str:
    fields = [part.strip() for part in base.split(",") if part.strip()]
    return ",".join(dict.fromkeys([*fields, *extras]))


def parse_field_id(raw: str) -> str:
    field_id = raw.strip()
    if not CUSTOM_FIELD_ID_RE.fullmatch(field_id):
        raise argparse.ArgumentTypeError("field IDs must match customfield_<number>")
    return field_id


def parse_custom_field(raw: str) -> tuple[str, str]:
    label, separator, field_id = raw.partition("=")
    label = label.strip()
    valid_label = (
        label
        and len(label) <= 80
        and "`" not in label
        and all(character.isprintable() for character in label)
    )
    if not separator or not valid_label or not field_id.strip():
        raise argparse.ArgumentTypeError("custom fields must use a safe LABEL=FIELD_ID")
    return label, parse_field_id(field_id)


def configured_custom_fields(args: argparse.Namespace) -> dict[str, str]:
    return dict(args.custom_fields or [])


def configured_fields(args: argparse.Namespace) -> str:
    extras = list(configured_custom_fields(args).values())
    if args.development_field:
        extras.append(args.development_field)
    return merge_fields(args.fields, extras)


def safe_attachment_filename(name: str) -> str:
    basename = Path(name).name
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", basename).strip(". ")
    return safe or "attachment"


def download_url(url: str, headers: dict[str, str]) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "eastwatch-jira-board/0.2", **headers},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def download_attachment(
    attachment: dict[str, Any],
    out_dir: Path,
    headers: dict[str, str],
    overwrite: bool = False,
) -> Path:
    url = attachment.get("content")
    filename = safe_attachment_filename(str(attachment.get("filename") or "attachment"))
    if not url:
        raise ValueError(f"attachment {filename!r} has no content URL")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    if path.exists() and not overwrite:
        return path
    path.write_bytes(download_url(str(url), headers))
    return path


def field(issue: dict[str, Any], name: str) -> Any:
    return (issue.get("fields") or {}).get(name)


def display_name(value: Any) -> str:
    if not value:
        return "-"
    if isinstance(value, dict):
        return str(
            value.get("displayName") or value.get("name") or value.get("key") or "-"
        )
    return str(value)


def compact_text(value: str, limit: int) -> str:
    if limit == 0:
        return value.rstrip()
    return value.rstrip()[:limit]


def plain_description(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return compact_text(value.strip(), limit)
    return compact_text(json.dumps(value, ensure_ascii=False, indent=2), limit)


def comments(issue: dict[str, Any], limit: int | None = None) -> list[dict[str, Any]]:
    raw = field(issue, "comment") or {}
    items = raw.get("comments") or []
    if limit is None or limit == 0:
        return items
    return items[-limit:]


def list_names(values: list[dict[str, Any]] | None) -> str:
    if not values:
        return "None"
    return ", ".join(display_name(value) for value in values)


def format_custom_value(value: Any) -> str:
    if value in (None, [], {}, ""):
        return "None"
    if isinstance(value, list):
        return (
            ", ".join(format_custom_value(item) for item in value) if value else "None"
        )
    if isinstance(value, dict):
        for key in ("value", "name", "displayName", "key", "id"):
            if value.get(key):
                return str(value[key])
        return json.dumps(value, ensure_ascii=False)[:500]
    text = str(value)
    sprint_name = SPRINT_NAME_RE.search(text)
    if sprint_name:
        return sprint_name.group(1)
    return text


def selected_sections(args: argparse.Namespace) -> list[str]:
    raw = args.section or DEFAULT_SECTIONS
    sections = []
    for item in raw:
        sections.extend(part.strip() for part in item.split(",") if part.strip())
    unknown = [section for section in sections if section not in SECTIONS]
    if unknown:
        raise ValueError(f"unknown section(s): {', '.join(unknown)}")
    if "all" in sections:
        return [section for section in SECTIONS if section != "all"]
    return list(dict.fromkeys(sections)) or DEFAULT_SECTIONS


def needs_remote_links(sections: list[str]) -> bool:
    return "links" in sections


def heading(title: str, markdown: bool) -> None:
    print(f"## {title}" if markdown else f"{title}:")


def print_kv(key: str, value: Any) -> None:
    print(f"{key}: {value if value not in (None, '') else '-'}")


def render_essentials(
    issue: dict[str, Any], base: str, key: str, markdown: bool
) -> None:
    status = field(issue, "status") or {}
    issue_type = field(issue, "issuetype") or {}
    project = field(issue, "project") or {}
    resolution = field(issue, "resolution") or {}
    priority = field(issue, "priority") or {}
    labels = field(issue, "labels") or []
    print_kv("Jira issue" if markdown else "key", issue.get("key", key))
    print_kv("URL", browse_url(base, key))
    print_kv("Summary", field(issue, "summary") or "-")
    print_kv("Status", status.get("name", "-"))
    print_kv("Resolution", resolution.get("name", "None") if resolution else "None")
    print_kv("Type", issue_type.get("name", "-"))
    print_kv("Project", f"{project.get('key', '-')} — {project.get('name', '-')}")
    print_kv("Priority", priority.get("name", "-") if priority else "-")
    print_kv("Assignee", display_name(field(issue, "assignee")))
    print_kv("Reporter", display_name(field(issue, "reporter")))
    print_kv("Labels", ", ".join(labels) if labels else "None")
    print_kv("Updated", field(issue, "updated") or "-")


def render_status(issue: dict[str, Any]) -> None:
    status = field(issue, "status") or {}
    resolution = field(issue, "resolution") or {}
    print_kv("Status", status.get("name", "-"))
    print_kv("Resolution", resolution.get("name", "None") if resolution else "None")


def render_details(issue: dict[str, Any]) -> None:
    print_kv("Fix Version/s", list_names(field(issue, "fixVersions")))
    print_kv("Affects Version/s", list_names(field(issue, "versions")))
    print_kv("Component/s", list_names(field(issue, "components")))
    print_kv("Created", field(issue, "created") or "-")
    print_kv("Updated", field(issue, "updated") or "-")


def render_description(issue: dict[str, Any], args: argparse.Namespace) -> None:
    print(plain_description(field(issue, "description"), args.body_limit) or "-")


def render_comments(issue: dict[str, Any], args: argparse.Namespace) -> None:
    raw = field(issue, "comment") or {}
    items = comments(issue, args.comments_limit)
    print(f"total: {raw.get('total', len(items))}")
    print(f"shown: {len(items)}")
    if not items:
        print("-")
        return
    for c in items:
        author = display_name(c.get("author"))
        body = plain_description(c.get("body"), args.body_limit)
        print(f"- {author} at {c.get('created', '-')}")
        print(textwrap.indent(body or "-", "  "))


def render_attachments(issue: dict[str, Any]) -> None:
    attachments = field(issue, "attachment") or []
    print(f"total: {len(attachments)}")
    if not attachments:
        print("-")
        return
    for item in attachments:
        print(
            f"- {item.get('filename', '-')} | {item.get('mimeType', '-')} | "
            f"{item.get('size', '-')} bytes | content: {item.get('content', '-')}"
        )


def issue_link_other(link: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if link.get("outwardIssue"):
        return "outward", link["outwardIssue"]
    if link.get("inwardIssue"):
        return "inward", link["inwardIssue"]
    return "-", {}


def render_links(
    issue: dict[str, Any], remote_links: list[dict[str, Any]], args: argparse.Namespace
) -> None:
    issue_links = field(issue, "issuelinks") or []
    print(f"issue_links_total: {len(issue_links)}")
    for link in issue_links:
        direction, other = issue_link_other(link)
        link_type = (link.get("type") or {}).get("name", "-")
        other_fields = other.get("fields") or {}
        print(
            f"- {link_type} {direction} {other.get('key', '-')} — {other_fields.get('summary', '-')}"
        )
    print(f"remote_links_total: {len(remote_links)}")
    shown = (
        remote_links
        if args.remote_link_limit == 0
        else remote_links[: args.remote_link_limit]
    )
    for link in shown:
        obj = link.get("object") or {}
        print(
            f"- {link.get('relationship') or '-'} {obj.get('title') or '-'} — {obj.get('url') or '-'}"
        )
    if len(shown) < len(remote_links):
        print(
            f"... {len(remote_links) - len(shown)} more remote links hidden; use --remote-link-limit 0"
        )


def linked_issue_keys(issue: dict[str, Any]) -> list[str]:
    keys = []
    for link in field(issue, "issuelinks") or []:
        _, other = issue_link_other(link)
        if other.get("key"):
            keys.append(other["key"])
    return list(dict.fromkeys(keys))


def render_search(payload: dict[str, Any], base: str) -> None:
    issues = payload.get("issues") or []
    print(f"total: {payload.get('total', len(issues))}")
    print(f"shown: {len(issues)}")
    if not issues:
        print("-")
        return
    for issue in issues:
        fields = issue.get("fields") or {}
        status = fields.get("status") or {}
        priority = fields.get("priority") or {}
        issue_type = fields.get("issuetype") or {}
        resolution = fields.get("resolution") or {}
        assignee = display_name(fields.get("assignee"))
        print(
            f"- {issue.get('key', '-')} | {status.get('name', '-')} | "
            f"resolution={resolution.get('name', 'Unresolved') if resolution else 'Unresolved'} | "
            f"type={issue_type.get('name', '-')} | priority={priority.get('name', '-') if priority else '-'} | "
            f"assignee={assignee} | updated={fields.get('updated', '-')} | "
            f"{fields.get('summary', '-')} | {browse_url(base, issue.get('key', '-'))}"
        )


def render_custom(issue: dict[str, Any], custom_fields: dict[str, str]) -> None:
    if not custom_fields:
        print("Not configured")
        return
    for label, field_id in custom_fields.items():
        print_kv(label, format_custom_value(field(issue, field_id)))


def development_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    marker = "devSummaryJson="
    if marker not in value:
        return None
    raw = value.split(marker, 1)[1]
    try:
        parsed, _ = json.JSONDecoder().raw_decode(raw)
        return parsed
    except json.JSONDecodeError:
        return None


def render_development(issue: dict[str, Any], field_id: str | None) -> None:
    if field_id is None:
        print("Not configured")
        return
    value = field(issue, field_id)
    if value in (None, [], {}, ""):
        print("None")
        return
    summary = development_summary(value)
    if not summary:
        print(format_custom_value(value))
        return
    cached = summary.get("cachedValue") or {}
    print_kv("isStale", summary.get("isStale"))
    for name, item in (cached.get("summary") or {}).items():
        overall = item.get("overall") or {}
        by_instance = item.get("byInstanceType") or {}
        instances = (
            ", ".join(
                f"{key}={value.get('count', 0)} ({value.get('name', key)})"
                for key, value in by_instance.items()
            )
            or "none"
        )
        details = []
        for key in (
            "count",
            "state",
            "openCount",
            "mergedCount",
            "declinedCount",
            "lastUpdated",
        ):
            if key in overall:
                details.append(f"{key}={overall[key]}")
        nested_details = overall.get("details") or {}
        for key in ("openCount", "mergedCount", "declinedCount", "total"):
            if key in nested_details:
                details.append(f"{key}={nested_details[key]}")
        print(
            f"- {name}: {', '.join(details) if details else 'no overall details'}; instances: {instances}"
        )


def render_issue(
    issue: dict[str, Any],
    base: str,
    key: str,
    args: argparse.Namespace,
    remote_links: list[dict[str, Any]] | None = None,
    markdown: bool = False,
) -> None:
    remote_links = remote_links or []
    sections = selected_sections(args)
    custom_fields = configured_custom_fields(args)
    if markdown:
        print(f"Jira issue: {issue.get('key', key)}")
        print(f"URL: {browse_url(base, key)}")
    else:
        print("Jira issue snapshot")
    for section in sections:
        if section != "essentials":
            print()
            heading(section.replace("_", " ").title(), markdown)
        if section == "essentials":
            render_essentials(issue, base, key, markdown)
        elif section == "status":
            render_status(issue)
        elif section == "details":
            render_details(issue)
        elif section == "description":
            render_description(issue, args)
        elif section == "comments":
            render_comments(issue, args)
        elif section == "links":
            render_links(issue, remote_links, args)
        elif section == "attachments":
            render_attachments(issue)
        elif section == "custom":
            render_custom(issue, custom_fields)
        elif section == "development":
            render_development(issue, args.development_field)


def print_result(label: str, result: HttpResult) -> None:
    print(f"{label}:")
    print(f"  ok: {result.ok}")
    print(f"  status: {result.status}")
    print(f"  final_url: {result.final_url}")
    print(f"  content_type: {result.content_type}")
    if result.error:
        print(f"  error: {result.error}")
    if result.body:
        compact = " ".join(result.body.strip().split())[:500]
        print(f"  body_prefix: {compact}")


def cmd_probe(args: argparse.Namespace) -> int:
    key = issue_key(args.issue)
    base = normalize_base_url(args.base_url)
    headers, auth_source = auth_headers(args)

    print("read-only Jira helper probe")
    print(f"issue_key: {key}")
    print(f"base_url: {base}")
    print(f"browse_url: {browse_url(base, key)}")
    fields = configured_fields(args)
    print(f"rest_url: {issue_rest_url(base, key, fields)}")
    print(f"auth_source: {auth_source}")
    print()

    print_result("browse HEAD", request("HEAD", browse_url(base, key), headers))
    print()
    print_result("rest GET", request("GET", issue_rest_url(base, key, fields), headers))
    return 0


def fetch_for_render(
    args: argparse.Namespace,
) -> tuple[
    str, str, dict[str, str], HttpResult, dict[str, Any] | None, list[dict[str, Any]]
]:
    key = issue_key(args.issue)
    base = normalize_base_url(args.base_url)
    headers, _ = auth_headers(args)
    result, issue = fetch_issue(base, key, configured_fields(args), headers)
    remote_links: list[dict[str, Any]] = []
    if issue and needs_remote_links(selected_sections(args)):
        _, remote_links = fetch_remote_links(base, key, headers)
    return key, base, headers, result, issue, remote_links


def cmd_view(args: argparse.Namespace) -> int:
    key, base, _, result, issue, remote_links = fetch_for_render(args)
    if not issue:
        _, auth_source = auth_headers(args)
        print("Jira view failed")
        print(f"issue_key: {key}")
        print(f"auth_source: {auth_source}")
        print_result("rest GET", result)
        return 2
    render_issue(issue, base, key, args, remote_links, markdown=False)
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    key, base, _, result, issue, remote_links = fetch_for_render(args)
    if not issue:
        _, auth_source = auth_headers(args)
        print(f"Jira issue: {key}")
        print(f"URL: {browse_url(base, key)}")
        print(f"Auth source: {auth_source}")
        print(f"Fetch status: {result.status or result.error}")
        if result.body:
            print("Fetch response:")
            print(textwrap.indent(" ".join(result.body.split())[:500], "  "))
        print(
            "\nCould not fetch Jira fields. Treat the URL as navigation context only."
        )
        return 2
    render_issue(issue, base, key, args, remote_links, markdown=True)
    return 0


def cmd_attachments(args: argparse.Namespace) -> int:
    key = issue_key(args.issue)
    base = normalize_base_url(args.base_url)
    headers, _ = auth_headers(args)
    result, issue = fetch_issue(
        base, key, merge_fields(configured_fields(args), ["attachment"]), headers
    )
    if not issue:
        print("Jira attachments failed")
        print_result("rest GET", result)
        return 2
    render_attachments(issue)
    return 0


def cmd_download_attachments(args: argparse.Namespace) -> int:
    key = issue_key(args.issue)
    base = normalize_base_url(args.base_url)
    headers, _ = auth_headers(args)
    result, issue = fetch_issue(
        base, key, merge_fields(configured_fields(args), ["attachment"]), headers
    )
    if not issue:
        print("Jira attachment download failed")
        print_result("rest GET", result)
        return 2
    attachments = field(issue, "attachment") or []
    out_dir = Path(args.out).expanduser() if args.out else Path.cwd() / f"jira-{key}"
    print(f"issue_key: {key}")
    print(f"out_dir: {out_dir}")
    print(f"total: {len(attachments)}")
    for attachment in attachments:
        candidate = out_dir / safe_attachment_filename(
            str(attachment.get("filename") or "attachment")
        )
        existed = candidate.exists()
        path = download_attachment(
            attachment, out_dir, headers, overwrite=args.overwrite
        )
        status = "present" if existed and not args.overwrite else "saved"
        print(f"- {status}: {path}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    base = normalize_base_url(args.base_url)
    headers, _ = auth_headers(args)
    result, payload = fetch_search(
        base, args.issue, args.search_fields, args.max_results, headers
    )
    if payload is None:
        print("Jira search failed")
        print_result("search GET", result)
        return 2
    print(f"jql: {args.issue}")
    render_search(payload, base)
    return 0


def cmd_project_open(args: argparse.Namespace) -> int:
    project = args.issue.strip().upper()
    args.issue = (
        f"project = {project} AND resolution = Unresolved ORDER BY updated DESC"
    )
    return cmd_search(args)


def cmd_linked_open(args: argparse.Namespace) -> int:
    key = issue_key(args.issue)
    base = normalize_base_url(args.base_url)
    headers, _ = auth_headers(args)
    result, issue = fetch_issue(
        base, key, merge_fields(configured_fields(args), ["issuelinks"]), headers
    )
    if not issue:
        print("Jira linked issue fetch failed")
        print_result("rest GET", result)
        return 2
    keys = linked_issue_keys(issue)
    print(f"source_issue: {key}")
    print(f"linked_issue_keys_total: {len(keys)}")
    if not keys:
        print("-")
        return 0
    jql = (
        "key in ("
        + ",".join(keys)
        + ") AND resolution = Unresolved ORDER BY updated DESC"
    )
    result, payload = fetch_search(
        base, jql, args.search_fields, args.max_results, headers
    )
    if payload is None:
        print("Jira linked-open search failed")
        print_result("search GET", result)
        return 2
    print(f"jql: {jql}")
    render_search(payload, base)
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    key = issue_key(args.issue)
    url = browse_url(args.base_url, key)
    print(url)
    try:
        subprocess.run(["open", url], check=False)
    except OSError as exc:
        print(f"open failed: {exc}", file=sys.stderr)
        return 2
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="read-only Jira helper")
    p.add_argument(
        "command",
        nargs="?",
        default="probe",
        choices=[
            "probe",
            "view",
            "prompt",
            "open",
            "attachments",
            "download-attachments",
            "search",
            "project-open",
            "linked-open",
        ],
        help="read-only action to run (default: probe)",
    )
    p.add_argument(
        "issue", help="Jira key/URL, project key for project-open, or JQL for search"
    )
    default_base_url = os.environ.get("JIRA_BASE_URL") or None
    p.add_argument(
        "--base-url",
        default=default_base_url,
        required=default_base_url is None,
        type=parse_base_url,
        help="Jira server URL; may also be set with JIRA_BASE_URL",
    )
    p.add_argument("--fields", default=os.environ.get("JIRA_FIELDS", DEFAULT_FIELDS))
    p.add_argument(
        "--custom-field",
        dest="custom_fields",
        action="append",
        type=parse_custom_field,
        default=None,
        metavar="LABEL=FIELD_ID",
        help="Jira custom field to fetch and render; repeat for multiple fields",
    )
    p.add_argument(
        "--development-field",
        default=os.environ.get("JIRA_DEVELOPMENT_FIELD"),
        type=parse_field_id,
        metavar="FIELD_ID",
        help="Jira development-summary field; may also be set with JIRA_DEVELOPMENT_FIELD",
    )
    p.add_argument(
        "--section",
        action="append",
        help=(
            "section to render; repeat or comma-separate. "
            "Default: essentials. Choices: " + ", ".join(SECTIONS)
        ),
    )
    p.add_argument(
        "--comments-limit",
        type=int,
        default=0,
        help="number of comments to show in comments section; 0 means all (default)",
    )
    p.add_argument(
        "--remote-link-limit",
        type=int,
        default=25,
        help="number of remote links to show in links section; 0 means all",
    )
    p.add_argument(
        "--body-limit",
        type=int,
        default=2000,
        help="max characters per description/comment body; 0 means full text",
    )
    p.add_argument(
        "--out",
        help="output directory for download-attachments; default: ./jira-<KEY>",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="overwrite existing files for download-attachments",
    )
    p.add_argument(
        "--max-results",
        type=int,
        default=25,
        help="maximum Jira search results for search/project-open/linked-open",
    )
    p.add_argument(
        "--search-fields",
        default=os.environ.get("JIRA_SEARCH_FIELDS", SEARCH_FIELDS),
        help="comma-separated fields returned by Jira search commands",
    )
    p.add_argument(
        "--keychain-service",
        default=os.environ.get("JIRA_KEYCHAIN_SERVICE") or DEFAULT_KEYCHAIN_SERVICE,
        help="macOS Keychain service containing a Jira PAT",
    )
    p.add_argument(
        "--keychain-account",
        default=os.environ.get("JIRA_KEYCHAIN_ACCOUNT") or getpass.getuser(),
        help="macOS Keychain account containing a Jira PAT",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "probe":
            return cmd_probe(args)
        if args.command == "view":
            return cmd_view(args)
        if args.command == "prompt":
            return cmd_prompt(args)
        if args.command == "attachments":
            return cmd_attachments(args)
        if args.command == "download-attachments":
            return cmd_download_attachments(args)
        if args.command == "search":
            return cmd_search(args)
        if args.command == "project-open":
            return cmd_project_open(args)
        if args.command == "linked-open":
            return cmd_linked_open(args)
        if args.command == "open":
            return cmd_open(args)
        raise AssertionError(args.command)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
