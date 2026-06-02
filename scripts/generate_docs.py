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
from collections import Counter, defaultdict

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


def h(value: object) -> str:
    return html.escape(str(value), quote=True)


def profile_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = []
    profiles = conn.execute(
        "SELECT name, description, minimum_custom_format_score, upgrade_until_score "
        "FROM quality_profiles ORDER BY name"
    ).fetchall()
    for profile in profiles:
        name = profile["name"]
        arr_types = [r[0] for r in conn.execute(
            "SELECT DISTINCT arr_type FROM quality_profile_custom_formats "
            "WHERE quality_profile_name = ? ORDER BY arr_type",
            (name,),
        ).fetchall()]
        app = ", ".join(a.title() if a != "all" else "Both" for a in arr_types) or "Both"
        count = scalar(conn, "SELECT COUNT(*) FROM quality_profile_custom_formats WHERE quality_profile_name = ?", (name,))
        positive = scalar(conn, "SELECT COUNT(*) FROM quality_profile_custom_formats WHERE quality_profile_name = ? AND CAST(score AS INT) > 0", (name,))
        negative = scalar(conn, "SELECT COUNT(*) FROM quality_profile_custom_formats WHERE quality_profile_name = ? AND CAST(score AS INT) < 0", (name,))
        zero = scalar(conn, "SELECT COUNT(*) FROM quality_profile_custom_formats WHERE quality_profile_name = ? AND CAST(score AS INT) = 0", (name,))
        hard_blocks = scalar(conn, "SELECT COUNT(*) FROM quality_profile_custom_formats WHERE quality_profile_name = ? AND CAST(score AS INT) <= -10000", (name,))
        top = conn.execute(
            "SELECT custom_format_name, score FROM quality_profile_custom_formats "
            "WHERE quality_profile_name = ? ORDER BY CAST(score AS INT) DESC LIMIT 3",
            (name,),
        ).fetchall()
        rows.append({
            "name": name,
            "app": app,
            "count": count,
            "positive": positive,
            "negative": negative,
            "zero": zero,
            "hard_blocks": hard_blocks,
            "min_score": profile["minimum_custom_format_score"] or "0",
            "upgrade_until": profile["upgrade_until_score"] or "0",
            "description": trim(profile["description"], 230),
            "top": ", ".join(f"{r['custom_format_name']} ({r['score']})" for r in top),
        })
    return rows


def tag_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT tag_name, COUNT(*) AS count FROM regular_expression_tags "
        "GROUP BY tag_name ORDER BY count DESC, tag_name LIMIT 12"
    ).fetchall()


def recent_ops(ops: list[pathlib.Path], limit: int = 10) -> list[dict[str, str]]:
    rows = []
    for path in reversed(ops[-limit:]):
        number, title = path.name.split(".", 1)
        title = title.rsplit(".sql", 1)[0].replace("-", " ").strip().capitalize()
        text = path.read_text(errors="ignore")
        begin = re.findall(r"-- --- BEGIN op \d+ \((.*?)\)", text)
        summary = Counter(item.split(' "', 1)[0].strip() for item in begin).most_common(3)
        rows.append({
            "number": number,
            "title": title,
            "summary": ", ".join(f"{name}: {count}" for name, count in summary) or "SQL maintenance",
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
    return rows[:14]


def render_section(conn: sqlite3.Connection, ops: list[pathlib.Path]) -> str:
    meta = json.loads(META.read_text()) if META.exists() else {}
    commit = sh(["git", "rev-parse", "--short", "HEAD"], DATABASE) or "unknown"
    commit_date = sh(["git", "log", "-1", "--format=%cs"], DATABASE) or "unknown"
    generated = commit_date
    profiles = profile_rows(conn)
    max_count = max((int(str(p["count"])) for p in profiles), default=1)

    stats = [
        ("Profiles", len(profiles), "Radarr and Sonarr quality profiles"),
        ("Custom formats", scalar(conn, "SELECT COUNT(*) FROM custom_formats"), "Scoring rules replayed from SQL"),
        ("Regex patterns", scalar(conn, "SELECT COUNT(*) FROM regular_expressions"), "Release title and group matchers"),
        ("SQL operations", len(ops), "Versioned database changes"),
    ]

    stat_html = "\n".join(
        f'<article class="docs-stat"><span>{h(label)}</span><strong>{h(value)}</strong><small>{h(desc)}</small></article>'
        for label, value, desc in stats
    )

    profile_html = "\n".join(
        "<tr>"
        f"<td><strong>{h(p['name'])}</strong><small>{h(p['description'])}</small></td>"
        f"<td>{h(p['app'])}</td>"
        f"<td>{h(p['count'])}</td>"
        f"<td><span class='score good'>+{h(p['positive'])}</span> <span class='score bad'>-{h(p['negative'])}</span> <span class='score neutral'>{h(p['zero'])} zero</span></td>"
        f"<td>{h(p['min_score'])}</td>"
        f"<td>{h(p['hard_blocks'])}</td>"
        f"<td>{h(p['top'])}</td>"
        "</tr>"
        for p in profiles
    )

    chart_html = "\n".join(
        f'<div class="docs-bar-row"><span>{h(p["name"])}</span><div class="docs-bar"><i style="width:{max(6, int(int(str(p["count"])) / max_count * 100))}%"></i></div><b>{h(p["count"])}</b></div>'
        for p in profiles
    )

    tag_html = "\n".join(
        f"<tr><td>{h(row['tag_name'])}</td><td>{h(row['count'])}</td></tr>" for row in tag_rows(conn)
    )

    op_html = "\n".join(
        f"<tr><td>#{h(row['number'])}</td><td>{h(row['title'])}</td><td>{h(row['summary'])}</td></tr>"
        for row in recent_ops(ops)
    )

    fix_html = "\n".join(
        f"<tr><td>{h(show)}</td><td>{h(detail)}</td></tr>" for show, detail in readme_fixes()
    )

    return f"""{START}
  <section id="docs" class="docs-section">
    <style>
      .docs-section {{ background: var(--surface); }}
      .docs-meta {{ display:flex; flex-wrap:wrap; gap:10px; justify-content:center; color:var(--on-surface-v); font-size:.92rem; margin-bottom:28px; }}
      .docs-meta code {{ background:var(--surface-1); padding:3px 8px; border-radius:999px; color:var(--purple); }}
      .docs-stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:32px 0; }}
      .docs-stat {{ background:linear-gradient(145deg,var(--surface-1),#fff); border:1px solid var(--outline); border-radius:var(--radius-m); padding:18px; }}
      .docs-stat span {{ display:block; color:var(--on-surface-v); font-size:.82rem; text-transform:uppercase; letter-spacing:.06em; }}
      .docs-stat strong {{ display:block; font-family:'Outfit',sans-serif; font-size:2rem; color:var(--purple); margin:4px 0; }}
      .docs-stat small {{ color:var(--on-surface-v); }}
      .docs-grid {{ display:grid; grid-template-columns:1.2fr .8fr; gap:24px; margin:24px 0; }}
      .docs-panel {{ background:#fff; border:1px solid var(--outline); border-radius:var(--radius-l); padding:24px; box-shadow:0 8px 26px rgba(28,27,31,.06); overflow:hidden; }}
      .docs-panel h3 {{ font-family:'Outfit',sans-serif; margin-bottom:14px; color:var(--on-surface); }}
      .docs-table-wrap {{ overflow-x:auto; border:1px solid var(--outline); border-radius:var(--radius-m); }}
      .docs-table {{ width:100%; border-collapse:collapse; min-width:860px; font-size:.9rem; }}
      .docs-table th {{ background:var(--surface-1); color:var(--on-surface-v); text-align:left; font-size:.78rem; text-transform:uppercase; letter-spacing:.05em; }}
      .docs-table th,.docs-table td {{ padding:12px 14px; border-bottom:1px solid var(--outline); vertical-align:top; }}
      .docs-table tr:last-child td {{ border-bottom:0; }}
      .docs-table small {{ display:block; color:var(--on-surface-v); max-width:420px; margin-top:4px; }}
      .score {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:.78rem; margin:1px; }}
      .score.good {{ background:var(--green-light); color:var(--green); }}
      .score.bad {{ background:#FFE1D6; color:var(--fire); }}
      .score.neutral {{ background:var(--surface-1); color:var(--on-surface-v); }}
      .docs-bar-row {{ display:grid; grid-template-columns:150px 1fr 42px; gap:10px; align-items:center; margin:10px 0; font-size:.9rem; }}
      .docs-bar {{ height:12px; background:var(--surface-1); border-radius:999px; overflow:hidden; }}
      .docs-bar i {{ display:block; height:100%; background:linear-gradient(90deg,var(--purple),var(--green-mid)); border-radius:999px; }}
      .docs-two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; margin-top:24px; }}
      .docs-mini-table {{ width:100%; border-collapse:collapse; font-size:.9rem; }}
      .docs-mini-table td,.docs-mini-table th {{ padding:9px 0; border-bottom:1px solid var(--outline); text-align:left; }}
      @media (max-width: 860px) {{ .docs-stats,.docs-grid,.docs-two-col {{ grid-template-columns:1fr; }} .docs-bar-row {{ grid-template-columns:1fr; }} }}
    </style>
    <div class="section-inner">
      <h2 class="section-title">Database Documentation</h2>
      <p class="section-desc">Automatically generated from the Dumpstarr Database repository. It summarizes the current Profilarr package, profile scoring, regex coverage, and recent SQL changes for Radarr and Sonarr.</p>
      <div class="docs-meta">
        <span>Package: <code>{h(meta.get('name', 'Dumpstarr Database'))} {h(meta.get('version', ''))}</code></span>
        <span>Minimum Profilarr: <code>{h(meta.get('profilarr', {}).get('minimum_version', '2.0.0'))}</code></span>
        <span>Source commit: <code>{h(commit)}</code></span>
        <span>Source date: <code>{h(commit_date)}</code></span>
        <span>Generated: <code>{h(generated)}</code></span>
      </div>
      <div class="docs-stats">{stat_html}</div>
      <div class="docs-grid">
        <div class="docs-panel">
          <h3>Profile format coverage</h3>
          {chart_html}
        </div>
        <div class="docs-panel">
          <h3>Top regex tags</h3>
          <table class="docs-mini-table"><thead><tr><th>Tag</th><th>Patterns</th></tr></thead><tbody>{tag_html}</tbody></table>
        </div>
      </div>
      <div class="docs-panel">
        <h3>Quality profile matrix</h3>
        <div class="docs-table-wrap"><table class="docs-table"><thead><tr><th>Profile</th><th>App</th><th>Formats</th><th>Score mix</th><th>Min score</th><th>Hard blocks</th><th>Top preferred formats</th></tr></thead><tbody>{profile_html}</tbody></table></div>
      </div>
      <div class="docs-two-col">
        <div class="docs-panel">
          <h3>Recent database operations</h3>
          <table class="docs-mini-table"><thead><tr><th>Op</th><th>Change</th><th>Summary</th></tr></thead><tbody>{op_html}</tbody></table>
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
