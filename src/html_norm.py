"""Episode HTML normalization (presentation layer, independent of API client)."""

from bs4 import BeautifulSoup

from src.helper import normalize_url


def html_from_episode_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "lxml")

    for img in soup.find_all("img"):
        if img.get("data-src") and not img.get("src"):
            img["src"] = img["data-src"]
        if "style" in img.attrs:
            del img["style"]
        if img.get("src"):
            img["src"] = normalize_url(img["src"])

    # Ensure document wrapper with meta charset="utf-8"
    if not soup.find("html"):
        html_tag = soup.new_tag("html")
        head_tag = soup.new_tag("head")
        meta_tag = soup.new_tag("meta", attrs={"charset": "utf-8"})
        head_tag.append(meta_tag)
        body = soup.new_tag("body")
        for el in list(soup.children):
            body.append(el.extract())
        html_tag.append(head_tag)
        html_tag.append(body)
        soup.append(html_tag)
    else:
        # With lxml, soup often gets auto-wrapped in <html><body>
        # We need to ensure the head and meta tag are present to avoid regressions.
        found_head = soup.find("head")
        if not found_head or isinstance(found_head, str):
            found_head = soup.new_tag("head")
            found_html = soup.html
            if found_html is not None and not isinstance(found_html, str):
                found_html.insert(0, found_head)

        found_meta = found_head.find("meta", {"charset": "utf-8"}) if not isinstance(found_head, str) else None
        if not found_meta:
            new_meta = soup.new_tag("meta", attrs={"charset": "utf-8"})
            found_head.append(new_meta)

    return str(soup)
