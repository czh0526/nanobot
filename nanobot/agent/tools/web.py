"""Web tools: web_search and web_fetch."""

import html
import json
import os
import re
import urllib
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)

        

class SearchBackend(ABC):
    """
    Abstract base class for search engine backends.
    """
    @abstractmethod 
    async def search(self, query: str, count: int) -> list[dict]:
        """
        Execute search and return list of results with title, url, snippet.
        """
        pass 

    @abstractmethod 
    def is_available(self) -> tuple[bool, str]:
        """
        Check if the backend is available. Returns (available, error_mssage).
        """
        pass 

class DuckDuckGoBackend(SearchBackend):
    """
    DuckDuckGo HTML search = free, on API key required.
    """
    async def search(self, query: str, count: int) -> list[dict]:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                timeout=15.0
            )
            r.raise_for_status() 

        return self._parse_html(r.text, count)

    def is_available(self) -> tuple[bool, str]:
        return True, "", 

    def _parse_html(self, html_content: str, max_results: int) -> list[dict]:
        """Parse DuckDuckGo HTML search results."""
        results = []
        
        # Pattern to extract title and URL
        title_pattern = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            re.DOTALL | re.IGNORECASE
        )
        snippet_pattern = re.compile(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL | re.IGNORECASE
        )
        
        # Split by result divs
        result_divs = re.split(
            r'<div[^>]*class="[^"]*result[^"]*results_links[^"]*"',
            html_content
        )
        
        for div in result_divs[1:max_results + 1]:
            title_match = title_pattern.search(div)
            snippet_match = snippet_pattern.search(div)
            
            if title_match:
                url = title_match.group(1)
                # Extract actual URL from DuckDuckGo redirect
                if "uddg=" in url:
                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                    url = parsed.get("uddg", [url])[0]
                
                title = _strip_tags(title_match.group(2))
                snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ""
                
                if title and url:
                    results.append({"title": title, "url": url, "snippet": snippet})
            
            if len(results) >= max_results:
                break
        
        return results


class WebSearchTool(Tool):
    """Search the web using Brave Search API."""

    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10}
        },
        "required": ["query"]
    }

    def __init__(self, 
        api_key: str | None = None, 
        max_results: int = 5,
        engine: str = "duckduckgo"):
        
        self.max_results = max_results
        if engine: 
            self.engine = engine.lower()

        if api_key:
            self.api_key = api_key 
        elif self.engine == "tavily":
            self.api_key = os.environ.get("TAVILY_API_KEY", "")
        elif self.engine == "brave":
            self.api_key = os.environ.get("BRAVE_API_KEY", "") 

        self._backend = self._create_backend()

    def _create_backend(self) -> SearchBackend:
        """Create the search backend based on configured engine."""
        if self.engine == "duckduckgo":
            return DuckDuckGoBackend()
        else:
            logger.error(f"Unsupported engine `{self.engine}`")
            return None

    async def execute(
        self, 
        query: str, 
        count: int | None = None, 
        **kwargs: Any) -> str: 
        
        available, error_msg = self._backend.is_available()
        if not available:
            return f"Error: {error_msg}"
        
        try:
            n = min(max(count or self.max_results, 1), 10)
            
            results = await self._backend.search(query, n)
            if not results:
                return f"Noresults for: {query}"
            
            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results, 1):
                lines.append(f"{i}. {item['title']}\n   {item['url']}")
                if item.get("snippet"):
                    lines.append(f" {item['snippet']}")

            return "\n".join(lines) 

        except Exception as e:
            return f"Error: {e}"

class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Readability."""

    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML → markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100}
        },
        "required": ["url"]
    }

    def __init__(self, max_chars: int = 50000, proxy: str | None = None):
        self.max_chars = max_chars
        self.proxy = proxy

    async def execute(self, url: str, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any) -> str:
        from readability import Document

        max_chars = maxChars or self.max_chars
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        try:
            logger.debug("WebFetch: {}", "proxy enabled" if self.proxy else "direct connection")
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=30.0,
                proxy=self.proxy,
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()

            ctype = r.headers.get("content-type", "")

            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2, ensure_ascii=False), "json"
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = self._to_markdown(doc.summary()) if extractMode == "markdown" else _strip_tags(doc.summary())
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"

            truncated = len(text) > max_chars
            if truncated: text = text[:max_chars]

            return json.dumps({"url": url, "finalUrl": str(r.url), "status": r.status_code,
                              "extractor": extractor, "truncated": truncated, "length": len(text), "text": text}, ensure_ascii=False)
        except httpx.ProxyError as e:
            logger.error("WebFetch proxy error for {}: {}", url, e)
            return json.dumps({"error": f"Proxy error: {e}", "url": url}, ensure_ascii=False)
        except Exception as e:
            logger.error("WebFetch error for {}: {}", url, e)
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

    def _to_markdown(self, html: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html, flags=re.I)
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))
