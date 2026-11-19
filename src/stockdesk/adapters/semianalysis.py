"""SemiAnalysis public RSS feeds (semianalysis.com, the semiconductor research site).

Only the publisher's own public RSS feeds are read: title, date, short summary and link.
No login, no paywall bypass, no page scraping. Article bodies are never stored or shown;
the public feed text is only scanned in memory for company / ticker mentions.
Requests are rate-limited by the engine (default: at most once an hour) and use
conditional GET (ETag / Last-Modified) when the server supports it.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from ..i18n import L
from ..models import NewsItem
from .base import AdapterError, HttpGet, ResearchAdapter, http_get

_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(s: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", s or ""))).strip()


def build_matchers(watch: dict[str, list[str]]) -> dict[str, re.Pattern]:
    """``watch``: symbol -> aliases. Short tickers (<3 chars) only match as $T or (T)."""
    out = {}
    for sym, aliases in watch.items():
        pats = [re.escape(a) for a in aliases if a and len(a) >= 3]
        base = re.escape(sym)
        pats.append(rf"\${base}")
        pats.append(rf"(?:NASDAQ|NYSE|NYSEARCA)\s*:\s*{base}")
        body = "|".join(rf"(?<![A-Za-z0-9]){p}(?![A-Za-z0-9])" for p in pats)
        if len(sym) >= 3:
            body += rf"|(?<![A-Za-z0-9$]){base}(?![A-Za-z0-9])"   # bare ticker: case-sensitive
            out[sym] = re.compile(body)
        else:
            out[sym] = re.compile(body)
    return out


def match_tickers(head: str, matchers: dict[str, re.Pattern], body: str = "", body_min: int = 3) -> list[str]:
    """A ticker is tagged if it is named in the title/summary, or at least ``body_min`` times in the body."""
    out = []
    for sym, pat in matchers.items():
        if pat.search(head) or (body and sum(1 for _ in pat.finditer(body)) >= body_min):
            out.append(sym)
    return out


def parse_feed(xml_bytes: bytes, source: str, matchers: dict[str, re.Pattern],
               max_items: int = 40, summary_chars: int = 280) -> list[NewsItem]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise AdapterError(L("RSS 无法解析", "The RSS feed could not be parsed")) from None
    items: list[NewsItem] = []
    for node in root.iter("item"):
        title = strip_html(node.findtext("title") or "")
        link = (node.findtext("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        summary = strip_html(node.findtext("description") or "")
        try:
            published = parsedate_to_datetime(node.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            published = 0.0
        # mentions: title + summary, plus the public feed text (scanned in memory, never stored)
        tickers = match_tickers(f"{title} {summary}", matchers,
                                strip_html(node.findtext(_CONTENT) or "")[:30000])
        if len(summary) > summary_chars:
            summary = summary[:summary_chars].rsplit(" ", 1)[0] + "…"
        items.append(NewsItem(id=link, source=source, title=title, summary=summary, link=link,
                              published=published, tickers=tickers))
        if len(items) >= max_items:
            break
    return items


class SemiAnalysisAdapter(ResearchAdapter):
    name = "semianalysis"

    def __init__(self, feeds: list[str], watch: dict[str, list[str]], getter: HttpGet = http_get,
                 max_items: int = 40, validators: dict | None = None):
        self.feeds = feeds
        self.matchers = build_matchers(watch)
        self._get = getter
        self.max_items = max_items
        self.validators = validators if validators is not None else {}   # url -> {etag, modified}

    def fetch(self) -> list[NewsItem]:
        """Tries each feed in order and returns the first one that yields items."""
        last_err: Exception | None = None
        for url in self.feeds:
            headers = {"Accept": "application/rss+xml, application/xml;q=0.9"}
            v = self.validators.get(url) or {}
            if v.get("etag"):
                headers["If-None-Match"] = v["etag"]
            if v.get("modified"):
                headers["If-Modified-Since"] = v["modified"]
            try:
                status, resp_headers, body = self._get(url, headers=headers, timeout=30.0)
            except AdapterError as e:
                last_err = e
                continue
            if status == 304:
                return []            # nothing new; cached items stay valid
            h = {k.lower(): val for k, val in resp_headers.items()}
            self.validators[url] = {"etag": h.get("etag"), "modified": h.get("last-modified")}
            try:
                items = parse_feed(body, "SemiAnalysis", self.matchers, self.max_items)
            except AdapterError as e:
                last_err = e
                continue
            if items:
                return items
        if last_err:
            raise last_err
        return []
