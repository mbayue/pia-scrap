from collections.abc import Mapping
from typing import Any, Literal, Protocol

from src.contracts import EpisodeItem, EpisodeListResponse, NovelResponse
from src.html_norm import html_from_episode_text
from src.logutil import get_logger

logger = get_logger(__name__)

AccountStatus = Literal["paid", "free", "unknown"]

# Re-export for existing imports (tests, callers).
__all__ = [
    "AccountStatus",
    "NovelMetadataClient",
    "fetch_novel_and_episodes",
    "html_from_episode_text",
    "user_subscription_status",
]


class NovelMetadataClient(Protocol):
    def me(self) -> Mapping[str, Any]: ...

    def novel(self, novel_id: int) -> NovelResponse: ...

    def episode_list(self, novel_id: int, rows: int) -> EpisodeListResponse: ...


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
