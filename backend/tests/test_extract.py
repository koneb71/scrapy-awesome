import json

from scrapy_awesome.extract import extract_list_items, extract_page_fields, validate_on_samples
from scrapy_awesome.extract.coerce import coerce_one
from scrapy_awesome.extract.engine import next_page_url
from scrapy_awesome.extract.jsonpath import resolve
from scrapy_awesome.extract.validate import Sample
from scrapy_awesome.recipe import Recipe
from tests.fixtures import sites

BASE = "http://fx.local"

RECIPE = {
    "name": "fixture static",
    "seeds": [f"{BASE}/static/"],
    "list": {"container": "article.product_pod", "alternates": ["//article"]},
    "detail": {"enabled": True, "link": {"css": "h3 a"}},
    "pagination": {"kind": "next_link", "selector": "li.next a", "max_pages": 5},
    "fields": [
        {"name": "title", "extract": {"css": "h3 a", "attr": "title"}, "required": True},
        {"name": "price", "type": "price", "extract": {"css": ".price_color::text"}},
        {
            "name": "rating",
            "type": "enum",
            "enum": ["One", "Two", "Three", "Four", "Five"],
            "extract": {"css": "p.star-rating::attr(class)", "regex": r"star-rating (\w+)"},
        },
        {"name": "in_stock", "type": "bool", "extract": {"css": ".availability::text"}},
        {"name": "url", "type": "url", "extract": {"css": "h3 a::attr(href)"}},
        {"name": "id", "type": "number", "extract": {"xpath": "./@data-id"}},
        {
            "name": "description",
            "scope": "detail",
            "extract": {"css": "#product_description ~ p::text"},
        },
        {"name": "tags", "type": "list", "scope": "detail", "extract": {"css": "ul.tags li::text"}},
        {"name": "missing", "extract": {"css": ".nope::text"}},
    ],
}


def test_extract_list_page():
    r = Recipe.model_validate(RECIPE)
    html = sites.list_page(1, "/static")
    items, which = extract_list_items(r, html, f"{BASE}/static/")
    assert which == "primary" and len(items) == 5
    it = items[0]
    assert it.values["title"] == "Widget 01"
    assert it.values["price"] == 11.5
    assert it.values["rating"] == "Two"
    assert it.values["in_stock"] is True
    assert it.values["url"] == f"{BASE}/static/item/1"
    assert it.values["id"] == 1
    assert it.detail_url == f"{BASE}/static/item/1"
    assert it.provenance["missing"] == "missing" and it.values["missing"] is None
    assert items[3].values["in_stock"] is False  # id 4 out of stock


def test_extract_detail_page():
    r = Recipe.model_validate(RECIPE)
    html = sites.detail_page(2, "/static")
    it = extract_page_fields(r, html, f"{BASE}/static/item/2", scope="detail")
    assert it.values["description"].startswith("Widget 02 is a fine widget")
    assert it.values["tags"] == ["a", "b", "c"]


def test_next_page_url():
    r = Recipe.model_validate(RECIPE)
    assert (
        next_page_url(r, sites.list_page(1, "/static"), f"{BASE}/static/")
        == f"{BASE}/static/?page=2"
    )
    assert next_page_url(r, sites.list_page(3, "/static"), f"{BASE}/static/?page=3") is None


def test_alternate_container_used():
    data = json.loads(json.dumps(RECIPE))
    data["list"]["container"] = "div.nope"
    r = Recipe.model_validate(data)
    items, which = extract_list_items(r, sites.list_page(1, "/static"), f"{BASE}/static/")
    assert which == "alt:1" and len(items) == 5


def test_json_container_and_paths():
    data = {
        "seeds": [f"{BASE}/embedded/"],
        "list": {"container": "json:__NEXT_DATA__.props.pageProps.products[*]"},
        "fields": [
            {"name": "title", "extract": {"json_path": "title"}},
            {"name": "price", "type": "price", "extract": {"json_path": "price"}},
            {"name": "first_tag", "extract": {"json_path": "tags[0]"}},
        ],
    }
    r = Recipe.model_validate(data)
    payload = {"props": {"pageProps": {"products": sites.CATALOG[:5]}}}
    items, which = extract_list_items(
        r, "<html></html>", f"{BASE}/embedded/", json_blobs={"__NEXT_DATA__": payload}
    )
    assert which == "primary" and len(items) == 5
    assert items[0].values == {"title": "Widget 01", "price": 11.5, "first_tag": "a"}
    assert resolve({"a": [{"b": 1}, {"b": 2}]}, "a[*].b") == [1, 2]
    assert resolve({"a": [{"b": 1}, {"b": 2}]}, "a.b") == [1, 2]
    assert resolve({"a": [1, 2, 3]}, "a[-1]") == [3]


def test_coercions():
    assert coerce_one("£1,234.50", "price") == 1234.5
    assert coerce_one("1.234,50 €", "price") == 1234.5
    assert coerce_one("about 42 items", "number") == 42
    assert coerce_one("In stock (3 available)", "bool") is True
    assert coerce_one("Sold out", "bool") is False
    assert coerce_one("2024-03-05", "date") == "2024-03-05"
    assert coerce_one("  spaced   text ", "text") == "spaced   text"
    assert coerce_one('{"a": 1}', "json") == {"a": 1}


def test_validate_report():
    r = Recipe.model_validate(RECIPE)
    samples = [
        Sample(f"{BASE}/static/", sites.list_page(1, "/static"), "list"),
        Sample(f"{BASE}/static/?page=2", sites.list_page(2, "/static"), "list"),
        Sample(f"{BASE}/static/item/1", sites.detail_page(1, "/static"), "detail"),
    ]
    rep = validate_on_samples(r, samples)
    d = rep.to_dict()
    assert d["fields"]["title"]["fill_rate"] == 1.0
    assert d["fields"]["missing"]["fill_rate"] == 0.0
    codes = {i.code for i in rep.issues}
    assert "empty_field" in codes  # `missing`
    assert rep.ok is False  # empty_field is an error
    assert d["pagination"]["found_on_first"] is True
    assert d["detail"]["with_link"] == 10
    assert d["fields"]["description"]["n_filled"] == 1
    assert len([row for row in rep.rows if row.get("_kind") != "detail"]) == 10


def test_validate_flags_positional_and_identical():
    data = json.loads(json.dumps(RECIPE))
    data["fields"] = [
        {"name": "a", "extract": {"css": "h3 a::attr(title)"}},
        {"name": "b", "extract": {"css": "h3:nth-child(1) a::attr(title)"}},
    ]
    data["detail"] = {"enabled": False}
    r = Recipe.model_validate(data)
    rep = validate_on_samples(r, [Sample(f"{BASE}/static/", sites.list_page(1, "/static"), "list")])
    codes = {i.code for i in rep.issues}
    assert {"positional_selector", "identical_columns"} <= codes
