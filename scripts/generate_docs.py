#!/usr/bin/env python3
"""Generate the Dumpstarr documentation section from the Database repo.

The website is a single static HTML file. This script reads the Profilarr
Database SQL operations, reconstructs the current state in SQLite, and injects a
human-readable documentation section into index.html.
"""

from __future__ import annotations

import html
import json
import os
import pathlib
import re
import sqlite3
import subprocess
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
DEFAULT_DATABASE = ROOT.parent / "Database"
DATABASE = pathlib.Path(os.environ.get("DATABASE_REPO_PATH", DEFAULT_DATABASE)).resolve()
OPS = DATABASE / "ops"
META = DATABASE / "pcd.json"

START = "<!-- AUTO DOCS START -->"
END = "<!-- AUTO DOCS END -->"

EXTRA_COLUMNS = {
    "qualities": ["name"],
    "languages": ["name"],
    "quality_api_mappings": ["name", "quality_name", "arr_type", "api_id", "api_name"],
    "custom_formats": ["include_in_rename"],
    "custom_format": ["include_in_rename"],
}


def sh(args: list[str], cwd: pathlib.Path) -> str:
    try:
        return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def natural_ops() -> list[pathlib.Path]:
    if not OPS.exists():
        raise SystemExit(f"Database ops directory not found: {OPS}")

    def key(path: pathlib.Path) -> tuple[int, str]:
        match = re.match(r"(\d+)\.", path.name)
        return (int(match.group(1)) if match else 999999, path.name)

    return sorted(OPS.glob("*.sql"), key=key)


def build_schema(sql_text: str) -> dict[str, set[str]]:
    columns: dict[str, set[str]] = defaultdict(set)
    for match in re.finditer(r'INSERT INTO\s+"?([A-Za-z_][\w]*)"?\s*\(([^)]*)\)', sql_text, re.I):
        table = match.group(1)
        for col in match.group(2).split(","):
            columns[table].add(col.strip().strip('"'))
    for table in list(columns):
        if table.endswith("s"):
            columns[table[:-1]].update(columns[table])
    for table, extra in EXTRA_COLUMNS.items():
        columns[table].update(extra)
    return columns


def connect_database(ops: list[pathlib.Path]) -> sqlite3.Connection:
    sql_text = "\n".join(path.read_text(errors="ignore") for path in ops)
    columns = build_schema(sql_text)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for table, cols in sorted(columns.items()):
        clean = [c for c in cols if c and re.match(r"^[A-Za-z_]\w*$", c)]
        if "name" not in clean:
            clean.append("name")
        defs = ", ".join(f'"{col}" TEXT' for col in clean)
        unique = ", UNIQUE(name) ON CONFLICT REPLACE" if "name" in clean else ""
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({defs}{unique})')
    errors: list[str] = []
    for path in ops:
        try:
            conn.executescript(path.read_text(errors="ignore"))
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    if errors:
        raise SystemExit("Failed to replay Database SQL:\n" + "\n".join(errors[:10]))
    return conn


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def trim(text: str | None, limit: int = 190) -> str:
    value = " ".join((text or "").replace("\\n", " ").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "..."


def markdown_inline(value: str) -> str:
    escaped = h(value)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def markdown_description(value: str | None) -> str:
    text = (value or "").replace("\\n", "\n").strip()
    if not text:
        return ""

    parts = []
    list_items = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            parts.append("<ul>" + "".join(f"<li>{markdown_inline(item)}</li>" for item in list_items) + "</ul>")
            list_items = []

    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        if line.startswith("- "):
            list_items.append(line[2:].strip())
        else:
            flush_list()
            parts.append(f"<p>{markdown_inline(line)}</p>")

    flush_list()
    return '<div class="docs-markdown">' + "".join(parts) + "</div>"


def h(value: object) -> str:
    return html.escape(str(value), quote=True)


def profile_aliases(profile_name: str) -> list[str]:
    aliases = [profile_name]
    legacy = {
        "Anime 1080p": "1080p Anime",
        "LQ 1080p": "1080p LQ",
    }
    if profile_name in legacy:
        aliases.append(legacy[profile_name])
    return aliases


def initial_quality_names(profile_name: str) -> list[str]:
    initial = OPS / "1.initial.sql"
    if not initial.exists():
        return []
    sql_text = initial.read_text(errors="ignore")
    qualities: list[str] = []
    seen = set()
    for candidate in profile_aliases(profile_name):
        pattern = re.compile(
            r"INSERT INTO quality_profile_qualities.*?WHERE qp\.name = '" + re.escape(candidate) +
            r"' AND (?:qg\.quality_profile_name = qp\.name AND qg\.name|q\.name) = '([^']+)';",
            re.S,
        )
        for match in pattern.finditer(sql_text):
            quality = match.group(1)
            if quality not in seen:
                seen.add(quality)
                qualities.append(quality)
        if qualities:
            return qualities
    return qualities


def quality_names(conn: sqlite3.Connection, profile_name: str) -> list[str]:
    for candidate in profile_aliases(profile_name):
        rows = conn.execute(
            "SELECT COALESCE(quality_group_name, quality_name, name) AS quality "
            "FROM quality_profile_qualities "
            "WHERE quality_profile_name = ? "
            "AND COALESCE(quality_group_name, quality_name, name) IS NOT NULL "
            "AND (enabled IS NULL OR enabled != '0') "
            "ORDER BY CAST(position AS INT)",
            (candidate,),
        ).fetchall()
        qualities = []
        seen = set()
        for row in rows:
            quality = row["quality"]
            if quality and quality not in seen:
                seen.add(quality)
                qualities.append(quality)
        if len(qualities) >= 2:
            return qualities

    return initial_quality_names(profile_name)


def profile_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = []
    profiles = conn.execute(
        "SELECT name, description, minimum_custom_format_score "
        "FROM quality_profiles ORDER BY name"
    ).fetchall()
    for profile in profiles:
        name = profile["name"]
        count = scalar(conn, "SELECT COUNT(*) FROM quality_profile_custom_formats WHERE quality_profile_name = ?", (name,))
        rows.append({
            "name": name,
            "count": count,
            "min_score": profile["minimum_custom_format_score"] or "0",
            "description": profile["description"] or "",
            "qualities": quality_names(conn, name),
        })
    return rows


def display_date(value: str) -> str:
    try:
        return "-".join([value[5:7], value[8:10], value[:4]])
    except Exception:
        return value


def titleize_change(value: str) -> str:
    acronyms = {
        "ai", "asl", "av1", "b&w", "dv", "hdr", "hdr10", "hq", "lq", "ma",
        "mcr", "mtbb", "regex", "sdr", "sql", "tbbt", "tv", "web", "x265",
    }
    small_words = {"a", "an", "and", "for", "from", "in", "of", "on", "the", "to", "with"}
    words = (
        value.rsplit(".sql", 1)[0]
        .replace("-", " ")
        .replace("foramt", "format")
        .strip()
        .split()
    )
    formatted = []
    for index, word in enumerate(words):
        lower = word.lower()
        if lower in acronyms:
            formatted.append(lower.upper())
        elif index > 0 and lower in small_words:
            formatted.append(lower)
        else:
            formatted.append(lower.capitalize())
    return " ".join(formatted)


def github_repo_url(repo: pathlib.Path) -> str:
    remote = sh(["git", "remote", "get-url", "origin"], repo)
    if not remote:
        return ""
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+)(?:\.git)?$", remote)
    return f"https://github.com/{match.group(1)}" if match else ""


def commit_for_path(repo: pathlib.Path, path: pathlib.Path) -> str:
    rel = path.relative_to(repo)
    return sh(["git", "log", "-1", "--format=%H", "--", str(rel)], repo)


def recent_ops(ops: list[pathlib.Path], limit: int) -> list[dict[str, str]]:
    rows = []
    repo_url = github_repo_url(DATABASE)
    for path in reversed(ops[-limit:]):
        number, title = path.name.split(".", 1)
        commit = commit_for_path(DATABASE, path)
        rows.append({
            "number": number,
            "title": titleize_change(title),
            "url": f"{repo_url}/commit/{commit}" if repo_url and commit else "",
        })
    return rows


def readme_fixes() -> list[tuple[str, str]]:
    readme = DATABASE / "README.md"
    if not readme.exists():
        return []
    rows = []
    in_fixes = False
    for line in readme.read_text(errors="ignore").splitlines():
        if "Fixes & Features" in line:
            in_fixes = True
            continue
        if not in_fixes:
            continue
        if not line.startswith("|") or "---" in line or "Show" in line:
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            rows.append((parts[0], parts[1]))
    return rows


def render_section(conn: sqlite3.Connection, ops: list[pathlib.Path]) -> str:
    meta = json.loads(META.read_text()) if META.exists() else {}
    commit_date = sh(["git", "log", "-1", "--format=%cs"], DATABASE) or "unknown"
    modified = display_date(commit_date)
    profiles = profile_rows(conn)

    stats = [
        ("Profiles", len(profiles)),
        ("Custom formats", scalar(conn, "SELECT COUNT(*) FROM custom_formats")),
        ("Regex patterns", scalar(conn, "SELECT COUNT(*) FROM regular_expressions")),
    ]

    stat_html = "\n".join(
        f'<article class="docs-stat"><span>{h(label)}</span><strong>{h(value)}</strong></article>'
        for label, value in stats
    )

    profile_html_parts = []
    for profile in profiles:
        qualities = profile["qualities"] if isinstance(profile["qualities"], list) else []
        target_quality = qualities[0] if qualities else "Not specified"
        profile_html_parts.append(
            '<article class="docs-profile-card">'
            f"<h4>{h(profile['name'])}</h4>"
            f"{markdown_description(str(profile['description'] or ''))}"
            '<div class="docs-profile-metrics">'
            f"<span><small>Formats</small><strong>{h(profile['count'])}</strong></span>"
            f"<span><small>Min score</small><strong>{h(profile['min_score'])}</strong></span>"
            f"<span><small>Target quality</small><strong>{h(target_quality)}</strong></span>"
            "</div>"
            "</article>"
        )
    profile_html = "\n".join(profile_html_parts)

    fixes = readme_fixes()
    op_limit = max(1, len(fixes))
    op_html = "\n".join(
        f'<tr><td data-label="Op"><a href="{h(row["url"])}" target="_blank" rel="noopener">#{h(row["number"])}</a></td><td data-label="Change">{h(row["title"])}</td></tr>'
        for row in recent_ops(ops, op_limit)
    )

    fix_html = "\n".join(
        f'<tr><td data-label="Show">{h(show)}</td><td data-label="Behavior">{h(detail)}</td></tr>' for show, detail in fixes
    )

    return f"""{START}
  <section id="docs" class="docs-section">
    <style>
      .docs-section {{ background: var(--surface); }}
      .docs-meta {{ display:flex; flex-wrap:wrap; gap:10px; justify-content:flex-start; color:var(--on-surface-v); font-size:.92rem; margin-bottom:28px; text-align:left; }}
      .docs-meta code {{ background:var(--surface-1); padding:3px 8px; border-radius:999px; color:var(--purple); }}
      .docs-stats {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin:32px 0; }}
      .docs-stat {{ background:linear-gradient(145deg,var(--surface-1),#fff); border:1px solid var(--outline); border-radius:var(--radius-m); padding:18px; }}
      .docs-stat span {{ display:block; color:var(--on-surface-v); font-size:.82rem; text-transform:uppercase; letter-spacing:.06em; }}
      .docs-stat strong {{ display:block; font-family:'Outfit',sans-serif; font-size:2rem; color:var(--purple); margin:4px 0; }}
      .docs-panel {{ background:#fff; border:1px solid var(--outline); border-radius:var(--radius-l); padding:24px; box-shadow:0 8px 26px rgba(28,27,31,.06); overflow:hidden; }}
      .docs-panel h3 {{ font-family:'Outfit',sans-serif; margin-bottom:14px; color:var(--on-surface); }}
      .docs-profile-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; align-items:stretch; }}
      .docs-profile-card {{ display:flex; flex-direction:column; border:1px solid var(--outline); border-radius:var(--radius-m); padding:18px; background:var(--surface); }}
      .docs-profile-card h4 {{ font-family:'Outfit',sans-serif; font-size:1.25rem; color:var(--purple); margin:0 0 10px; }}
      .docs-profile-metrics {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin:14px 0 0; padding-top:12px; margin-top:auto; }}
      .docs-profile-metrics span {{ min-width:0; padding:7px 8px; border:1px solid var(--outline); border-radius:var(--radius-s); background:#fff; }}
      .docs-profile-metrics small {{ display:block; color:var(--on-surface-v); font-size:.56rem; line-height:1; font-weight:800; text-transform:uppercase; letter-spacing:.025em; margin-bottom:4px; white-space:nowrap; }}
      .docs-profile-metrics strong {{ display:block; color:var(--on-surface); font-size:.86rem; font-weight:700; line-height:1.15; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
      .docs-markdown {{ font-size:.86rem; line-height:1.42; color:var(--on-surface-v); margin-bottom:14px; }}
      .docs-markdown p {{ margin:0 0 8px; }}
      .docs-markdown ul {{ margin:0; padding-left:18px; }}
      .docs-markdown li {{ margin:4px 0; }}
      .docs-mini-table a {{ color:var(--purple); text-decoration:none; }}
      .docs-mini-table a:hover {{ text-decoration:underline; }}

      .docs-two-col {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:24px; margin-top:24px; }}
      .docs-two-col > .docs-panel {{ min-width:0; }}
      .docs-mini-table {{ width:100%; border-collapse:collapse; font-size:.9rem; }}
      .docs-mini-table td,.docs-mini-table th {{ padding:10px 12px; border-bottom:1px solid var(--outline); text-align:left; vertical-align:top; overflow-wrap:anywhere; }}
      .docs-mini-table th:first-child,.docs-mini-table td:first-child {{ width:72px; white-space:nowrap; color:var(--purple); font-weight:700; }}
      .docs-recent-table {{ table-layout:fixed; }}
      .docs-recent-table th:first-child,.docs-recent-table td:first-child {{ width:72px; }}
      .docs-recent-table th:last-child,.docs-recent-table td:last-child {{ width:auto; }}
      @media (max-width: 1100px) {{ .docs-profile-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .docs-two-col {{ grid-template-columns:1fr; }} }}
      @media (max-width: 860px) {{ .docs-stats,.docs-profile-grid {{ grid-template-columns:1fr; }} }}
      @media (max-width: 640px) {{ .docs-panel {{ padding:18px; }} .docs-mini-table,.docs-mini-table thead,.docs-mini-table tbody,.docs-mini-table tr,.docs-mini-table th,.docs-mini-table td {{ display:block; width:100%; }} .docs-mini-table thead {{ display:none; }} .docs-mini-table tr {{ padding:10px 0; border-bottom:1px solid var(--outline); }} .docs-mini-table td,.docs-mini-table th:first-child,.docs-mini-table td:first-child,.docs-recent-table th:first-child,.docs-recent-table td:first-child,.docs-recent-table th:last-child,.docs-recent-table td:last-child {{ width:100%; padding:6px 0; border-bottom:0; white-space:normal; }} .docs-mini-table td::before {{ content:attr(data-label); display:block; margin-bottom:2px; color:var(--on-surface-v); font-size:.66rem; line-height:1; font-weight:800; text-transform:uppercase; letter-spacing:.04em; }} }}
    </style>
    <div class="section-inner">
      <h2 class="section-title">Database Documentation</h2>
      <p class="section-desc">Automatically generated from the Dumpstarr Database repository. It summarizes the current Profilarr package, profile scoring, regex coverage, and recent SQL changes for Radarr and Sonarr.</p>
      <div class="docs-meta">
        <span>Package: <code>{h(meta.get('name', 'Dumpstarr Database'))} {h(meta.get('version', ''))}</code></span>
        <span>Minimum Profilarr: <code>{h(meta.get('profilarr', {}).get('minimum_version', '2.0.0'))}</code></span>
        <span>Modified: <code>{h(modified)}</code></span>
      </div>
      <div class="docs-stats">{stat_html}</div>
      <div class="docs-panel">
        <h3>Quality profiles</h3>
        <div class="docs-profile-grid">{profile_html}</div>
      </div>
      <div class="docs-two-col">
        <div class="docs-panel">
          <h3>Recent database operations</h3>
          <table class="docs-mini-table docs-recent-table"><thead><tr><th>Op</th><th>Change</th></tr></thead><tbody>{op_html}</tbody></table>
        </div>
        <div class="docs-panel">
          <h3>Targeted show fixes</h3>
          <table class="docs-mini-table"><thead><tr><th>Show</th><th>Behavior</th></tr></thead><tbody>{fix_html}</tbody></table>
        </div>
      </div>
    </div>
  </section>
{END}"""


def update_index(section: str) -> None:
    html_text = INDEX.read_text()
    if '<li><a href="#docs">Docs</a></li>' not in html_text:
        html_text = html_text.replace(
            '<li><a href="#start">Get Started</a></li>',
            '<li><a href="#start">Get Started</a></li>\n        <li><a href="#docs">Docs</a></li>',
        )
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    if pattern.search(html_text):
        html_text = pattern.sub(section, html_text)
    else:
        html_text = html_text.replace("  <!-- FAQ -->", section + "\n\n  <!-- FAQ -->")
    INDEX.write_text(html_text)


def main() -> None:
    ops = natural_ops()
    conn = connect_database(ops)
    update_index(render_section(conn, ops))
    print(f"Generated documentation from {len(ops)} SQL operation files into {INDEX}")


if __name__ == "__main__":
    main()
