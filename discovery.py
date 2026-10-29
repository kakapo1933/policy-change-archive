"""Bounded, same-origin page discovery plus per-policy logical records.

Only GET publicly reachable pages. An inventory is a list of *observed* public
resources, not a claim that every browser view or private URL was captured.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
import hashlib
import json
import posixpath
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit, unquote
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

BLOCKED_PATH = re.compile(r"(?:^|/)(?:admin|login|logout|signin|signup|account|settings|api/private|auth|checkout|payment)(?:/|$)", re.I)
ACCEPTED_FILES = {"", ".html", ".htm", ".json", ".xml", ".js", ".mjs", ".css", ".txt"}
SKIP_REPO = (".github/", "node_modules/", "tests/", "test/", "docs/", "scripts/", "vendor/", "assets/", "dist/", "build/", "coverage/", "admin/", "src/admin/", "public/fonts/")
REPO_SUFFIXES = (".html", ".htm", ".json", ".js", ".mjs", ".css", ".ts", ".tsx", ".jsx")


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag in {"a", "area"} and attr.get("href"):
            self.urls.append(attr["href"])
        elif tag == "script" and attr.get("src"):
            self.urls.append(attr["src"])
        elif tag == "link" and attr.get("href") and any(v in (attr.get("rel") or "").lower() for v in ("stylesheet", "canonical", "alternate")):
            self.urls.append(attr["href"])


def canonical(url: str, allowed_host: str | None = None, prefixes: list[str] | None = None) -> str | None:
    """Reject query variants, non-public schemes, cross-origin and unsafe routes."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        return None
    try:
        p = urlsplit(url)
        if p.scheme.lower() != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
            return None
        host = p.hostname.lower()
        if allowed_host and host != allowed_host.lower():
            return None
        if p.query:
            return None  # unbounded query permutations and destructive GET endpoints
        path = re.sub(r"/{2,}", "/", p.path or "/")
        if "%2f" in path.lower() or "%5c" in path.lower() or "\\" in path:
            return None
        path = quote(unquote(path), safe="/%-._~")
        if any(seg == ".." for seg in unquote(path).split("/")):
            return None
        if BLOCKED_PATH.search(unquote(path)):
            return None
        ext = posixpath.splitext(path)[1].lower()
        if ext not in ACCEPTED_FILES:
            return None
        if prefixes and not any(path == pre.rstrip("/") or path.startswith(pre) for pre in prefixes):
            return None
        return urlunsplit(("https", host, path, "", ""))
    except (ValueError, UnicodeError):
        return None


def source_id(url: str) -> str:
    return "page-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def body_from_json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def entries_from_json(data, dataset: str):
    """Domain-aware extraction; each entry is one trackable policy, not an HTML route."""
    if dataset == "heart-policies" and isinstance(data, dict):
        for island in data.get("islands", []):
            if isinstance(island, dict):
                for item in island.get("buildings", []):
                    if isinstance(item, dict):
                        yield item.get("id"), item, item.get("name") or item.get("title")
    elif dataset == "policy-island-policies" and isinstance(data, dict):
        for theme in data.get("themes", []):
            if isinstance(theme, dict):
                for item in theme.get("items", []):
                    if isinstance(item, dict):
                        yield item.get("id"), item, item.get("title")
    else:
        items = data.get("items", []) if isinstance(data, dict) else data
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, dict):
                    title = item.get("title") or item.get("pledge") or item.get("short") or item.get("name") or ""
                    stable = item.get("id") or ("title-" + hashlib.sha256(str(title).encode()).hexdigest()[:20] if title else f"index-{index}")
                    yield stable, item, title


@dataclass
class Result:
    sources: list[dict] = field(default_factory=list)
    virtual_blobs: dict[str, bytes] = field(default_factory=dict)
    responses: dict[str, tuple[bytes, int, dict]] = field(default_factory=dict)
    missing_virtual: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)


def discover(site: dict, getter, previous_history: list[dict], sleep=time.sleep) -> Result:
    """Collect concrete URLs and JSON records, with auditable partial-coverage reasons."""
    cfg = site.get("crawl")
    result = Result()
    if not cfg:
        return result
    notes = []
    errors = []
    candidates: dict[str, dict] = {}
    existing_urls = {s["url"] for s in site.get("sources", [])}
    site_id = site["id"]
    max_pages = max(1, min(int(cfg.get("max_pages", 100)), 250))
    max_repo_files = max(1, min(int(cfg.get("max_repo_files", 140)), 250))
    delay = max(0, min(float(cfg.get("delay_seconds", 0.15)), 3.0))
    origin = urlsplit(site["homepage"])
    host = origin.hostname
    prefixes = cfg.get("allowed_paths") or ["/"]
    hit_cap = False
    seen = set()
    robots = None
    sitemap_urls = set()

    def read(url, cache=True):
        if url in result.responses:
            return result.responses[url]
        fetched = getter(url)
        if cache:
            result.responses[url] = fetched
        return fetched

    def add(url, reason, kind=None):
        normal = canonical(url, host, prefixes)
        if not normal or normal in candidates or normal in existing_urls:
            return normal
        # Discovery must also honor the same robots rules as its crawler.
        if cfg.get("discover_web") and (robots is False or (robots is not None and not robots.can_fetch("PolicyWatch", normal))):
            notes.append(f"not scheduled (robots): {normal}")
            return None
        typ = kind or ("json" if normal.endswith(".json") else "html" if not posixpath.splitext(urlsplit(normal).path)[1] or normal.endswith((".htm", ".html")) else "asset")
        candidates[normal] = {"id": source_id(normal), "name": unquote(urlsplit(normal).path)[:120], "url": normal, "type": typ, "discovered_by": reason}
        return normal

    if cfg.get("discover_web", False):
        # Never silently bypass an explicit robots.txt disallow; 404 means no rules.
        try:
            robots_url = f"https://{host}/robots.txt"
            body, status, _ = read(robots_url)
            if status != 200:
                raise ValueError(f"robots HTTP {status}")
            robots = RobotFileParser()
            rules = body.decode("utf-8-sig", errors="replace").splitlines()
            robots.parse(rules)
            for line in rules:
                if line.lower().startswith("sitemap:"):
                    sitemap_urls.add(line.split(":", 1)[1].strip())
        except HTTPError as exc:
            if exc.code != 404:
                errors.append(f"robots.txt HTTP {exc.code}; halted web crawl")
                robots = False
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"robots.txt unavailable ({type(exc).__name__}); halted web crawl")
            robots = False
        for url in cfg.get("sitemaps", []):
            sitemap_urls.add(url)
        sitemap_queue = deque((url, 0) for url in sorted(sitemap_urls))
        sitemap_seen = set()
        sitemap_count = 0
        queued = deque()
        def enqueue(url, reason, depth=0):
            u = canonical(url, host, prefixes)
            if u is not None and u not in seen and u not in {pair[0] for pair in queued}:
                queued.append((u, depth, reason))
        for url in [site["homepage"], *cfg.get("seed_urls", [])]:
            enqueue(url, "seed")
        while sitemap_queue and sitemap_count < 5 and robots is not False:
            url, depth = sitemap_queue.popleft()
            p = urlsplit(url)
            if p.scheme != "https" or p.hostname != host or p.query or url in sitemap_seen:
                continue
            sitemap_seen.add(url)
            sitemap_count += 1
            try:
                blob, status, _ = read(url, cache=False)
                if status != 200:
                    raise ValueError(f"HTTP {status}")
                root = ET.fromstring(blob)
                root_tag = root.tag.split("}")[-1]
                locs = [x.text.strip() for x in root.iter() if x.tag.split("}")[-1] == "loc" and x.text]
                if root_tag == "sitemapindex" and depth < 2:
                    sitemap_queue.extend((x, depth + 1) for x in locs[:10])
                elif root_tag == "urlset":
                    for x in locs[:max_pages * 3]:
                        enqueue(x, "sitemap")
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, ET.ParseError) as exc:
                # Many sites simply don't have a sitemap; retain that uncertainty.
                if not isinstance(exc, HTTPError) or exc.code != 404:
                    notes.append(f"sitemap unavailable: {url}: {type(exc).__name__}")
        while queued and robots is not False:
            if len(seen) >= max_pages:
                hit_cap = True
                break
            url, depth, reason = queued.popleft()
            if url in seen:
                continue
            if robots is not None and not robots.can_fetch("PolicyWatch", url):
                notes.append(f"robots disallowed: {url}")
                continue
            seen.add(url)
            add(url, reason)
            try:
                blob, status, headers = read(url)
                if 200 <= status < 300 and depth < int(cfg.get("max_depth", 3)) and "html" in headers.get("content-type", "text/html"):
                    parser = Links()
                    parser.feed(blob.decode("utf-8-sig", errors="replace"))
                    for href in parser.urls:
                        enqueue(urljoin(url, href), f"html:{url}", depth + 1)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                # Will be captured as a failed per-page observation later.
                errors.append(f"page discovery fetch failed: {url} ({type(exc).__name__})")
            if delay:
                sleep(delay)
        if robots is False:
            notes.append("Web discovery halted because robots.txt could not be verified")

    # Discover standalone policy pages and logical (SPA) policy records from JSON.
    derived_current = set()
    complete_datasets = set()
    for dataset in cfg.get("policy_datasets", []):
        parent = next((x for x in site["sources"] if x["id"] == dataset.get("source_id")), None)
        if not parent:
            errors.append(f"dataset source missing: {dataset.get('source_id')}")
            continue
        try:
            blob, status, _ = read(parent["url"])
            if status != 200:
                raise ValueError(f"HTTP {status}")
            parsed = json.loads(blob)
            if dataset.get("policy_urls") and isinstance(parsed, dict):
                for row in parsed.get("sources", []):
                    if isinstance(row, dict) and isinstance(row.get("url"), str):
                        add(row["url"], "official-policy-index", "html")
            entries = list(entries_from_json(parsed, dataset["kind"]))
            duplicate_ids = {}
            for record_id, record, title in entries[:1000]:
                if not record_id:
                    continue
                raw_id = str(record_id)
                duplicate_ids[raw_id] = duplicate_ids.get(raw_id, 0) + 1
                record_id = raw_id if duplicate_ids[raw_id] == 1 else f"{raw_id}--{duplicate_ids[raw_id]}"
                logical = f"{parent['url']}#record={quote(str(record_id), safe='')}"
                sid = "item-" + hashlib.sha256(f"{parent['id']}:{record_id}".encode()).hexdigest()[:20]
                if isinstance(title, dict):
                    display_title = title.get("zh") or title.get("zh-TW") or title.get("en") or record_id
                else:
                    display_title = title or record_id
                source = {"id": sid, "url": logical, "name": str(display_title)[:140], "type": "json", "discovered_by": f"record:{parent['id']}", "logical": True}
                result.sources.append(source)
                result.virtual_blobs[sid] = body_from_json(record)
                derived_current.add(sid)
            if len(entries) > 1000:
                hit_cap = True
                notes.append(f"logical record cap: {parent['id']}")
            complete_datasets.add(parent["id"])
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"policy dataset {parent['id']} unavailable: {type(exc).__name__}: {exc}")

    # Discover text website resources directly from an independently published source repo.
    if cfg.get("repository_tree"):
        url = cfg["repository_tree"]
        try:
            blob, status, _ = read(url, cache=False)
            if status != 200:
                raise ValueError(f"HTTP {status}")
            data = json.loads(blob)
            if data.get("truncated"):
                hit_cap = True
                notes.append("GitHub repository tree was truncated")
            accepted = [x for x in data.get("tree", []) if x.get("type") == "blob" and x.get("size", 0) <= 5_000_000
                        and x.get("path", "").lower().endswith(REPO_SUFFIXES)
                        and not x.get("path", "").startswith(SKIP_REPO)
                        and x.get("path", "").split("/")[-1] not in {"package.json", "package-lock.json", "components.json", ".oxlintrc.json"}
                        and not x.get("path", "").split("/")[-1].startswith("tsconfig")]
            if len(accepted) > max_repo_files:
                hit_cap = True
                notes.append(f"repository file cap: {len(accepted)} > {max_repo_files}")
            for entry in accepted[:max_repo_files]:
                file_url = cfg["repository_raw_base"].rstrip("/") + "/" + quote(entry["path"], safe="/")
                # Repo is an explicitly configured separate origin, not a crawled link.
                typ = "json" if entry["path"].endswith(".json") else "html" if entry["path"].endswith((".html", ".htm")) else "asset"
                if file_url not in existing_urls:
                    result.sources.append({"id": source_id(file_url), "url": file_url, "name": entry["path"], "type": typ, "discovered_by": "repository-tree"})
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"GitHub source tree unavailable: {type(exc).__name__}: {exc}")

    result.sources.extend(candidates.values())
    # Follow previously recorded physical pages even if no longer linked. A 404/410
    # on this actual URL is stronger evidence than absence from a sitemap.
    previously_seen = {}
    for rec in previous_history:
        if rec.get("site_id") != site_id or not str(rec.get("source_id", "")).startswith("page-"):
            continue
        previously_seen[rec["source_id"]] = rec
    current_ids = {x["id"] for x in result.sources}
    for sid, rec in previously_seen.items():
        url = rec.get("url", "")
        if sid not in current_ids and len(result.sources) < max_pages + max_repo_files + 1100:
            permitted = (canonical(url, host, prefixes) if robots is not False else None) or (
                cfg.get("repository_raw_base") and url.startswith(cfg["repository_raw_base"].rstrip("/") + "/")
            )
            if permitted and (robots is None or robots is False or not canonical(url, host, prefixes) or robots.can_fetch("PolicyWatch", url)):
                result.sources.append({"id": sid, "url": url, "name": rec.get("name") or unquote(urlsplit(url).path), "type": rec.get("kind") or "html", "discovered_by": "previously-observed", "listed": False})

    for rec in previous_history:
        sid = rec.get("source_id", "")
        if rec.get("site_id") != site_id or not sid.startswith("item-") or sid in derived_current:
            continue
        parent_id = str(rec.get("discovered_by", "")).removeprefix("record:")
        if parent_id in complete_datasets and sid not in {x["id"] for x in result.missing_virtual}:
            result.missing_virtual.append({"id": sid, "url": rec["url"], "name": rec.get("name", sid), "type": "json", "discovered_by": f"record:{parent_id}", "logical": True})

    unique = {}
    for item in result.sources:
        unique.setdefault(item["id"], item)
    result.sources = list(unique.values())
    result.coverage = {"site_id": site_id, "discovered_pages": len(candidates), "logical_records": len(derived_current),
                       "repository_files": sum(x.get("discovered_by") == "repository-tree" for x in result.sources),
                       "tracked_resources": len(result.sources) + len(site["sources"]),
                       "complete": not hit_cap and not errors and robots is not False,
                       "capped": hit_cap, "errors": errors[:15], "notes": notes[:15],
                       "scope": "Public HTML, discovered text resources and per-policy JSON records; JavaScript runtime states and binary assets not fully covered"}
    return result
