import importlib.util
import json
import re
from pathlib import Path
from urllib.error import URLError

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "job-watch" / "scripts" / "job_watch.py"
spec = importlib.util.spec_from_file_location("job_watch", SCRIPT)
jw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jw)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_WATCH_HOME", str(tmp_path))


def run(argv, capsys):
    code = jw.main(argv)
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("title, expected", [
    ("Software Engineer, Backend", True), ("Software Engineer - Full stack", True),
    ("Forward Deployed Software Engineer", True), ("Applied AI Engineer", True),
    ("Member of Technical Staff, API", True), ("Product Engineer", True),
    ("Senior Software Engineer", False), ("Staff Software Engineer, Platform", False),
    ("Engineering Manager, Backend", False), ("Software Engineer Intern", False),
    ("Software Engineer III", False), ("Hardware Engineer, Silicon", False),
    ("Site Reliability Engineer", False), ("Account Executive", False),
])
def test_title_filter(title, expected):
    assert jw.title_matches(title) is expected


def test_include_and_exclude_options():
    assert jw.title_matches("Data Platform Engineer", include=re.compile("data", re.I)) is True
    assert jw.title_matches("Software Engineer, Security", exclude=re.compile("security", re.I)) is False


@pytest.mark.parametrize("location, country, mode, expected", [
    ("San Francisco, CA", None, "us", True), ("US-WA-Bellevue", None, "us", True),
    ("London | New York", None, "us", True), ("Remote", None, "us", True),
    ("Remote - Canada", None, "us", False), ("Dublin, Ireland", None, "us", False),
    ("Singapore", "SG", "us", False), ("Washington, D.C.", "US", "us", True),
    ("Dublin, Ireland", None, "any", True),
])
def test_location_filter(location, country, mode, expected):
    assert jw.location_ok(location, country, mode) is expected


FIXTURES = {
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [
        {"id": 1, "title": "Software Engineer", "location": {"name": "Seattle, WA"},
         "absolute_url": "https://example.test/1", "first_published": "2099-01-01T00:00:00Z"},
        {"id": 2, "title": "Backend Engineer", "location": {"name": "Austin, TX"},
         "absolute_url": "https://example.test/2", "first_published": "2020-01-01T00:00:00Z"},
        {"id": 3, "title": "Senior Software Engineer", "location": {"name": "Austin, TX"},
         "absolute_url": "https://example.test/3", "first_published": "2099-01-01T00:00:00Z"}]},
    "https://api.lever.co/v0/postings/beta?mode=json": [
        {"id": "x", "text": "Platform Engineer", "categories": {"location": "Remote"},
         "hostedUrl": "https://example.test/x", "createdAt": 4102444800000, "country": "US"}],
    "https://api.ashbyhq.com/posting-api/job-board/gamma": {"jobs": [
        {"id": "y", "title": "Applied AI Engineer", "location": "Tokyo", "jobUrl": "https://example.test/y",
         "publishedAt": "2099-01-01T00:00:00Z", "isListed": True},
        {"id": "z", "title": "Applied AI Engineer", "location": "New York", "jobUrl": "https://example.test/z",
         "publishedAt": "2099-01-01T00:00:00Z", "isListed": False}]},
}


@pytest.fixture
def api(monkeypatch):
    def fake(url):
        if url not in FIXTURES:
            raise ValueError(f"unexpected url {url}")
        return FIXTURES[url]
    monkeypatch.setattr(jw, "get_json", fake)
    jw.write_json("boards.json", [
        {"company": "Acme", "source": "greenhouse", "slug": "acme"},
        {"company": "Beta", "source": "lever", "slug": "beta"},
        {"company": "Gamma", "source": "ashby", "slug": "gamma"}])


def test_first_run_baseline_then_dedup(api, capsys):
    code, first = run(["check"], capsys)
    assert code == 0 and first["first_run"] and first["boards_checked"] == 3
    assert sorted(first["baselined_boards"]) == ["Acme", "Beta", "Gamma"]
    urls = {r["url"] for r in first["new_roles"]}
    assert urls == {"https://example.test/1", "https://example.test/x"}   # old posting: baseline only
    assert first["open_matching_roles"] == 3                              # senior and non-US are filtered
    _, second = run(["check"], capsys)
    assert second["new_count"] == 0 and not second["first_run"]


def test_later_unseen_role_counts_as_new_and_first_seen_never_changes(api, capsys):
    run(["check"], capsys)
    seen_before = json.loads((jw.home() / "state.json").read_text())["jobs"]
    FIXTURES["https://boards-api.greenhouse.io/v1/boards/acme/jobs"]["jobs"].append(
        {"id": 9, "title": "Full Stack Engineer", "location": {"name": "Remote"},
         "absolute_url": "https://example.test/9", "first_published": None})
    try:
        _, third = run(["check"], capsys)
    finally:
        FIXTURES["https://boards-api.greenhouse.io/v1/boards/acme/jobs"]["jobs"].pop()
    assert [r["url"] for r in third["new_roles"]] == ["https://example.test/9"]
    seen_after = json.loads((jw.home() / "state.json").read_text())["jobs"]
    key = "greenhouse:acme:1"
    assert seen_after[key]["first_seen"] == seen_before[key]["first_seen"]


def test_no_save_does_not_record(api, capsys):
    run(["check", "--no-save"], capsys)
    assert not (jw.home() / "state.json").exists()
    _, again = run(["check"], capsys)
    assert again["first_run"] and again["new_count"] == 2


def test_one_failing_board_is_reported_not_fatal(api, capsys, monkeypatch):
    real = jw.get_json
    monkeypatch.setattr(jw, "get_json", lambda u: (_ for _ in ()).throw(ValueError("boom")) if "lever" in u else real(u))
    _, out = run(["check"], capsys)
    assert out["errors"] == ["Beta: ValueError: boom"] and out["boards_checked"] == 2


def test_recent_and_details_use_saved_state(api, capsys, monkeypatch):
    run(["check"], capsys)
    _, recent = run(["recent", "--days", "1"], capsys)
    assert recent["count"] == 2                                           # the 2020 baseline posting is not "recent"
    state = json.loads((jw.home() / "state.json").read_text())["jobs"]
    assert state["greenhouse:acme:2"]["baseline"] is True
    monkeypatch.setitem(FIXTURES, "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1",
                        {"content": "&lt;p&gt;Build APIs.&lt;/p&gt;&lt;li&gt;Python&lt;/li&gt;"})
    _, details = run(["details", "https://example.test/1"], capsys)
    assert details["untrusted_text"] is True and "Build APIs." in details["description"]
    code, missing = run(["details", "https://example.test/nope"], capsys)
    assert code == 1 and "unknown url" in missing["error"]


def test_board_management(capsys):
    _, boards = run(["boards"], capsys)
    assert len(boards["boards"]) == 9
    _, added = run(["add-board", "Acme", "greenhouse", "acme"], capsys)
    assert added["boards"][-1] == {"company": "Acme", "source": "greenhouse", "slug": "acme"}
    _, removed = run(["remove-board", "acme"], capsys)
    assert all(b["company"] != "Acme" for b in removed["boards"])


def test_skill_md_matches_the_script():
    text = (SCRIPT.parents[1] / "SKILL.md").read_text()
    assert text.startswith("---\nname: job-watch\n")
    assert "SKILL_DIR/scripts/job_watch.py" in text            # referenced files are the ones copied at install
    for command in ("check", "recent", "details", "boards", "add-board", "remove-board"):
        assert f"job_watch.py {command}" in text


def test_total_failure_does_not_consume_the_baseline(api, capsys, monkeypatch):
    real = jw.get_json
    monkeypatch.setattr(jw, "get_json", lambda u: (_ for _ in ()).throw(URLError("offline")))
    _, down = run(["check"], capsys)
    assert down["boards_checked"] == 0 and len(down["errors"]) == 3 and down["saved"] is False
    monkeypatch.setattr(jw, "get_json", real)
    _, up = run(["check"], capsys)
    assert up["first_run"] and {r["url"] for r in up["new_roles"]} == {"https://example.test/1", "https://example.test/x"}


def test_a_board_that_failed_is_baselined_when_it_first_works(api, capsys, monkeypatch):
    real = jw.get_json
    monkeypatch.setattr(jw, "get_json", lambda u: (_ for _ in ()).throw(URLError("down")) if "lever" in u else real(u))
    run(["check"], capsys)                       # Acme and Gamma baselined, Beta failed
    monkeypatch.setattr(jw, "get_json", real)
    _, later = run(["check"], capsys)
    assert later["baselined_boards"] == ["Beta"]
    assert [r["url"] for r in later["new_roles"]] == ["https://example.test/x"]  # recent posting still reported once


def test_state_written_before_per_board_baselines_is_respected(api, capsys):
    run(["check"], capsys)
    state = json.loads((jw.home() / "state.json").read_text())
    state.pop("initialized_boards"); state["initialized_at"] = "2026-01-01T00:00:00+00:00"
    (jw.home() / "state.json").write_text(json.dumps(state))
    _, out = run(["check"], capsys)
    assert out["new_count"] == 0 and not out["first_run"]


def test_certificate_failures_come_with_a_fix():
    msg = jw.explain(URLError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate"))
    assert "Install Certificates" in msg and "certifi" in msg


def test_certificate_failure_falls_back_to_the_next_ca_bundle(monkeypatch):
    import io
    import ssl
    first, second = object(), object()
    monkeypatch.setattr(jw, "_contexts", lambda: [first, second])
    monkeypatch.setattr(jw, "_working_context", 0)
    seen = []

    def fake_urlopen(request, timeout, context):
        seen.append(context)
        if context is first:
            raise URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(jw, "urlopen", fake_urlopen)
    assert jw.get_json("https://example.test/x") == {"ok": True}
    assert seen == [first, second]
    assert jw.get_json("https://example.test/y") == {"ok": True}
    assert seen[-1] is second and seen.count(first) == 1      # the working bundle is remembered


def test_non_certificate_errors_are_not_retried_and_failures_are_raised(monkeypatch):
    import ssl
    monkeypatch.setattr(jw, "_contexts", lambda: [object(), object()])
    monkeypatch.setattr(jw, "_working_context", 0)
    calls = []

    def refuse(request, timeout, context):
        calls.append(context)
        raise URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(jw, "urlopen", refuse)
    with pytest.raises(URLError):
        jw.get_json("https://example.test/z")
    assert len(calls) == 1
    monkeypatch.setattr(jw, "urlopen", lambda r, timeout, context: (_ for _ in ()).throw(
        URLError(ssl.SSLCertVerificationError("bad"))))
    with pytest.raises(URLError):
        jw.get_json("https://example.test/z")
