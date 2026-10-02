"""Strict RSS 2.0 and declared JSON feeds; missing publication or body is not invented."""

import json
from email.utils import parsedate_to_datetime
from typing import Protocol

from defusedxml import ElementTree
from pydantic import Field

from app.analysis.equity import EvidenceModel
from app.core.data_origin import DataOrigin
from app.news.fetch import PublicFeedFetcher
from app.news.ingest import ArticleInput
from app.news.sources import SourcePolicy


class FeedArticle(ArticleInput):
    data_origin: DataOrigin = DataOrigin.LIVE


class JsonFeed(EvidenceModel):
    articles: list[FeedArticle] = Field(max_length=100)


class NewsProvider(Protocol):
    async def articles(self, policy: SourcePolicy) -> list[ArticleInput]: ...


def strict_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate feed field")
        result[name] = value
    return result


class RemoteNewsProvider:
    def __init__(self, fetcher=None, *, data_origin=DataOrigin.LIVE):
        self.fetcher = fetcher or PublicFeedFetcher()
        self.data_origin = DataOrigin(data_origin)

    async def articles(self, policy):
        if policy.kind not in {"rss", "api"}:
            raise ValueError("No adapter configured for this source kind")
        response = await self.fetcher.fetch(policy)
        text = response.body.decode("utf-8-sig")
        media_type = response.content_type.split(";", 1)[0].strip().lower()
        if policy.kind == "api":
            if media_type != "application/json":
                raise ValueError("JSON news feed content type required")
            parsed = json.loads(text, object_pairs_hook=strict_object)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("articles"), list):
                raise ValueError("Declared news API schema required")
            for article in parsed["articles"]:
                if not isinstance(article, dict) or "data_origin" in article:
                    raise ValueError("Remote content cannot declare provenance")
            return [
                article.model_copy(update={"data_origin": self.data_origin})
                for article in JsonFeed.model_validate(parsed).articles
            ]
        return self.rss_articles(text, media_type)

    def rss_articles(self, text, media_type):
        if media_type not in {"application/rss+xml", "application/xml", "text/xml"}:
            raise ValueError("RSS news feed content type required")
        if "<!doctype" in text.lower() or "<!entity" in text.lower():
            raise ValueError("RSS document declarations are forbidden")
        root = ElementTree.fromstring(
            text, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
        if root.tag != "rss" or root.get("version") != "2.0":
            raise ValueError("Only RSS 2.0 feeds are supported")
        channels = root.findall("channel")
        if len(channels) != 1:
            raise ValueError("RSS channel unavailable")
        items = channels[0].findall("item")
        if len(items) > 100:
            raise ValueError("News feed contains too many articles")
        articles = []
        for item in items:
            fields = {}
            for field in ("title", "link", "description", "pubDate"):
                nodes = item.findall(field)
                if len(nodes) != 1 or not nodes[0].text or len(nodes[0]):
                    raise ValueError("RSS article field missing or ambiguous")
                fields[field] = nodes[0].text
            articles.append(
                ArticleInput(
                    title=fields["title"],
                    url=fields["link"],
                    body=fields["description"],
                    published_at=parsedate_to_datetime(fields["pubDate"]),
                    data_origin=self.data_origin,
                )
            )
        return articles
