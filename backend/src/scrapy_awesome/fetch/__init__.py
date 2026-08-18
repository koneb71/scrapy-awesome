"""Fetch layer: tiered policy (http → browser → interactive), block detection, page cache."""

from scrapy_awesome.fetch.blocks import BlockVerdict, classify_response
from scrapy_awesome.fetch.policy import TIER_ORDER, FetchPolicy, next_tier

__all__ = ["TIER_ORDER", "BlockVerdict", "FetchPolicy", "classify_response", "next_tier"]
