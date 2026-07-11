from collections.abc import Mapping
from typing import Any, Literal, Protocol

from bs4 import BeautifulSoup

from src.contracts import EpisodeItem, EpisodeListResponse, NovelResponse
from src.helper import normalize_url
from src.logutil import get_logger

logger = get_logger(__name__)

AccountStatus = Literal["paid", "free", "unknown"]


class NovelMetadataClient(Protocol):
    def me(self) -> Mapping[str, Any]: ...

    def novel(self, novel_id: int) -> NovelResponse: ...

    def episode_list(self, novel_id: int, rows: int) -> EpisodeListResponse: ...


# ----------------------------
# Novelpia Novel & Episodes Fetcher
# ----------------------------


def html_from_episode_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "lxml")

    # normalize images
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


def user_subscription_status(me_response: object) -> AccountStatus:
    if not isinstance(me_response, dict):
        return "unknown"
    result = me_response.get("result")
    if not isinstance(result, dict):
        return "unknown"
    login = result.get("login")
    if not isinstance(login, dict):
        return "unknown"
    subscription = result.get("subscription")
    if subscription is not None:
        return "paid"
    plus_type = login.get("mem_plus_type")
    if isinstance(plus_type, int):
        return "paid" if plus_type != 0 else "free"
    if isinstance(plus_type, str) and plus_type.isdecimal():
        return "paid" if int(plus_type) != 0 else "free"
    return "unknown"


def fetch_novel_and_episodes(
    client: NovelMetadataClient,
    novel_id: int,
) -> tuple[NovelResponse, list[EpisodeItem], str, AccountStatus]:
    # Auth check
    account_status: AccountStatus = "unknown"
    try:
        res = client.me()
        if str(res.get("statusCode")) == "200":
            account_status = user_subscription_status(res)
            mem = (((res.get("result") or {}).get("login") or {}).get("mem_nick")) or "Unknown"
            logger.info(f"[auth] Logged in as: {mem}")
            logger.info(f"[info] User status: {account_status}")
    except Exception as e:
        logger.warning(f"[warn] auth check failed: {e}")

    logger.info("[info] extracting metadata…")
    data_novel = client.novel(novel_id)

    nv = data_novel["result"]["novel"]
    title = nv.get("novel_name", f"novel_{novel_id}")
    epi_cnt = data_novel["result"].get("info", {}).get("epi_cnt") or nv.get("count_epi") or 0
    writers = data_novel["result"].get("writer_list") or []
    author = writers[0].get("writer_name") if writers and writers[0].get("writer_name") else "Unknown Author"
    status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"

    logger.info(f"[info] title='{title}' author='{author}' chapter={epi_cnt} status={status}")

    rows = max(2, int(epi_cnt)) if epi_cnt else 1000
    data_list = client.episode_list(novel_id, rows=rows)
    ep_list = data_list["result"].get("list", [])

    return data_novel, ep_list, title, account_status
