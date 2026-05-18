from app.runtime.proxy import _dedup_subdomains, _normalize_domain, render_squid_conf


def test_normalize_bare_domain() -> None:
    assert _normalize_domain("pypi.org") == ".pypi.org"


def test_normalize_wildcard() -> None:
    assert _normalize_domain("*.pythonhosted.org") == ".pythonhosted.org"


def test_normalize_already_dotted() -> None:
    assert _normalize_domain(".foo.com") == ".foo.com"


def test_normalize_strip_whitespace() -> None:
    assert _normalize_domain("  pypi.org  ") == ".pypi.org"


def test_dedup_removes_covered_subdomain() -> None:
    out = _dedup_subdomains(
        [".pypi.org", ".pythonhosted.org", ".files.pythonhosted.org"]
    )
    # The broader .pythonhosted.org wins; .files.pythonhosted.org is dropped.
    assert ".files.pythonhosted.org" not in out
    assert ".pythonhosted.org" in out
    assert ".pypi.org" in out


def test_dedup_keeps_unrelated_domains() -> None:
    out = _dedup_subdomains([".a.com", ".b.org", ".c.net"])
    assert sorted(out) == sorted([".a.com", ".b.org", ".c.net"])


def test_render_inserts_domains() -> None:
    template = "acl x dstdomain {{ALLOWED_DOMAINS}}\n"
    out = render_squid_conf(template, ["pypi.org", "*.pythonhosted.org"])
    assert "{{ALLOWED_DOMAINS}}" not in out
    assert ".pypi.org" in out
    assert ".pythonhosted.org" in out


def test_render_empty_allowlist_uses_sentinel() -> None:
    template = "acl x dstdomain {{ALLOWED_DOMAINS}}\n"
    out = render_squid_conf(template, [])
    assert ".never-allowed.invalid" in out
