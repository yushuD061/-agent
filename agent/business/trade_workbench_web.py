"""Whitelist-first public web fetcher with SSRF protections."""
from __future__ import annotations
import ipaddress, re, socket
from urllib.parse import urljoin, urlparse
import httpx
from agent.business.trade_workbench_repository import TradeWorkbenchError, content_hash, now_utc


def validate_public_url(url: str, allowed_domains: set[str] | None = None) -> str:
    parsed=urlparse(str(url).strip())
    if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
        raise TradeWorkbenchError("trade_source_url_invalid")
    host=parsed.hostname.lower().rstrip(".")
    if allowed_domains and not any(host==domain or host.endswith("."+domain) for domain in allowed_domains):
        raise TradeWorkbenchError("trade_source_not_whitelisted",403)
    try: addresses={item[4][0] for item in socket.getaddrinfo(host,parsed.port or (443 if parsed.scheme=="https" else 80),type=socket.SOCK_STREAM)}
    except socket.gaierror as exc: raise TradeWorkbenchError("trade_source_unavailable",502) from exc
    if not addresses: raise TradeWorkbenchError("trade_source_unavailable",502)
    for value in addresses:
        ip=ipaddress.ip_address(value)
        if not ip.is_global: raise TradeWorkbenchError("trade_source_private_address",403)
    return parsed.geturl()


async def fetch_public_page(url: str, *, allowed_domains: set[str] | None = None,
                            max_bytes: int = 2_000_000) -> dict:
    safe=validate_public_url(url,allowed_domains)
    async with httpx.AsyncClient(follow_redirects=False,timeout=15,headers={"User-Agent":"NanoClawTradeWorkbench/1.0"}) as client:
        response=await client.get(safe)
        if response.status_code in {301,302,303,307,308}:
            target=validate_public_url(urljoin(safe,response.headers.get("location","")),allowed_domains)
            response=await client.get(target); safe=target
        if response.status_code>=400: raise TradeWorkbenchError("trade_source_fetch_failed",502)
        raw=response.content
        if len(raw)>max_bytes: raise TradeWorkbenchError("trade_source_too_large",413)
    content_type=response.headers.get("content-type","").lower()
    if "html" not in content_type and "text/plain" not in content_type: raise TradeWorkbenchError("trade_source_type_unsupported",415)
    text=response.text
    text=re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>"," ",text)
    text=re.sub(r"(?s)<[^>]+>"," ",text); text=re.sub(r"\s+"," ",text).strip()[:100_000]
    return {"url":safe,"content":text,"fetched_at":now_utc(),"content_hash":content_hash(text)}
