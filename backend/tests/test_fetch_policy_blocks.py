from scrapy_awesome.fetch import FetchPolicy, classify_response, next_tier
from scrapy_awesome.recipe import Action, FetchConfig, Recipe

BASE = {
    "seeds": ["https://example.com/"],
    "list": {"container": "li"},
    "fields": [{"name": "t", "extract": {"css": "a::text"}}],
}


def test_next_tier_chain():
    assert next_tier("http") == "browser"
    assert next_tier("browser") == "interactive"
    assert next_tier("interactive") is None


def test_policy_auto_initial_tier_and_meta():
    r = Recipe.model_validate(BASE)
    p = FetchPolicy.from_recipe(r)
    assert p.initial_tier() == "http"
    assert p.initial_tier(remembered="browser") == "browser"
    m = p.to_meta("http")
    assert m["stealth"]["driver"] == "turbo"
    assert m["stealth"]["fallback"] is False
    assert m["sa"]["tier"] == "http"
    m = p.to_meta("browser")
    assert m["stealth"]["driver"] == "browser" and m["stealth"]["headless"] is True
    m = p.to_meta("interactive")
    assert m["playwright"] is True and m["stealth"] is False
    assert m["playwright_page_methods"] == []


def test_policy_actions_force_interactive_and_compile():
    cfg = FetchConfig(
        wait_for="article",
        actions=[
            Action(kind="scroll_until_stable", max_rounds=10),
            Action(kind="click", selector="button.more", times=2, optional=True),
            Action(kind="wait_ms", ms=100),
        ],
    )
    p = FetchPolicy.from_config(cfg)
    assert p.needs_interactive and p.initial_tier() == "interactive"
    methods = p.to_meta("interactive")["playwright_page_methods"]
    names = [m.method for m in methods]
    assert names[0] == "wait_for_selector"
    assert "evaluate" in names and "wait_for_timeout" in names
    assert p.escalation_allowed()


def test_policy_session_uses_named_context():
    cfg = FetchConfig(session="abc")
    p = FetchPolicy.from_config(cfg, storage_state_path="/tmp/state.json")
    m = p.to_meta("interactive")
    assert m["playwright_context"] == "session-abc"
    assert m["playwright_context_kwargs"]["storage_state"] == "/tmp/state.json"


def test_explicit_tier_disables_escalation():
    p = FetchPolicy.from_config(FetchConfig(tier="http"))
    assert p.initial_tier(remembered="browser") == "http"
    assert not p.escalation_allowed()


# ---- block detection -----------------------------------------------------------------------
def test_classify_status_block():
    v = classify_response(403, {}, "<html><body>Forbidden</body></html>")
    assert v.blocked and v.reason == "status"


def test_classify_cf_mitigated_header():
    v = classify_response(200, {"CF-Mitigated": "challenge"}, "<html></html>")
    assert v.blocked and v.reason == "cf_mitigated"


def test_classify_challenge_marker_short_body():
    body = (
        "<html><head><title>Just a moment...</title></head><body><script>x</script></body></html>"
    )
    v = classify_response(200, {}, body)
    assert v.blocked and v.reason == "challenge_marker"


def test_classify_marker_in_long_body_is_not_block():
    body = (
        "<html><body>" + ("<p>real product content here</p>" * 3000) + " recaptcha </body></html>"
    )
    v = classify_response(200, {}, body)
    assert not v.blocked and not v.needs_js


def test_classify_app_shell_needs_js():
    body = '<html><head><title>x</title></head><body><div id="root"></div><script src="a.js"></script></body></html>'
    v = classify_response(200, {}, body)
    assert v.needs_js and v.reason == "app_shell"


def test_classify_noscript_needs_js():
    body = "<html><body><noscript>You need to enable JavaScript to run this app.</noscript><div id=x></div><script></script></body></html>"
    v = classify_response(200, {}, body)
    assert v.needs_js and v.reason == "noscript"


def test_classify_selector_missing_needs_js():
    body = "<html><body><div>hi</div>" + "<script>a</script>" * 5 + "</body></html>"
    v = classify_response(200, {}, body, expected_selector_matched=0)
    assert v.needs_js and v.reason == "selector_missing"
    v2 = classify_response(200, {}, body, expected_selector_matched=5)
    assert not v2.escalate


def test_classify_normal_page():
    body = (
        "<html><head><title>Shop</title></head><body><ul><li><a>A</a></li></ul>"
        + "<p>text</p>" * 50
        + "</body></html>"
    )
    assert not classify_response(200, {}, body).escalate
