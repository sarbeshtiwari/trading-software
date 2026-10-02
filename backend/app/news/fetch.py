"""Bounded public IPv4 HTTPS acquisition with pinned DNS and original TLS identity."""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass

import httpx

from app.news.sources import SourcePolicy


@dataclass(frozen=True)
class FeedResponse:
    body: bytes
    content_type: str


def public_address(address):
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not (
            address.version == 4
            and (
                address in ipaddress.ip_network("192.0.0.0/24")
                or address in ipaddress.ip_network("192.88.99.0/24")
            )
        )
    )


async def resolve_public_ipv4(host):
    answers = await asyncio.get_running_loop().getaddrinfo(
        host, 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
    )
    addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
    if not addresses or any(not public_address(address) for address in addresses):
        raise ValueError("News endpoint must resolve exclusively to public addresses")
    candidates = [str(address) for address in addresses if address.version == 4]
    if not candidates:
        raise ValueError("News endpoint requires a public IPv4 address")
    return candidates[0]


class PublicFeedFetcher:
    def __init__(
        self, *, timeout_seconds=15, maximum_bytes=1_000_000, resolver=None, transport=None
    ):
        if not 0 < timeout_seconds <= 60 or not 0 < maximum_bytes <= 5_000_000:
            raise ValueError("Invalid feed acquisition bounds")
        self.timeout_seconds = timeout_seconds
        self.maximum_bytes = maximum_bytes
        self.resolver = resolver or resolve_public_ipv4
        self.transport = transport

    async def fetch(self, policy: SourcePolicy) -> FeedResponse:
        policy = SourcePolicy.model_validate(policy.model_dump())
        return await asyncio.wait_for(self._fetch(policy), timeout=self.timeout_seconds)

    async def _fetch(self, policy):
        endpoint = httpx.URL(policy.endpoint)
        pinned = await self.resolver(endpoint.host)
        address = ipaddress.ip_address(pinned)
        if address.version != 4 or not public_address(address):
            raise ValueError("News connection address is not public IPv4")
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=self.timeout_seconds,
            transport=self.transport,
            limits=httpx.Limits(max_connections=1),
        ) as client:
            async with client.stream(
                "GET",
                endpoint.copy_with(host=str(address)),
                headers={
                    "Host": endpoint.host,
                    "Accept-Encoding": "identity",
                    "Accept": "application/rss+xml, application/xml, application/json",
                    "User-Agent": "ATS-News/1",
                },
                extensions={"sni_hostname": endpoint.host},
            ) as response:
                if response.status_code != 200:
                    raise ValueError("News source did not return a successful document")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed news documents are not accepted")
                length = response.headers.get("content-length")
                if length is not None and (
                    not length.isdigit() or int(length) > self.maximum_bytes
                ):
                    raise ValueError("News document exceeds acquisition bounds")
                chunks, size = [], 0
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > self.maximum_bytes:
                        raise ValueError("News document exceeds acquisition bounds")
                    chunks.append(chunk)
                return FeedResponse(
                    body=b"".join(chunks), content_type=response.headers.get("content-type", "")
                )
