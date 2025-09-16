from typing import Dict, List, Set
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import requests

from fetch_utils import build_session, DEFAULT_HEADERS


def is_internal(base_url: str, href: str) -> bool:
    href_url = urlparse(href)
    base_url_parsed = urlparse(base_url)
    if not href_url.netloc:
        return True
    return href_url.netloc == base_url_parsed.netloc


def check_internal_links(html: str, base_url: str, timeout: int = 20, max_links: int = 25) -> Dict:
    soup = BeautifulSoup(html, 'html.parser')
    internal_links: Set[str] = set()
    for a in soup.find_all('a', href=True):
        full = urljoin(base_url, a['href'])
        if is_internal(base_url, full):
            internal_links.add(full)

    links_to_check = list(internal_links)[:max_links]
    broken: List[str] = []

    session = build_session(DEFAULT_HEADERS)
    for link in links_to_check:
        try:
            r = session.get(link, timeout=timeout, allow_redirects=True)
            if r.status_code >= 400:
                broken.append(f"{link} ({r.status_code})")
        except requests.RequestException:
            broken.append(f"{link} (request failed)")

    # Count contextual links within main content (heuristic): anchors inside <main>, or in <p>/<li> not within header/nav/footer/aside
    def is_in_context(a_tag) -> bool:
        # Exclude common chrome
        for parent in a_tag.parents:
            if parent.name in ("header", "nav", "footer", "aside"):
                return False
        if a_tag.find_parent("main") is not None:
            return True
        parent = a_tag.find_parent(["p", "li", "article", "section"])
        return parent is not None

    contextual_links = [
        urljoin(base_url, a['href']) for a in soup.find_all('a', href=True)
        if is_internal(base_url, urljoin(base_url, a['href'])) and is_in_context(a)
    ]
    contextual_count = len(set(contextual_links))

    status = "pass" if links_to_check and not broken and contextual_count >= 2 else (
        "warning" if links_to_check and contextual_count > 0 else ("fail" if not links_to_check or contextual_count == 0 else "warning")
    )
    return {
        "total_internal": len(internal_links),
        "checked": len(links_to_check),
        "broken": broken,
        "contextual_links": contextual_count,
        "status": status,
        "message": (
            (
                f"Checked {len(links_to_check)} internal links; all working. Contextual links: {contextual_count}."
                if not broken and links_to_check and contextual_count >= 2 else
                ("No internal links found." if not links_to_check else (
                    f"{len(broken)} broken link(s) found." if broken else f"Only {contextual_count} contextual link(s); need 2+."
                ))
            )
        )
    }
