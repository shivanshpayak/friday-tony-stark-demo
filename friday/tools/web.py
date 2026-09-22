"""
Web tools — search, fetch pages, and global news briefings.
"""

import httpx
import xml.etree.ElementTree as ET
import asyncio  # Required for parallel execution
import re
from datetime import datetime

SEED_FEEDS = [
    'https://feeds.bbci.co.uk/news/world/rss.xml',
    'https://www.cnbc.com/id/100727362/device/rss/rss.html',
    'https://rss.nytimes.com/services/xml/rss/nyt/World.xml',
    'https://www.aljazeera.com/xml/rss/all.xml'
]

async def fetch_and_parse_feed(client, url):
    """Helper function to handle a single feed request and parse its XML."""
    try:
        response = await client.get(url, headers={'User-Agent': 'Friday-AI/1.0'}, timeout=5.0)
        if response.status_code != 200:
            return []

        root = ET.fromstring(response.content)
        # Extract source name from URL (e.g., 'BBC' or 'NYTIMES')
        source_name = url.split('.')[1].upper()
        
        feed_items = []
        # Get top 5 items per feed
        items = root.findall(".//item")[:5]
        for item in items:
            title = item.findtext("title")
            description = item.findtext("description")
            link = item.findtext("link")
            
            if description:
                description = re.sub('<[^<]+?>', '', description).strip()

            feed_items.append({
                "source": source_name,
                "title": title,
                "summary": description[:200] + "..." if description else "",
                "link": link
            })
        return feed_items
    except Exception:
        # If one feed fails, return an empty list so others can still succeed
        return []

def open_on_screen_impl(target: str, kind: str = "auto") -> str:
    """Pull up a URL, a Google search, or a local file/folder. See the tool
    docstring for arg meaning. Kept module-level so it is unit-testable."""
    import os
    import webbrowser
    from urllib.parse import quote_plus, urlparse
    from friday.tools.files import _resolve_and_check

    target = (target or "").strip()
    if not target:
        return "There's nothing to open, sir."

    def _looks_like_url(t: str) -> bool:
        if urlparse(t).scheme in ("http", "https"):
            return True
        return bool(re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", t))

    def _open_url(t: str) -> str:
        if urlparse(t).scheme not in ("http", "https"):
            t = "https://" + t
        webbrowser.open(t)
        return "Pulling that up, sir."

    def _open_search(t: str) -> str:
        webbrowser.open("https://www.google.com/search?q=" + quote_plus(t))
        return "Pulling that up, sir."

    def _open_file(t: str) -> str:
        path = _resolve_and_check(t)
        if path is None or not path.exists():
            return "That's outside the folders I can open, sir."
        os.startfile(str(path))
        return "Pulling that up, sir."

    kind = (kind or "auto").lower().strip()
    if kind == "url":
        return _open_url(target)
    if kind == "search":
        return _open_search(target)
    if kind == "file":
        return _open_file(target)
    # auto
    if _looks_like_url(target):
        return _open_url(target)
    path = _resolve_and_check(target)
    if path is not None and path.exists():
        return _open_file(target)
    return _open_search(target)


def register(mcp):

    @mcp.tool()
    async def get_world_news() -> str:
        """
        Fetches the latest global headlines from major news outlets simultaneously.
        Use this when the user asks 'What's going on in the world?' or for recent events.
        """
        
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            # 1. Create a list of 'tasks' (one for each URL)
            tasks = [fetch_and_parse_feed(client, url) for url in SEED_FEEDS]
            
            # 2. Fire them all at once and wait for the results
            # results will be a list of lists: [[news from bbc], [news from nyt], ...]
            results_of_lists = await asyncio.gather(*tasks)
            
            # 3. Flatten the list of lists into a single list of articles
            all_articles = [item for sublist in results_of_lists for item in sublist]

        if not all_articles:
            return "The global news grid is unresponsive, sir. I'm unable to pull headlines."

        # 4. Format a compact, speech-friendly briefing — no brackets,
        #    no markdown, just plain sentences the LLM can paraphrase.
        lines = []
        for entry in all_articles[:5]:
            lines.append(f"{entry['title']}.")

        return "Here are today's top stories. " + " ".join(lines)

    @mcp.tool()
    async def search_web(query: str) -> str:
        """
        Search the web for current information on any topic.
        Use this when the user asks about a specific event, person, conflict,
        or anything that needs up-to-date information beyond general news headlines.
        """
        from ddgs import DDGS
        import asyncio

        def _search():
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=5))
                if not results:
                    return "No results found for that query."
                lines = []
                for r in results:
                    title = r.get("title", "")
                    body = r.get("body", "")
                    # Keep it concise and speech-friendly
                    if body:
                        lines.append(f"{title}: {body[:150]}")
                    else:
                        lines.append(title)
                return " | ".join(lines)
            except Exception as e:
                return f"Search failed: {str(e)}"

        # Run the synchronous DDGS call in a thread so we don't block the event loop
        return await asyncio.get_event_loop().run_in_executor(None, _search)

    @mcp.tool()
    async def open_world_monitor() -> str:
        """
        Opens the World Monitor dashboard (worldmonitor.app) in the system's web browser.
        Use this when the user wants a visual overview of global events or a real-time map.
        """
        import webbrowser
        url = "https://worldmonitor.app/"
        
        try:
            # This opens the URL in the default browser (Chrome/Edge/Safari)
            webbrowser.open(url)
            return "Opening the World Monitor for you now."
        except Exception as e:
            return f"I'm unable to initialize the visual monitor: {str(e)}"
            
    @mcp.tool()
    def create_document(doc_type: str) -> str:
        """
        Creates a new web-based document by opening its associated shortcut URL.
        Use this when the user says "new slide", "fresh doc", "create a spreadsheet", or "new code repo".
        Types should be strings like "slide", "doc", "sheet", or "repo".
        """
        import os
        doc_type = doc_type.lower()
        mapping = {
            "slide": "https://slides.new",
            "slides": "https://slides.new",
            "presentation": "https://slides.new",
            "doc": "https://docs.new",
            "document": "https://docs.new",
            "word": "https://docs.new",
            "sheet": "https://sheets.new",
            "spreadsheet": "https://sheets.new",
            "excel": "https://sheets.new",
            "repo": "https://repo.new",
            "repository": "https://repo.new"
        }
        
        target = mapping.get(doc_type, "https://docs.new") # Default to doc
        try:
            os.startfile(target)
            return f"Opening a new {doc_type} for you in the browser."
        except Exception as e:
            return f"Failed to create document: {str(e)}"

    @mcp.tool()
    def open_on_screen(target: str, kind: str = "auto") -> str:
        """Open/"pull up" something in front of the user: a web URL, a Google
        search, or a local file/folder. Use for "pull up X", "open X", "show me X
        on screen", "google X and open it". First say a short line (ACTING OUT
        LOUD), then call this.

        kind: "auto" (default), "url", "search", or "file".
        - url: opens the target URL in the default browser.
        - search: opens a Google search for the target text.
        - file: opens a local file (default app) or folder (Explorer), restricted
          to Friday's allowed folders.
        - auto: url if it looks like a link, an allowed file if it resolves to one,
          otherwise a Google search.
        """
        return open_on_screen_impl(target, kind)