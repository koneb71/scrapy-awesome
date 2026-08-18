"""Deterministic extraction: selectors → raw values → typed values, plus in-process validation.

The exact same code path runs inside `RecipeSpider` (crawl) and inside the design-time preview, so
"what you preview is what you crawl".
"""

from scrapy_awesome.extract.engine import ExtractedItem, extract_list_items, extract_page_fields
from scrapy_awesome.extract.validate import ValidationReport, validate_on_samples

__all__ = [
    "ExtractedItem",
    "ValidationReport",
    "extract_list_items",
    "extract_page_fields",
    "validate_on_samples",
]
