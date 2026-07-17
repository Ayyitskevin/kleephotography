"""Trust-boundary tests for canonical transport and public proof provenance."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _config_probe(base_url, canonical_redirects=None):
    env = os.environ.copy()
    env["MISE_BASE_URL"] = base_url
    env["MISE_ENV_FILE"] = "/nonexistent"
    env.pop("MISE_COOKIE_SECURE", None)
    if canonical_redirects is None:
        env.pop("MISE_CANONICAL_REDIRECTS", None)
    else:
        env["MISE_CANONICAL_REDIRECTS"] = canonical_redirects
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from app import config; "
            "print(config.BASE_URL, config.CANONICAL_REDIRECTS, config.COOKIE_SECURE)",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("base_url", "expected"),
    (
        ("http://LOCALHOST:80/", "http://localhost False False"),
        (" HTTPS://KleePhotography.COM:443/ ", "https://kleephotography.com False True"),
        ("https://kleephotography.com:8443", "https://kleephotography.com:8443 False True"),
    ),
)
@pytest.mark.unit
def test_base_url_is_normalized_and_redirects_are_opt_in(base_url, expected):
    result = _config_probe(base_url)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.unit
def test_canonical_redirect_can_be_explicitly_enabled():
    result = _config_probe("https://kleephotography.com", "true")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://kleephotography.com True True"


@pytest.mark.parametrize(
    "base_url",
    (
        "not-a-url",
        "ftp://kleephotography.com",
        "https://user:secret@kleephotography.com",
        "https://kleephotography.com/path",
        "https://kleephotography.com?query=1",
        "https://kleephotography.com:invalid",
    ),
)
@pytest.mark.unit
def test_invalid_base_url_fails_before_startup(base_url):
    result = _config_probe(base_url)
    assert result.returncode != 0
    assert "MISE_BASE_URL" in result.stderr


@pytest.mark.unit
def test_active_sources_cannot_publish_retired_prototype_proof():
    root = Path(__file__).resolve().parent.parent
    assert not (root / "app" / "bootstrap.py").exists()

    retired_scripts = (
        root / "scripts" / "seed-showcase-flow.sh",
        root / "scripts" / "seed-real-showcase-flow.sh",
    )
    for script in retired_scripts:
        source = script.read_text()
        assert "exit 1" in source
        assert "ssh " not in source
        assert "curl " not in source

    active_text = "\n".join(
        path.read_text(errors="ignore")
        for source_root in (root / "app", root / "scripts", root / "templates")
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".sh", ".html"}
    )
    for retired_marker in (
        "A tasting menu, shot at its peak.",
        "Our reservations jumped the week",
        "Fastest turnaround we have ever had",
        "full menu refresh between lunch and dinner service",
    ):
        assert retired_marker not in active_text


@pytest.mark.integration
def test_retirement_migration_only_unpublishes_exact_prototype_proof():
    migration = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "068_retire_public_showcase_seed.sql"
    ).read_text()
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE testimonials (
            quote TEXT, attribution_name TEXT, business TEXT, published INTEGER
        );
        CREATE TABLE galleries (
            id INTEGER PRIMARY KEY, title TEXT, client_name TEXT, cs_tagline TEXT,
            cs_brief TEXT, cs_credits TEXT, cs_published INTEGER
        );
        INSERT INTO testimonials VALUES
          ('Our reservations jumped the week the new photos went live. Kevin made the food look exactly like the room feels.',
           'Edited attribution', 'Edited business', 1),
          ('Fastest turnaround we have ever had, and the social crops mean our marketing person stopped re-cropping everything by hand.',
           'Marketing lead', 'Neighborhood cafe', 1),
          ('He shot a full menu refresh between lunch and dinner service without ever getting in the way. Rare.',
           'Executive chef', 'Chef-owned dining room', 1),
          ('Verified client quote', 'Real Client', 'Real Business', 1);
        INSERT INTO galleries VALUES
          (42, 'Real Restaurant Launch', 'Named Client',
           'A tasting menu, shot at its peak.',
           'A full menu refresh and brand library in a single service window — plating, pours, and the dining room, delivered as a same-week gallery with social crops baked in.',
           'Client: Independent restaurant
Scope: Menu refresh · brand library
Deliverables: 6 finals · social crop pack
Turnaround: Same-week gallery', 1),
          (2, 'Real case study', 'Real Client', 'Real result', 'Verified brief',
           'Verified credits', 1);
        """
    )

    con.execute(
        "UPDATE galleries SET cs_credits=replace(cs_credits, char(10), char(13)||char(10)) "
        "WHERE id=42"
    )
    con.executescript(migration)

    proof = con.execute(
        "SELECT attribution_name, published FROM testimonials ORDER BY attribution_name"
    ).fetchall()
    assert [(row["attribution_name"], row["published"]) for row in proof] == [
        ("Edited attribution", 0),
        ("Executive chef", 0),
        ("Marketing lead", 0),
        ("Real Client", 1),
    ]
    studies = con.execute("SELECT id, cs_published FROM galleries ORDER BY id").fetchall()
    assert [(row["id"], row["cs_published"]) for row in studies] == [(2, 1), (42, 0)]


@pytest.mark.integration
def test_clean_install_finishes_with_prototype_proof_unpublished(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "mise.db")
    db.migrate()

    rows = db.all_(
        """SELECT attribution_name, published FROM testimonials
           WHERE attribution_name IN ('Restaurant owner', 'Marketing lead', 'Executive chef')
           ORDER BY attribution_name"""
    )
    assert [(row["attribution_name"], row["published"]) for row in rows] == [
        ("Executive chef", 0),
        ("Marketing lead", 0),
        ("Restaurant owner", 0),
    ]
    assert db.one(
        "SELECT 1 AS applied FROM schema_migrations WHERE name=?",
        ("068_retire_public_showcase_seed.sql",),
    )


@pytest.mark.integration
def test_startup_does_not_publish_unverified_showcase():
    from app import db

    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        ("truth-startup", "Unverified gallery", "1234"),
    )
    aid = db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status, portfolio)
           VALUES (?,?,?,?,?,0)""",
        (gid, "photo", "unverified.jpg", "unverified.jpg", "ready"),
    )
    before = db.one("SELECT COUNT(*) AS n FROM testimonials")["n"]

    with TestClient(app):
        pass

    gallery = db.one("SELECT title, cs_published FROM galleries WHERE id=?", (gid,))
    asset = db.one("SELECT portfolio FROM assets WHERE id=?", (aid,))
    assert gallery["title"] == "Unverified gallery" and gallery["cs_published"] == 0
    assert asset["portfolio"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM testimonials")["n"] == before


@pytest.mark.integration
def test_canonical_origin_redirects_browser_routes(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "BASE_URL", "https://kleephotography.com")
    monkeypatch.setattr(config, "CANONICAL_REDIRECTS", True)
    monkeypatch.setattr(config, "COOKIE_SECURE", True)

    expected = {
        "http://kleephotography.com/contact?from=http": (
            "https://kleephotography.com/contact?from=http"
        ),
        "https://www.kleephotography.com/contact?from=www": (
            "https://kleephotography.com/contact?from=www"
        ),
        "http://www.kleephotography.com/site/img/a%2Fb/%25/%E2%9C%93//?tag=a%2Fb&tag=&empty=": (
            "https://kleephotography.com/site/img/a%2Fb/%25/%E2%9C%93//?tag=a%2Fb&tag=&empty="
        ),
    }
    for url, location in expected.items():
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 308
        assert response.headers["location"] == location
        assert response.headers["cache-control"] == "no-store"

    canonical = client.get("https://kleephotography.com/contact", follow_redirects=False)
    assert canonical.status_code == 200
    assert canonical.headers["strict-transport-security"] == "max-age=300"

    default_port = client.get("https://kleephotography.com:443/contact", follow_redirects=False)
    assert default_port.status_code == 200


@pytest.mark.integration
def test_canonical_static_redirect_is_not_cached_as_an_asset(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "BASE_URL", "https://kleephotography.com")
    monkeypatch.setattr(config, "CANONICAL_REDIRECTS", True)

    redirected = client.get(
        "http://www.kleephotography.com/static/site.js?v=1", follow_redirects=False
    )
    assert redirected.status_code == 308
    assert redirected.headers["cache-control"] == "no-store"
    assert "immutable" not in redirected.headers["cache-control"]

    canonical = client.get("https://kleephotography.com/static/site.js?v=1", follow_redirects=False)
    assert canonical.status_code == 200
    assert canonical.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.integration
def test_malformed_host_redirects_to_fixed_canonical_origin(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "BASE_URL", "https://kleephotography.com")
    monkeypatch.setattr(config, "CANONICAL_REDIRECTS", True)

    response = client.get(
        "http://kleephotography.com/contact",
        headers={"host": "kleephotography.com:99999"},
        follow_redirects=False,
    )

    assert response.status_code == 308
    assert response.headers["location"] == "https://kleephotography.com/contact"


@pytest.mark.parametrize("peer", ("127.0.0.1", "::1"))
@pytest.mark.integration
def test_trusted_loopback_proxy_reconstructs_canonical_https(peer, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "BASE_URL", "https://kleephotography.com")
    monkeypatch.setattr(config, "CANONICAL_REDIRECTS", True)
    proxied_app = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1,::1")

    with TestClient(
        proxied_app,
        base_url="http://kleephotography.com",
        client=(peer, 43210),
    ) as proxied:
        response = proxied.get(
            "/contact",
            headers={
                "host": "kleephotography.com:443",
                "x-forwarded-proto": "https",
            },
            follow_redirects=False,
        )

    assert response.status_code == 200
    assert "FORWARDED_ALLOW_IPS=127.0.0.1,::1" in Path(".env.example").read_text()


@pytest.mark.integration
def test_forwarded_host_is_not_trusted_as_a_host_rewrite(monkeypatch):
    """Uvicorn reconstructs scheme, but cloudflared must preserve Host itself."""
    from app import config

    monkeypatch.setattr(config, "BASE_URL", "https://kleephotography.com")
    monkeypatch.setattr(config, "CANONICAL_REDIRECTS", True)
    proxied_app = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1,::1")

    with TestClient(
        proxied_app,
        base_url="http://flow:8400",
        client=("127.0.0.1", 43210),
    ) as proxied:
        response = proxied.get(
            "/contact",
            headers={
                "host": "flow:8400",
                "x-forwarded-host": "kleephotography.com",
                "x-forwarded-proto": "https",
            },
            follow_redirects=False,
        )

    assert response.status_code == 308
    assert response.headers["location"] == "https://kleephotography.com/contact"


@pytest.mark.integration
def test_untrusted_peer_cannot_spoof_forwarded_scheme(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "BASE_URL", "https://kleephotography.com")
    monkeypatch.setattr(config, "CANONICAL_REDIRECTS", True)
    proxied_app = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1,::1")

    with TestClient(
        proxied_app,
        base_url="http://kleephotography.com",
        client=("203.0.113.10", 43210),
    ) as proxied:
        response = proxied.get(
            "/contact",
            headers={"host": "kleephotography.com", "x-forwarded-proto": "https"},
            follow_redirects=False,
        )

    assert response.status_code == 308
    assert response.headers["location"] == "https://kleephotography.com/contact"


@pytest.mark.integration
def test_noncanonical_contact_post_is_not_processed(client, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "BASE_URL", "https://kleephotography.com")
    monkeypatch.setattr(config, "CANONICAL_REDIRECTS", True)
    before = db.one("SELECT COUNT(*) AS n FROM inquiries")["n"]

    response = client.post(
        "http://kleephotography.com/contact",
        data={"name": "Redirect Test", "email": "redirect@example.com", "message": "hello"},
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )

    assert response.status_code == 421
    assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert db.one("SELECT COUNT(*) AS n FROM inquiries")["n"] == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "origin",
    ("https://evil.example", "null", "https://kleephotography.com:not-a-port", "https://["),
)
def test_canonical_contact_rejects_untrusted_or_opaque_origin(client, monkeypatch, origin):
    from app import config, db

    monkeypatch.setattr(config, "BASE_URL", "https://kleephotography.com")
    monkeypatch.setattr(config, "CANONICAL_REDIRECTS", True)
    before = db.one("SELECT COUNT(*) AS n FROM inquiries")["n"]

    response = client.post(
        "https://kleephotography.com/contact",
        data={"name": "CSRF Test", "email": "csrf@example.com", "message": "hello"},
        headers={"origin": origin},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "cross-origin request blocked"}
    assert db.one("SELECT COUNT(*) AS n FROM inquiries")["n"] == before


@pytest.mark.integration
def test_private_origin_health_and_service_api_bypass_redirect(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "BASE_URL", "https://kleephotography.com")
    monkeypatch.setattr(config, "CANONICAL_REDIRECTS", True)

    assert client.get("http://flow:8400/healthz", follow_redirects=False).status_code == 200
    assert client.get("http://flow:8400/api/shots", follow_redirects=False).status_code == 503
