from __future__ import annotations

from digest.render.text import md_text, safe_url


def test_md_text_neutralises_markdown():
    assert md_text("A [link](http://evil) and **bold**\nnext") == "A ［link］(http://evil) and ＊＊bold＊＊ next"
    assert md_text("<font color=red>x</font>") == "‹font color=red›x‹/font›"


def test_safe_url_rejects_other_schemes():
    assert safe_url("javascript:alert(1)") is None
    assert safe_url("dingtalk://dingtalkclient/page/link") is None
    assert safe_url("https:///nohost") is None


def test_safe_url_escapes_parens_and_spaces():
    assert safe_url("https://a.test/x(1) y") == "https://a.test/x%281%29%20y"
    assert safe_url("https://a.test/p?q=1&r=%20") == "https://a.test/p?q=1&r=%20"
