import pytest
from pydantic import ValidationError

from scrapy_awesome.recipe import Extractor, Field, Recipe
from scrapy_awesome.recipe.compat import incompatible_changes, is_resume_compatible
from scrapy_awesome.recipe.io import dump_recipe, loads_recipe
from scrapy_awesome.recipe.models import selector_kind

BASE = {
    "name": "t",
    "seeds": ["https://example.com/list"],
    "list": {"container": "article.product_pod"},
    "fields": [
        {"name": "title", "extract": {"css": "h3 a::attr(title)"}},
        {"name": "price", "type": "price", "extract": {"css": ".price_color::text"}},
    ],
}


def test_minimal_recipe_roundtrip():
    r = Recipe.model_validate(BASE)
    text = dump_recipe(r)
    r2 = loads_recipe(text)
    assert r2.to_dict() == r.to_dict()
    assert r.domains() == ["example.com"]
    assert [f.name for f in r.list_fields] == ["title", "price"]


def test_extractor_exactly_one_source():
    with pytest.raises(ValidationError):
        Extractor()
    with pytest.raises(ValidationError):
        Extractor(css="a", xpath="//a")
    assert Extractor(css="a", attr="href").describe() == "a @href"
    assert Extractor(json_path="props.items[*].name").source == "json_path"


def test_field_name_rules():
    with pytest.raises(ValidationError):
        Field(name="Bad Name", extract=Extractor(css="a"))
    with pytest.raises(ValidationError):
        Field(name="_url", extract=Extractor(css="a"))
    with pytest.raises(ValidationError):
        Field(name="x", type="enum", extract=Extractor(css="a"))  # enum needs values


def test_detail_scope_requires_detail_enabled():
    data = dict(BASE)
    data["fields"] = BASE["fields"] + [
        {"name": "description", "scope": "detail", "extract": {"css": "p::text"}}
    ]
    with pytest.raises(ValidationError):
        Recipe.model_validate(data)
    data["detail"] = {"enabled": True, "link": {"css": "h3 a", "attr": "href"}}
    r = Recipe.model_validate(data)
    assert [f.name for f in r.detail_fields] == ["description"]


def test_dedupe_key_must_exist():
    data = dict(BASE, dedupe_key=["nope"])
    with pytest.raises(ValidationError):
        Recipe.model_validate(data)
    assert Recipe.model_validate(dict(BASE, dedupe_key=["title", "_url"])).dedupe_key == [
        "title",
        "_url",
    ]


def test_pagination_validation():
    with pytest.raises(ValidationError):
        Recipe.model_validate(dict(BASE, pagination={"kind": "next_link"}))
    r = Recipe.model_validate(
        dict(BASE, pagination={"kind": "url_template", "url_template": "https://e.com/?p={page}"})
    )
    assert r.pagination.max_pages == 20


def test_single_page_type_rescopes_fields():
    data = dict(BASE, page_type="single")
    data.pop("list")
    r = Recipe.model_validate(data)
    assert all(f.scope == "page" for f in r.fields)


def test_selector_kind():
    assert selector_kind("div.a > b") == "css"
    assert selector_kind("//div[@id='x']") == "xpath"
    assert selector_kind("(//a)[1]") == "xpath"
    assert selector_kind("./h3/a") == "xpath"


def test_resume_compat():
    a = Recipe.model_validate(BASE)
    b = a.model_copy(deep=True)
    b.fields[0].extract = Extractor(css="h3 a::text")
    assert is_resume_compatible(a, b)
    c = a.model_copy(deep=True)
    c.pagination.kind = "next_link"
    c.pagination.selector = "li.next a"
    assert incompatible_changes(a, c) == ["pagination"]
