"""Recipe: the portable, schema-validated description of *what* to scrape and *how*.

A recipe is plain data (JSON/YAML). The generic `RecipeSpider` interprets it; LLMs and the UI edit it.
"""

from scrapy_awesome.recipe.models import (
    Action,
    DetailConfig,
    Extractor,
    FetchConfig,
    Field,
    FieldType,
    Limits,
    ListConfig,
    Pagination,
    Recipe,
    Tier,
)

__all__ = [
    "Action",
    "DetailConfig",
    "Extractor",
    "FetchConfig",
    "Field",
    "FieldType",
    "Limits",
    "ListConfig",
    "Pagination",
    "Recipe",
    "Tier",
]
