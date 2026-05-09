#!/usr/bin/env python3
"""
ODP Summarize Data Catalog Changes

Fetches merged GitHub pull requests from opendataphilly/opendataphilly-jkan for a
configured date range, identifies substantive dataset additions and updates, and
writes a formatted Markdown summary suitable for a mailing list email.

Dependencies (pip install):
    requests
    python-frontmatter
"""

import base64
import datetime
import difflib
import logging
import os

import frontmatter
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Configuration ---
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")   # Personal access token; empty = unauthenticated (60 req/hr)
TEST_MODE     = False         # If True, process only TEST_SIZE pull requests
TEST_SIZE     = 10           # Number of pull requests to process in TEST_MODE
START_DATE    = "4/1/2026"    # Inclusive start date (M/D/YYYY)
END_DATE      = "6/30/2026"   # Inclusive end date (M/D/YYYY)
LOG_DIR       = "logs"
LOG_BASE_NAME = "odp-summarize-changes"
REPO_URL      = "https://github.com/opendataphilly/opendataphilly-jkan/"

# --- Internal constants ---
_API_BASE       = "https://api.github.com/repos/opendataphilly/opendataphilly-jkan"
_DS_PREFIX      = "_datasets/"
_DS_URL_BASE    = "https://opendataphilly.org/datasets/"
_DESC_THRESHOLD = 0.30

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _headers():
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _get(url, params=None):
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=30)
        if r.status_code != 200:
            logger.error("HTTP %s for %s", r.status_code, url)
            return None
        return r.json()
    except requests.RequestException as exc:
        logger.error("Request failed for %s: %s", url, exc)
        return None


def fetch_closed_prs():
    """Return every closed PR from the repository (all pages)."""
    prs, page = [], 1
    print("Fetching closed pull requests...", flush=True)
    while True:
        data = _get(
            f"{_API_BASE}/pulls",
            params={"state": "closed", "per_page": 100, "page": page,
                    "sort": "created", "direction": "desc"},
        )
        if not data:
            break
        prs.extend(data)
        print(f"  {len(prs)} PRs fetched so far...", flush=True)
        if len(data) < 100:
            break
        page += 1
    return prs


def get_pr_files(pr_number):
    """Return the list of file objects changed in a PR (paginated). None on error."""
    files, page = [], 1
    while True:
        data = _get(
            f"{_API_BASE}/pulls/{pr_number}/files",
            params={"per_page": 100, "page": page},
        )
        if data is None:
            return None
        files.extend(data)
        if len(data) < 100:
            break
        page += 1
    return files


def get_file_at_ref(path, ref):
    """Return decoded text content of a file at the given git ref. None on error."""
    data = _get(f"{_API_BASE}/contents/{path}", params={"ref": ref})
    if data is None:
        return None
    if data.get("encoding") == "base64":
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Failed to decode %s at %s: %s", path, ref, exc)
    return None


# ---------------------------------------------------------------------------
# Dataset helpers
# ---------------------------------------------------------------------------

def _parse_config_date(s):
    return datetime.datetime.strptime(s, "%m/%d/%Y").date()


def _gh_date(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).date()


def _dataset_url(filepath):
    slug = os.path.basename(filepath).removesuffix(".md")
    return f"{_DS_URL_BASE}{slug}/"


def _safe_frontmatter(content, context=""):
    try:
        return frontmatter.loads(content)
    except Exception as exc:
        logger.warning("Frontmatter parse error%s: %s", f" ({context})" if context else "", exc)
        return None


def _desc_change_ratio(before, after):
    """
    Ratio of unmatched characters in the longer string to its length.
    Measures how much the description changed relative to the longer version.
    """
    a, b = str(before or ""), str(after or "")
    longer = max(len(a), len(b))
    if longer == 0:
        return 0.0
    matches = sum(blk.size for blk in difflib.SequenceMatcher(None, a, b).get_matching_blocks())
    return (longer - matches) / longer


# ---------------------------------------------------------------------------
# PR processing
# ---------------------------------------------------------------------------

def process_pr(pr):
    """
    Analyse one merged PR for substantive dataset changes.

    Returns:
        new_datasets    – list of {title, url} (and 'reason' when TEST_MODE)
        updated_datasets – list of {title, url} (and 'reason' when TEST_MODE)
        had_changes     – True when at least one new or updated dataset was found
    """
    number   = pr["number"]
    base_sha = pr["base"]["sha"]
    head_sha = pr["head"]["sha"]

    files = get_pr_files(number)
    if files is None:
        logger.warning("PR #%d: could not fetch file list, skipping", number)
        return [], [], False

    # Only _datasets/*.md files are relevant
    ds_files = [
        f for f in files
        if f["filename"].startswith(_DS_PREFIX) and f["filename"].endswith(".md")
    ]
    if not ds_files:
        return [], [], False

    added    = [f for f in ds_files if f["status"] == "added"]
    deleted  = [f for f in ds_files if f["status"] == "deleted"]
    modified = [f for f in ds_files if f["status"] == "modified"]

    # --- Rename detection ---
    # Exactly one added + one deleted with identical resources → rename, skip both.
    is_rename = False
    if len(added) == 1 and len(deleted) == 1:
        add_txt = get_file_at_ref(added[0]["filename"], head_sha)
        del_txt = get_file_at_ref(deleted[0]["filename"], base_sha)
        if add_txt and del_txt:
            add_post = _safe_frontmatter(add_txt, added[0]["filename"])
            del_post = _safe_frontmatter(del_txt, deleted[0]["filename"])
            if add_post is not None and del_post is not None:
                if add_post.get("resources") == del_post.get("resources"):
                    is_rename = True

    # --- New datasets ---
    new_datasets = []
    if not is_rename:
        for f in added:
            content = get_file_at_ref(f["filename"], head_sha)
            if content is None:
                logger.warning("PR #%d: cannot read %s at head SHA", number, f["filename"])
                continue
            post = _safe_frontmatter(content, f["filename"])
            if post is None:
                continue
            title = post.get("title") or os.path.basename(f["filename"]).removesuffix(".md")
            new_datasets.append({"title": title, "url": _dataset_url(f["filename"])})

    # --- Updated datasets ---
    updated_datasets = []
    for f in modified:
        before_txt = get_file_at_ref(f["filename"], base_sha)
        after_txt  = get_file_at_ref(f["filename"], head_sha)
        if before_txt is None or after_txt is None:
            logger.warning("PR #%d: cannot read before/after for %s", number, f["filename"])
            continue
        before = _safe_frontmatter(before_txt, f["filename"] + ":before")
        after  = _safe_frontmatter(after_txt,  f["filename"] + ":after")
        if before is None or after is None:
            continue

        title = after.get("title") or os.path.basename(f["filename"]).removesuffix(".md")
        url   = _dataset_url(f["filename"])
        reason = None

        # Rule 1: resources block changed
        before_res = before.get("resources") or []
        after_res  = after.get("resources")  or []
        if before_res != after_res:
            reason = (
                f"Resources block modified "
                f"({len(before_res)} → {len(after_res)} resources)"
            )

        # Rule 2: description changed by more than threshold
        if reason is None:
            ratio = _desc_change_ratio(before.get("description"), after.get("description"))
            if ratio > _DESC_THRESHOLD:
                pct = int(round(ratio * 100))
                thr = int(_DESC_THRESHOLD * 100)
                reason = f"Description changed by {pct}% (exceeds {thr}% threshold)"

        if reason:
            entry = {"title": title, "url": url}
            # if TEST_MODE:
            #     entry["reason"] = reason
            updated_datasets.append(entry)

    had_changes = bool(new_datasets or updated_datasets)
    return new_datasets, updated_datasets, had_changes


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def check_urls(datasets):
    """GET each dataset URL and set 'url_404': True on any that return 404."""
    print(f"Checking {len(datasets)} URL(s)...", flush=True)
    for d in datasets:
        try:
            r = requests.get(d["url"], timeout=15, allow_redirects=True)
            if r.status_code == 404:
                d["url_404"] = True
                print(f"  404: {d['url']}", flush=True)
        except requests.RequestException as exc:
            logger.warning("URL check failed for %s: %s", d["url"], exc)


def _fmt_line(d):
    line = f"- _{d['title']}_ - {d['url']}"
    if d.get("url_404"):
        line += " **404 ERROR**"
    return line


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def build_output(new_ds, updated_ds, stats):
    lines = []

    # Email-ready section
    lines += ["**New Datasets**", ""]
    if new_ds:
        for d in new_ds:
            lines.append(_fmt_line(d))
    else:
        lines.append("_(none)_")

    lines += ["", "**Updated Datasets**", ""]
    if updated_ds:
        for d in updated_ds:
            line = _fmt_line(d)
            # if TEST_MODE and "reason" in d:
            #     line += f" ({d['reason']})"
            lines.append(line)
    else:
        lines.append("_(none)_")

    # Stats section
    lines += ["", "---", "", "## Run Statistics", ""]
    lines.append(f"- Date range: {stats['start_date']} to {stats['end_date']}")
    lines.append(f"- PRs examined: {stats['prs_examined']}")
    lines.append(f"- PRs merged in range: {stats['prs_merged_in_range']}")
    lines.append(f"- PRs skipped (no substantive dataset changes): {stats['prs_skipped']}")
    lines.append(f"- New datasets: {stats['new_datasets']}")
    lines.append(f"- Updated datasets: {stats['updated_datasets']}")
    lines.append(f"- Run timestamp: {stats['run_timestamp']}")
    lines.append(f"- Test mode: {'Yes' if TEST_MODE else 'No'}")
    if TEST_MODE:
        lines.append(f"- PRs processed (test mode): {stats['prs_processed']}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    start_date = _parse_config_date(START_DATE)
    end_date   = _parse_config_date(END_DATE)
    run_ts     = datetime.datetime.now()

    os.makedirs(LOG_DIR, exist_ok=True)

    start_str = start_date.strftime("%Y-%m-%d")
    end_str   = end_date.strftime("%Y-%m-%d")
    fname     = f"{LOG_BASE_NAME}-{start_str}-to-{end_str}-run-{run_ts.strftime('%Y%m%d')}.md"
    out_path  = os.path.join(LOG_DIR, fname)

    # Fetch all closed PRs, then filter to those merged within the date range
    all_closed = fetch_closed_prs()
    merged_in_range = sorted(
        [
            p for p in all_closed
            if p.get("merged_at") and start_date <= _gh_date(p["merged_at"]) <= end_date
        ],
        key=lambda p: p["merged_at"],
        reverse=True,  # newest first
    )

    print(f"Total closed PRs fetched: {len(all_closed)}")
    print(f"PRs merged in range [{start_str} – {end_str}]: {len(merged_in_range)}")

    to_process = merged_in_range[:TEST_SIZE] if TEST_MODE else merged_in_range
    if TEST_MODE:
        print(f"TEST_MODE: processing {len(to_process)} of {len(merged_in_range)} merged PRs")

    all_new, all_updated = [], []
    prs_skipped = 0

    for i, pr in enumerate(to_process, 1):
        print(f"  PR #{pr['number']} ({i}/{len(to_process)})...", flush=True)
        new_ds, upd_ds, had_changes = process_pr(pr)
        if not had_changes:
            prs_skipped += 1
        all_new.extend(new_ds)
        all_updated.extend(upd_ds)

    check_urls(all_new + all_updated)

    stats = {
        "start_date":          start_str,
        "end_date":            end_str,
        "prs_examined":        len(all_closed),
        "prs_merged_in_range": len(merged_in_range),
        "prs_skipped":         prs_skipped,
        "new_datasets":        len(all_new),
        "updated_datasets":    len(all_updated),
        "run_timestamp":       run_ts.strftime("%Y-%m-%d %H:%M:%S"),
        "prs_processed":       len(to_process),
    }

    output = build_output(all_new, all_updated, stats)

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(output)

    print(f"\nOutput written to: {out_path}")
    print(f"New datasets: {len(all_new)}  |  Updated datasets: {len(all_updated)}")


if __name__ == "__main__":
    main()
