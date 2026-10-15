"""Public reviewer leads with provenance, plus an explicitly collected contact index."""

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import AEError


def fetch(url):
    request = Request(url, headers={"User-Agent": "llm-ae/0.1 (editorial reviewer discovery)"})
    try:
        with urlopen(request, timeout=30) as response:
            data = response.read(5_000_001)
        if len(data) > 5_000_000:
            raise AEError("Discovery response exceeds the size limit")
        return data
    except HTTPError as exc:
        raise AEError(f"Discovery API returned HTTP {exc.code}; check credentials/rate limits") from exc
    except URLError as exc:
        raise AEError("Discovery API is unreachable") from exc


def public_results(workflow, source, query, limit):
    key = "discovery:" + json.dumps([source, query, limit])
    saved = workflow.store.one("SELECT value FROM settings WHERE key=?", (key,))
    if saved:
        cached = json.loads(saved["value"])
        if time.time() - cached["at"] < 86400:
            return cached["results"]
    results = []
    if source == "arxiv":
        previous = workflow.store.one("SELECT value FROM settings WHERE key='arxiv_last_request'")
        if previous and time.time() - float(previous["value"]) < 3:
            raise AEError("arXiv requires at least 3 seconds between requests; retry shortly")
        workflow.store.db.execute("INSERT OR REPLACE INTO settings VALUES('arxiv_last_request',?)", (str(time.time()),))
        url = "https://export.arxiv.org/api/query?" + urlencode({"search_query": query, "start": 0, "max_results": limit})
        try:
            root = ET.fromstring(fetch(url))
        except ET.ParseError as exc:
            raise AEError("arXiv returned invalid XML") from exc
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("a:entry", ns):
            paper = entry.findtext("a:title", "", ns).strip()
            link = entry.findtext("a:id", "", ns)
            if "/api/errors" in link:
                raise AEError("arXiv rejected the search query")
            for author in entry.findall("a:author", ns):
                name = author.findtext("a:name", "", ns).strip()
                if name:
                    results.append({"name": name, "evidence": json.dumps({"paper": paper, "url": link, "query": query})})
    elif source == "openalex":
        api_key = os.environ.get(workflow.config["discovery"]["openalex_key_env"], "")
        params = {"search": query, "per-page": limit}
        if api_key:
            params["api_key"] = api_key
        try:
            data = json.loads(fetch("https://api.openalex.org/works?" + urlencode(params)))
            for work in data["results"]:
                for authorship in work.get("authorships", []):
                    author = authorship.get("author", {})
                    if author.get("display_name"):
                        institutions = ", ".join(i.get("display_name", "") for i in authorship.get("institutions", []))
                        results.append({"name": author["display_name"], "affiliation": institutions,
                                        "evidence": json.dumps({"paper": work.get("title"), "url": work.get("id"),
                                                                "author_id": author.get("id"), "query": query})})
        except (ValueError, KeyError, TypeError) as exc:
            raise AEError("OpenAlex returned malformed data") from exc
    else:
        raise AEError("Unknown public discovery source")
    workflow.store.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps({"at": time.time(), "results": results})))
    return results


def discover(workflow, sid, source, query, limit=10):
    workflow.submission(sid)
    if not query.strip() or not 1 <= limit <= 50:
        raise AEError("Supply a query and a limit between 1 and 50")
    if source == "scholar":
        return {"url": "https://scholar.google.com/scholar?" + urlencode({"q": query}),
                "note": "Inspect Scholar in your browser, then add candidates with reviewers add. No scraping."}
    if source == "contacts":
        tokens = set(re.findall(r"\w+", query.casefold()))
        results = []
        for c in workflow.store.all("SELECT * FROM contacts"):
            overlap = tokens & set(re.findall(r"\w+", (c["topics"] + " " + c["name"]).casefold()))
            if overlap:
                results.append({"name": c["name"], "email": c["email"], "score": len(overlap) / len(tokens),
                                "evidence": json.dumps({"source": c["source"], "subject": c["topics"], "matched_terms": sorted(overlap)})})
        results = sorted(results, key=lambda c: c["score"], reverse=True)[:limit]
    else:
        results = public_results(workflow, source, query, limit)
    return [workflow.add_candidate(sid, source=source, **result) for result in results]

