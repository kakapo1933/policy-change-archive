#!/usr/bin/env python3
"""Daily public website evidence collector. Python stdlib only.

Preserves one record per target per run, including errors. History is not rewritten.
Git commits and UTC timestamps independently help verify the observation trail.
"""
from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from discovery import discover

BASE = Path(__file__).resolve().parent
ROOT = BASE / "data"
CONFIG = BASE / "targets.json"
MAX_BYTES = 5 * 1024 * 1024
USER_AGENT = "PolicyWatch/1.0 (+public archival monitor; one request per target per day)"


class TextExtractor(HTMLParser):
    BLOCKS = {"article", "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "section", "header", "footer", "main", "div", "br", "tr", "td"}
    IGNORE = {"script", "style", "svg", "noscript", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.IGNORE:
            self.hidden += 1
        elif not self.hidden and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.IGNORE:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def normalized_text(blob: bytes, kind: str, content_type: str = "") -> str:
    raw = blob.decode("utf-8-sig", errors="replace")
    if kind == "json":
        try:
            raw = json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass
    elif kind == "html":
        parser = TextExtractor()
        parser.feed(raw)
        raw = html.unescape("".join(parser.parts))
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def policy_text(blob: bytes) -> str:
    """Extract human-facing policy claims, excluding URLs, metadata and source IDs.

    This produces a heuristic textual comparison aid, not plagiarism evidence.
    """
    try:
        source = json.loads(blob.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        return ""
    excluded = {"url", "uri", "href", "link", "links", "source", "sources",
                "id", "slug", "key", "image", "icon", "logo", "color",
                "created", "updated", "checkedat", "verifiedat", "published",
                "status", "confidence", "publisher", "schemaVersion"}
    items = []

    def visit(item, key=""):
        if key.casefold() in excluded:
            return
        if isinstance(item, dict):
            for k, v in item.items():
                visit(v, k)
        elif isinstance(item, list):
            for v in item:
                visit(v, key)
        elif isinstance(item, str):
            value = re.sub(r"\s+", " ", item).strip()
            if len(value) >= 4 and re.search(r"[\u3400-\u9fff]", value) and not value.startswith(("http://", "https://")):
                items.append(value)

    visit(source)
    return "\n".join(items)


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if new.scheme != "https" or old.hostname != new.hostname or new.username or new.password:
            raise ValueError(f"cross-origin or insecure redirect blocked: {old.hostname} -> {new.hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(SafeRedirect())


def fetch(url: str) -> tuple[bytes, int, dict]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"})
    with _OPENER.open(req, timeout=25) as res:
        status = res.status
        headers = {k.lower(): v for k, v in res.headers.items() if k.lower() in {"content-type", "last-modified", "etag"}}
        blob = res.read(MAX_BYTES + 1)
        if len(blob) > MAX_BYTES:
            raise ValueError(f"response exceeds {MAX_BYTES} bytes")
        if not blob:
            raise ValueError("empty HTTP body")
        return blob, status, headers


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path, fallback):
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def short_diff(before: str, after: str) -> list[str]:
    return list(difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile="previous", tofile="current", lineterm="", n=2))[:200]


def trigrams(value: str) -> set[str]:
    val = re.sub(r"\s+", "", value)
    return {val[i:i+3] for i in range(max(len(val)-2, 0))}


def similarity(a: str, b: str) -> float | None:
    x, y = trigrams(a[:150000]), trigrams(b[:150000])
    if len(x) < 12 or len(y) < 12:
        return None
    return round(100 * len(x & y) / len(x | y), 2)


def run(now: datetime | None = None, getter=fetch):
    now = now or datetime.now(timezone.utc)
    timestamp = now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    run_id = timestamp.replace(":", "-")
    targets = read_json(CONFIG, {}).get("sites", [])
    history_path = ROOT / "history.json"
    history = read_json(history_path, [])
    records = []
    coverage = {}
    for site in targets:
        discovered = discover(site, getter, history)
        coverage[site["id"]] = discovered.coverage or {"site_id": site["id"], "complete": True, "tracked_resources": len(site["sources"]), "notes": [], "errors": []}
        all_sources = list(site["sources"]) + discovered.sources
        for source in all_sources:
            prev = next((r for r in reversed(history + records) if r["site_id"] == site["id"] and r["source_id"] == source["id"] and r["status"] == "ok"), None)
            rec = {"name": source.get("name", source["id"]), "kind": source.get("type", "html"), "discovered_by": source.get("discovered_by", "configured"), "listed": source.get("listed", True), "logical": source.get("logical", False), "observed_at": timestamp, "run_id": run_id, "site_id": site["id"], "source_id": source["id"], "url": source["url"], "status": "error", "http_status": None, "raw_sha256": None, "text_sha256": None, "bytes": None, "change": "unknown", "previous_observed_at": prev["observed_at"] if prev else None, "previous_text_sha256": prev["text_sha256"] if prev else None, "error": None, "headers": {}, "snapshot": None, "text_snapshot": None, "comparison_text_snapshot": None, "comparison_text_sha256": None, "diff": []}
            try:
                if source.get("logical"):
                    blob, code, headers = discovered.virtual_blobs[source["id"]], 200, {"content-type": "application/json", "x-derived": "policy-item"}
                else:
                    if source.get("discovered_by") and source["url"] not in discovered.responses:
                        delay = site.get("crawl", {}).get("delay_seconds", 0.15)
                        time.sleep(max(0, min(float(delay), 2)))
                    blob, code, headers = discovered.responses.get(source["url"]) or getter(source["url"])
                if not 200 <= code < 300:
                    raise ValueError(f"HTTP {code}")
                text = normalized_text(blob, source["type"], headers.get("content-type", ""))
                raw_hash, text_hash = sha(blob), sha(text.encode("utf-8"))
                snap = ROOT / "snapshots" / f"{raw_hash}.gz"
                if not snap.exists():
                    snap.parent.mkdir(parents=True, exist_ok=True)
                    with open(snap, "wb") as sink:
                        # Reproducible gzip bytes avoid unnecessary Git churn.
                        sink.write(gzip.compress(blob, compresslevel=9, mtime=0))
                txt = ROOT / "texts" / f"{text_hash}.txt"
                if not txt.exists():
                    txt.parent.mkdir(parents=True, exist_ok=True)
                    txt.write_text(text, encoding="utf-8")
                comparison_path = None
                comparison_hash = None
                if source["type"] == "json":
                    claims = policy_text(blob)
                    if claims:
                        comparison_hash = sha(claims.encode("utf-8"))
                        comparison_path = ROOT / "texts" / f"{comparison_hash}.txt"
                        if not comparison_path.exists():
                            comparison_path.parent.mkdir(parents=True, exist_ok=True)
                            comparison_path.write_text(claims, encoding="utf-8")
                before = ""
                if prev and prev.get("text_snapshot"):
                    before_path = BASE / prev["text_snapshot"]
                    if before_path.exists():
                        before = before_path.read_text(encoding="utf-8")
                change = "initial" if not prev else "changed" if raw_hash != prev["raw_sha256"] else "unchanged"
                # A page can still return 200 even after all navigation links disappear.
                if source.get("listed") is False and prev and prev.get("listed") is not False:
                    change = "unlisted" if change == "unchanged" else change
                diff = short_diff(before, text) if prev and text_hash != prev["text_sha256"] and before else []
                rec.update(status="ok", http_status=code, raw_sha256=raw_hash, text_sha256=text_hash, bytes=len(blob), change=change, headers=headers, snapshot=snap.relative_to(BASE).as_posix(), text_snapshot=txt.relative_to(BASE).as_posix(), comparison_text_snapshot=comparison_path.relative_to(BASE).as_posix() if comparison_path else None, comparison_text_sha256=comparison_hash, diff=diff)
            except (HTTPError, URLError, TimeoutError, ValueError, OSError, UnicodeError) as exc:
                rec["error"] = f"{type(exc).__name__}: {exc}"[:350]
                if isinstance(exc, HTTPError):
                    rec["http_status"] = exc.code
                    if exc.code in (404, 410) and prev is not None:
                        rec["status"] = "gone"
                        rec["change"] = "removed" if not any(h.get("site_id") == site["id"] and h.get("source_id") == source["id"] and h.get("status") == "gone" for h in reversed(history)) else "still_gone"
            records.append(rec)
            print(f"{site['id']}/{source['id']}: {rec['status']} ({rec['change']})", flush=True)
        for missing in discovered.missing_virtual:
            # A missing logical policy is meaningful only after its parent JSON
            # was read successfully, and only once per disappearance.
            past = [r for r in history if r.get("site_id") == site["id"] and r.get("source_id") == missing["id"]]
            if not past or past[-1].get("status") == "gone":
                continue
            last = next((r for r in reversed(past) if r.get("status") == "ok"), None)
            if last is None:
                continue
            records.append({"name": missing["name"], "kind": "json", "discovered_by": missing["discovered_by"],
                            "listed": False, "logical": True, "observed_at": timestamp, "run_id": run_id,
                            "site_id": site["id"], "source_id": missing["id"], "url": missing["url"],
                            "status": "gone", "http_status": None, "raw_sha256": None,
                            "text_sha256": None, "bytes": None, "change": "removed", "previous_observed_at": last["observed_at"],
                            "previous_text_sha256": last["text_sha256"], "error": "Policy record absent from newly fetched parent JSON",
                            "headers": {}, "snapshot": None, "text_snapshot": None, "comparison_text_snapshot": None,
                            "comparison_text_sha256": None, "diff": []})
    # Append-only history: reject a replay with the same run ID rather than silently overwrite.
    if any(r["run_id"] == run_id for r in history):
        raise ValueError(f"run_id already exists: {run_id}")
    history.extend(records)
    save_json(history_path, history)
    save_json(ROOT / "runs" / f"{run_id}.json", {"run_id": run_id, "observed_at": timestamp, "records": records})

    latest = {}
    for site in targets:
        rlist = [r for r in history if r["site_id"] == site["id"]]
        latest_run = rlist[-1]["run_id"] if rlist else None
        latest_records = [r for r in rlist if r["run_id"] == latest_run]
        latest[site["id"]] = {"latest": latest_records, "total_observations": len(rlist),
                              "change_events": sum(r.get("change") in {"changed", "removed", "unlisted"} for r in rlist),
                              "resource_count": len(latest_records),
                              "latest_changed": sum(r.get("change") in {"changed", "removed", "unlisted"} for r in latest_records)}
    comparisons = []
    for i, first in enumerate(targets):
        for second in targets[i+1:]:
            def basis(site):
                combined = []
                selected = []
                for src in site.get("comparison_sources", ["homepage"]):
                    r = next((r for r in reversed(history) if r["site_id"] == site["id"] and r["source_id"] == src and r["status"] == "ok"), None)
                    if not r:
                        continue
                    chosen = r.get("comparison_text_snapshot") or r.get("text_snapshot")
                    if chosen and (BASE / chosen).exists():
                        combined.append((BASE / chosen).read_text(encoding="utf-8"))
                        selected.append(src)
                return "\n".join(combined), selected

            a_text, a_sources = basis(first)
            b_text, b_sources = basis(second)
            score = similarity(a_text, b_text) if a_sources and b_sources else None
            comparisons.append({"a": first["id"], "b": second["id"], "score": score, "sources_a": a_sources, "sources_b": b_sources})
    save_json(ROOT / "summary.json", {"generated_at": timestamp, "sites": targets, "stats": latest,
                                     "coverage": coverage, "comparisons": comparisons,
                                     "history": list(reversed(history[-2000:]))})
    return records


if __name__ == "__main__":
    try:
        records = run()
        if all(r["status"] != "ok" for r in records):
            print("All monitored sources failed; saved error records", file=sys.stderr)
            sys.exit(1)
    except Exception as err:
        print(f"monitor fatal: {err}", file=sys.stderr)
        sys.exit(2)
