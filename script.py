#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

KEYWORDS_IMPORTANT = [
    "readme",
    "main",
    "index",
    "app",
    "config",
    "api",
    "datafeed",
    "core",
    "client",
]

KEYWORDS_PY_DATA_ALGO = [
    "data",
    "dataset",
    "etl",
    "transform",
    "processing",
    "preprocess",
    "parser",
    "algorithm",
    "algo",
    "strategy",
    "indicator",
    "signal",
    "feature",
    "pipeline",
    "loader",
    "fetch",
    "stream",
    "analytics",
    "model",
]

CONTENT_HINTS = [
    "pandas",
    "numpy",
    "sklearn",
    "DataFrame",
    "read_csv",
    "rolling",
    "indicator",
]

MAX_CONTENT_BYTES = 200 * 1024
GITHUB_API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"


@dataclass
class RepoRef:
    owner: str
    repo: str


def parse_repo_url(repo_url: str) -> RepoRef:
    parsed = urlparse(repo_url.strip())
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError(f"Unsupported host: {parsed.netloc}")

    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("Repository URL must be in format: https://github.com/{owner}/{repo}")

    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        raise ValueError("Could not parse owner/repo from URL")
    return RepoRef(owner=owner, repo=repo)


def _request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: int = 20,
    retries: int = 3,
    **kwargs: Any,
) -> Optional[requests.Response]:
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            resp = session.request(method=method, url=url, timeout=timeout, **kwargs)

            if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
                reset_raw = resp.headers.get("X-RateLimit-Reset")
                wait_seconds = 10
                if reset_raw and reset_raw.isdigit():
                    wait_seconds = max(1, int(reset_raw) - int(time.time()))
                wait_seconds = min(wait_seconds, 60)
                print(f"[warn] Rate limit hit, waiting {wait_seconds}s before retry...")
                time.sleep(wait_seconds)
                continue

            if resp.status_code in {429, 500, 502, 503, 504}:
                if attempt < retries:
                    backoff = 2 ** (attempt - 1)
                    print(f"[warn] HTTP {resp.status_code} from {url}, retrying in {backoff}s...")
                    time.sleep(backoff)
                    continue
            return resp
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                backoff = 2 ** (attempt - 1)
                print(f"[warn] Network error on attempt {attempt}/{retries}: {exc}; retrying in {backoff}s...")
                time.sleep(backoff)
            else:
                break

    if last_error:
        print(f"[error] Request failed after retries: {last_error}")
    return None


def detect_default_branch(session: requests.Session, owner: str, repo: str) -> Optional[str]:
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
    print("[info] Detecting default branch...")
    resp = _request_with_retry(session, "GET", url)

    if resp is not None and resp.ok:
        data = resp.json()
        branch = data.get("default_branch")
        if isinstance(branch, str) and branch:
            return branch

    for candidate in ("main", "master"):
        branch_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/branches/{candidate}"
        branch_resp = _request_with_retry(session, "GET", branch_url)
        if branch_resp is not None and branch_resp.ok:
            print(f"[warn] Falling back to branch: {candidate}")
            return candidate

    return None


def _fetch_tree_api(session: requests.Session, owner: str, repo: str, branch: str) -> Optional[List[Dict[str, Any]]]:
    tree_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    resp = _request_with_retry(session, "GET", tree_url)
    if resp is None or not resp.ok:
        return None

    payload = resp.json()
    tree = payload.get("tree", [])
    if not isinstance(tree, list):
        return None

    files: List[Dict[str, Any]] = []
    for node in tree:
        if node.get("type") == "blob":
            path = node.get("path")
            if isinstance(path, str) and path:
                files.append(
                    {
                        "path": path,
                        "size": int(node.get("size", 0) or 0),
                        "sha": node.get("sha"),
                    }
                )
    return files


def _fetch_contents_api(session: requests.Session, owner: str, repo: str, branch: str) -> Optional[List[Dict[str, Any]]]:
    print("[warn] Falling back to Contents API traversal...")
    root_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents"
    stack: List[str] = [""]
    files: List[Dict[str, Any]] = []

    while stack:
        current = stack.pop()
        url = root_url if not current else f"{root_url}/{current}"
        resp = _request_with_retry(session, "GET", url, params={"ref": branch})
        if resp is None or not resp.ok:
            continue

        payload = resp.json()
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            continue

        for item in payload:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            item_path = item.get("path")
            if not isinstance(item_path, str) or not item_path:
                continue

            if item_type == "dir":
                stack.append(item_path)
            elif item_type == "file":
                files.append(
                    {
                        "path": item_path,
                        "size": int(item.get("size", 0) or 0),
                        "sha": item.get("sha"),
                    }
                )

    return files


def fetch_repo_tree(session: requests.Session, owner: str, repo: str, branch: str) -> List[Dict[str, Any]]:
    print("[info] Fetching repository tree...")
    files = _fetch_tree_api(session, owner, repo, branch)
    if files is not None:
        return files

    contents_files = _fetch_contents_api(session, owner, repo, branch)
    return contents_files or []


def build_raw_url(owner: str, repo: str, branch: str, path: str) -> str:
    return f"{RAW_BASE}/{owner}/{repo}/{branch}/{path}"


def _file_basics(path: str) -> Tuple[str, str, int]:
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    depth = max(1, len(Path(path).parts))
    return filename, ext, depth


def score_file(path: str, size: int = 0, content_text: Optional[str] = None) -> Tuple[float, List[str]]:
    path_l = path.lower()
    filename, ext, depth = _file_basics(path)
    filename_l = filename.lower()

    score = 0.0
    reasons: List[str] = []

    for kw in KEYWORDS_IMPORTANT:
        if kw in filename_l:
            score += 0.28
            reasons.append(f"filename keyword:{kw}")
        elif f"/{kw}" in path_l or f"_{kw}" in path_l or f"-{kw}" in path_l:
            score += 0.12
            reasons.append(f"path keyword:{kw}")

    if ext in {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".ini"}:
        score += 0.12
        reasons.append(f"useful extension:{ext}")

    if ext == ".py":
        score += 0.26
        reasons.append("python file")

    if depth <= 2:
        score += 0.10
        reasons.append(f"shallow path depth:{depth}")
    elif depth <= 5:
        score += 0.05

    for kw in KEYWORDS_PY_DATA_ALGO:
        if kw in path_l:
            score += 0.08
            reasons.append(f"semantic keyword:{kw}")
            break

    if content_text:
        lower_text = content_text.lower()
        matched = [hint for hint in CONTENT_HINTS if hint.lower() in lower_text]
        if matched:
            score += min(0.24, 0.06 * len(matched))
            reasons.append("content hints:" + ",".join(matched[:4]))

    if size > 0 and size <= 4096:
        score += 0.03

    score = min(1.0, max(0.0, score))
    if not reasons:
        reasons.append("baseline")
    return score, reasons


def maybe_fetch_content(
    session: requests.Session,
    owner: str,
    repo: str,
    branch: str,
    path: str,
    size: int,
) -> Optional[str]:
    if size <= 0 or size > MAX_CONTENT_BYTES:
        return None

    raw_url = build_raw_url(owner, repo, branch, path)
    resp = _request_with_retry(session, "GET", raw_url)
    if resp is None or not resp.ok:
        return None

    ctype = resp.headers.get("Content-Type", "")
    if "text" not in ctype and "json" not in ctype and "python" not in ctype:
        return None

    try:
        return resp.text
    except Exception:
        return None


def classify_file(
    file_info: Dict[str, Any],
    owner: str,
    repo: str,
    branch: str,
    content_text: Optional[str] = None,
) -> Dict[str, Any]:
    path = str(file_info.get("path", ""))
    size = int(file_info.get("size", 0) or 0)
    filename, ext, _ = _file_basics(path)

    score, reasons = score_file(path=path, size=size, content_text=content_text)
    path_l = path.lower()

    is_python = ext == ".py"
    py_data_algo_match = False
    if is_python:
        py_data_algo_match = any(kw in path_l for kw in KEYWORDS_PY_DATA_ALGO)
        if not py_data_algo_match and content_text:
            content_l = content_text.lower()
            py_data_algo_match = any(kw in content_l for kw in KEYWORDS_PY_DATA_ALGO + CONTENT_HINTS)

    if is_python and py_data_algo_match:
        category = "python_data_algo"
    elif is_python:
        category = "python"
    elif score >= 0.45:
        category = "important"
    else:
        category = "other"

    raw_url = build_raw_url(owner, repo, branch, path)
    html_url = f"https://github.com/{owner}/{repo}/blob/{branch}/{path}"

    return {
        "path": path,
        "filename": filename,
        "extension": ext,
        "importance_score": round(score, 4),
        "category": category,
        "raw_url": raw_url,
        "html_url": html_url,
        "reason": "; ".join(dict.fromkeys(reasons)),
    }


def export_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def export_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows_list = list(rows)
    with path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "path",
            "filename",
            "extension",
            "importance_score",
            "category",
            "raw_url",
            "html_url",
            "reason",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows_list:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def _build_session() -> requests.Session:
    session = requests.Session()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "repo-analyzer/1.0",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    session.headers.update(headers)
    return session


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze GitHub repository files and export RAW URLs")
    parser.add_argument("--repo", required=True, help="GitHub repository URL")
    args = parser.parse_args()

    try:
        repo_ref = parse_repo_url(args.repo)
    except ValueError as exc:
        print(f"[error] Invalid repository URL: {exc}")
        return 2

    session = _build_session()
    owner, repo = repo_ref.owner, repo_ref.repo

    try:
        branch = detect_default_branch(session, owner, repo)
        if not branch:
            print("[error] Could not determine default branch.")
            return 3

        files = fetch_repo_tree(session, owner, repo, branch)
        if not files:
            print("[warn] Repository tree is empty or inaccessible.")

        print(f"[info] Found {len(files)} file(s). Classifying...")
        classified: List[Dict[str, Any]] = []

        for idx, file_info in enumerate(files, start=1):
            path = str(file_info.get("path", ""))
            size = int(file_info.get("size", 0) or 0)
            ext = os.path.splitext(path)[1].lower()

            content_text = None
            if ext == ".py" and size < MAX_CONTENT_BYTES:
                content_text = maybe_fetch_content(session, owner, repo, branch, path, size)

            item = classify_file(file_info, owner, repo, branch, content_text=content_text)
            classified.append(item)

            if idx % 250 == 0:
                print(f"[info] Processed {idx}/{len(files)} files...")

        important_files = [f for f in classified if f["category"] == "important"]
        python_files = [f for f in classified if f["category"] in {"python", "python_data_algo"}]
        python_data_algo_files = [f for f in classified if f["category"] == "python_data_algo"]

        output = {
            "repo_metadata": {
                "repo_url": args.repo,
                "owner": owner,
                "repo": repo,
                "default_branch": branch,
                "total_files": len(classified),
                "generated_at_unix": int(time.time()),
            },
            "important_files": important_files,
            "important_raw_urls": [f["raw_url"] for f in important_files],
            "python_files": python_files,
            "python_raw_urls": [f["raw_url"] for f in python_files],
            "python_data_algo_files": python_data_algo_files,
            "python_data_algo_raw_urls": [f["raw_url"] for f in python_data_algo_files],
        }

        out_dir = Path("output")
        export_json(out_dir / "important_raw_urls.json", output["important_raw_urls"])
        export_csv(out_dir / "important_raw_urls.csv", important_files)
        export_json(out_dir / "python_raw_urls.json", output["python_raw_urls"])
        export_json(out_dir / "python_data_algo_raw_urls.json", output["python_data_algo_raw_urls"])

        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    except KeyboardInterrupt:
        print("[error] Interrupted by user")
        return 130
    except Exception as exc:
        print(f"[error] Unexpected failure: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
