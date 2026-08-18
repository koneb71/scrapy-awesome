"""scrapy-awesome — local-first, AI-assisted interactive web scraper built on Scrapy."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("scrapy-awesome")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0+dev"

__all__ = ["__version__"]
