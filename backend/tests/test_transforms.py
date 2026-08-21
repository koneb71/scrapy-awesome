"""Field transforms: the long tail of "the value is nearly right"."""

from __future__ import annotations

import pytest

from scrapy_awesome.extract.engine import extract_list_items
from scrapy_awesome.extract.transform import apply, apply_one
from scrapy_awesome.recipe.models import Recipe, Transform

HTML = """
<ul>
  <li class="row">
    <span class="t">  The   Big   Book </span>
    <span class="p">Price: 1.234,56 €</span>
    <span class="tags">fiction, classics , </span>
    <a class="l" href="/book/1">read</a>
  </li>
</ul>
"""


def t(kind: str, **kw: object) -> Transform:
    return Transform(kind=kind, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kind,kwargs,value,expected",
    [
        ("trim", {}, "  hi  ", "hi"),
        ("trim", {"chars": "*"}, "**hi**", "hi"),
        ("collapse_space", {}, " a   b \n c ", "a b c"),
        ("strip_prefix", {"value": "Price: "}, "Price: 12", "12"),
        ("strip_prefix", {"value": "Nope"}, "Price: 12", "Price: 12"),
        ("strip_suffix", {"value": " €"}, "12 €", "12"),
        ("replace", {"pattern": ",", "value": ""}, "1,234", "1234"),
        ("regex_replace", {"pattern": r"\D+", "value": ""}, "a1b2", "12"),
        ("prepend", {"value": "https://x"}, "/a", "https://x/a"),
        ("append", {"value": ".html"}, "/a", "/a.html"),
        ("decimal_comma", {}, "1.234,56", "1234.56"),
        ("decimal_comma", {}, "1,234.56", "1,234.56"),  # already anglo: untouched
        ("digits", {}, "£12.50 each", "12.50"),
        ("default", {"value": "n/a"}, "   ", "n/a"),
        ("split", {"pattern": ",", "index": 0}, "a,b,c", "a"),
        ("split", {"pattern": ",", "index": -1}, "a,b,c", "c"),
        ("split", {"pattern": ",", "index": 9}, "a,b", None),  # out of range, not a crash
    ],
)
def test_each_transform(kind: str, kwargs: dict, value: str, expected: object):
    assert apply_one(value, t(kind, **kwargs)) == expected


def test_a_chain_runs_in_order_and_a_split_fans_out():
    chain = [t("strip_prefix", value="Price: "), t("decimal_comma"), t("digits")]
    assert apply(["Price: 1.234,56 €"], chain) == ["1234.56"]

    assert apply(["fiction, classics , "], [t("split", pattern=",")]) == ["fiction", "classics"]
    # values that transform themselves into nothing are dropped, not carried as empties
    assert apply(["", "  "], [t("trim")]) == []


def test_a_bad_regex_that_slipped_past_validation_leaves_the_value_alone():
    """Saving one is refused; a recipe hand-edited on disk still must not take the row down."""
    bad = Transform.model_construct(kind="regex_replace", pattern="(unclosed", value="")
    assert apply_one("keep", bad) == "keep"
    huge = Transform.model_construct(kind="regex_replace", pattern="a" * 500, value="")
    assert apply_one("keep", huge) == "keep"


def test_a_transform_never_costs_the_row():
    """A transform is a tidy-up. If it cannot apply, the value goes through as it was."""
    assert apply_one(None, t("trim")) is None
    assert apply_one(12, t("collapse_space")) == "12"  # a number is coerced to text, not dropped
    assert apply_one("x", Transform(kind="split", pattern="")) == ["x"]


def test_transforms_run_before_the_type_is_read():
    recipe = Recipe.model_validate(
        {
            "name": "t",
            "seeds": ["https://x/"],
            "list": {"container": "li.row"},
            "fields": [
                {
                    "name": "title",
                    "extract": {"css": "span.t"},
                    "transforms": [{"kind": "collapse_space"}],
                },
                {
                    "name": "price",
                    "type": "price",
                    "extract": {"css": "span.p"},
                    "transforms": [
                        {"kind": "strip_prefix", "value": "Price: "},
                        {"kind": "decimal_comma"},
                    ],
                },
                {
                    "name": "tags",
                    "type": "list",
                    "extract": {"css": "span.tags"},
                    "transforms": [{"kind": "split", "pattern": ","}],
                },
                {
                    "name": "url",
                    "type": "url",
                    "extract": {"css": "a.l", "attr": "href"},
                    "transforms": [{"kind": "strip_prefix", "value": "/"}],
                },
            ],
        }
    )
    items, _ = extract_list_items(recipe, HTML, "https://x/list")
    row = items[0].values
    assert row["title"] == "The Big Book"
    assert row["price"] == 1234.56  # the comma decimal survived coercion because it ran first
    assert row["tags"] == ["fiction", "classics"]
    assert row["url"] == "https://x/book/1"  # still absolutised after transforming


def test_a_transform_that_cannot_be_written_is_refused_at_save_time():
    with pytest.raises(ValueError, match="needs a pattern"):
        Transform(kind="regex_replace", value="x")
    with pytest.raises(ValueError, match="invalid"):
        Transform(kind="regex_replace", pattern="(unclosed", value="")
    with pytest.raises(ValueError, match="needs a value"):
        Transform(kind="strip_prefix")
