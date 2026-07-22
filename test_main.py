"""Tests for the NY Times Article Search loader in main.py.

The suite runs fully offline: ``requests.Session`` is replaced with a
``FakeSession`` that replays canned responses (and records every request),
and ``time.sleep`` is stubbed out so the retry tests don't actually wait.
"""

import argparse
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import main
from main import NYTimesSource, flatten_dict

# The tests only need *some* opaque token to assert the key is sent as a
# request param and never leaked into error messages; generating it keeps
# credential-looking literals out of the repo.
API_KEY = f"dummy-{uuid.uuid4()}"
UTC = timezone.utc

STATIC_SCHEMA = [
    "title",
    "body",
    "created_at",
    "id",
    "summary",
    "abstract",
    "keywords",
]


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.headers = headers or {}
        self.text = json.dumps(self._payload) if text is None else text

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


class FakeSession:
    """Stand-in for requests.Session that replays canned responses in order.

    Raises if the code under test makes more requests than were canned, so
    tests fail loudly on unexpected extra API calls.
    """

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None, timeout=None):
        assert not self.closed, "request made on a closed session"
        self.calls.append(
            {"url": url, "params": dict(params or {}), "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError("FakeSession ran out of canned responses")
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def pub_date(offset_minutes=0):
    """An NYT-style timestamp; larger offsets are further in the past."""
    base = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    stamp = base - timedelta(minutes=offset_minutes)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S+0000")


def make_doc(i, **overrides):
    """A realistic-enough NYT article document; docs sort newest-first by i."""
    doc = {
        "_id": f"nyt://article/{i}",
        "headline": {"main": f"Headline {i}"},
        "pub_date": pub_date(i),
        "keywords": [{"name": "subject", "value": f"Topic {i}", "rank": 1}],
    }
    doc.update(overrides)
    return doc


def page_response(docs, hits=None, meta=True):
    """A FakeResponse shaped like a successful Article Search API reply."""
    body = {"docs": docs}
    if meta:
        body["meta"] = {"hits": len(docs) if hits is None else hits}
    return FakeResponse(payload={"status": "OK", "response": body})


@pytest.fixture
def source():
    src = NYTimesSource()
    src.args = argparse.Namespace(api_key=API_KEY, query="Silicon Valley")
    return src


@pytest.fixture
def fake_api(monkeypatch):
    """Patch requests.Session so connect()/auto-connect use a FakeSession."""

    def install(*responses):
        fake = FakeSession(responses)
        monkeypatch.setattr(main.requests, "Session", lambda: fake)
        return fake

    return install


@pytest.fixture
def sleeps(monkeypatch):
    """Capture time.sleep() calls instead of actually sleeping."""
    calls = []
    monkeypatch.setattr(main.time, "sleep", calls.append)
    return calls


# ---------------------------------------------------------------------------
# flatten_dict
# ---------------------------------------------------------------------------


class TestFlattenDict:
    def test_flat_dict_is_unchanged(self):
        assert flatten_dict({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}

    def test_nested_dicts_use_dotted_keys(self):
        assert flatten_dict({"headline": {"main": "Title", "kicker": "K"}}) == {
            "headline.main": "Title",
            "headline.kicker": "K",
        }

    def test_lists_use_index_keys(self):
        assert flatten_dict({"tags": ["a", "b"]}) == {
            "tags.0": "a",
            "tags.1": "b",
        }

    def test_lists_of_dicts(self):
        flat = flatten_dict(
            {"keywords": [{"value": "Tech"}, {"value": "Politics"}]}
        )
        assert flat == {
            "keywords.0.value": "Tech",
            "keywords.1.value": "Politics",
        }

    def test_deep_mixed_nesting(self):
        flat = flatten_dict({"a": {"b": [{"c": [1, 2]}]}})
        assert flat == {"a.b.0.c.0": 1, "a.b.0.c.1": 2}

    def test_empty_dict_value_is_preserved(self):
        assert flatten_dict({"a": {}}) == {"a": {}}

    def test_empty_list_value_is_preserved(self):
        assert flatten_dict({"a": []}) == {"a": []}

    def test_none_and_scalar_values_are_preserved(self):
        flat = flatten_dict({"a": None, "b": 0, "c": False, "d": 1.5})
        assert flat == {"a": None, "b": 0, "c": False, "d": 1.5}

    def test_custom_separator(self):
        assert flatten_dict({"a": {"b": 1}}, sep="/") == {"a/b": 1}

    def test_non_string_keys_are_stringified(self):
        assert flatten_dict({1: {2: "x"}}) == {"1.2": "x"}

    def test_degenerate_top_level_inputs(self):
        # Non-document inputs collapse onto the empty key (current behavior).
        assert flatten_dict({}) == {"": {}}
        assert flatten_dict([]) == {"": []}
        assert flatten_dict("scalar") == {"": "scalar"}

    def test_realistic_nyt_document(self):
        doc = {
            "_id": "nyt://article/abc",
            "headline": {"main": "Title", "kicker": None},
            "keywords": [
                {"name": "subject", "value": "Tech", "rank": 1},
                {"name": "glocations", "value": "California", "rank": 2},
            ],
            "multimedia": [],
            "byline": {"original": "By J. Doe", "person": [{"firstname": "J."}]},
            "pub_date": "2026-07-21T12:00:00+0000",
        }
        flat = flatten_dict(doc)
        assert flat["_id"] == "nyt://article/abc"
        assert flat["headline.main"] == "Title"
        assert flat["headline.kicker"] is None
        assert flat["keywords.1.value"] == "California"
        assert flat["multimedia"] == []
        assert flat["byline.person.0.firstname"] == "J."
        assert flat["pub_date"] == "2026-07-21T12:00:00+0000"

    def test_flattening_is_lossless_for_scalars(self):
        # Every scalar leaf of the original document must survive flattening.
        doc = make_doc(7)
        flat = flatten_dict(doc)
        assert set(flat.values()) >= {
            "nyt://article/7",
            "Headline 7",
            "Topic 7",
            1,
        }


# ---------------------------------------------------------------------------
# NYTimesSource._parse_datetime
# ---------------------------------------------------------------------------


class TestParseDatetime:
    EXPECTED = datetime(2026, 7, 21, 12, 34, 56, tzinfo=UTC)

    @pytest.mark.parametrize(
        "value",
        [
            "2026-07-21T12:34:56+0000",  # NYT's compact offset
            "2026-07-21T12:34:56Z",
            "2026-07-21T12:34:56+00:00",
            "2026-07-21T07:34:56-0500",  # compact negative offset
            "2026-07-21T18:04:56+05:30",  # colon offset must not be mangled
            "2026-07-21T12:34:56",  # naive -> assumed UTC
            "  2026-07-21T12:34:56Z  ",  # surrounding whitespace
        ],
    )
    def test_valid_formats_normalize_to_utc_instant(self, value):
        parsed = NYTimesSource._parse_datetime(value)
        assert parsed == self.EXPECTED
        assert parsed.tzinfo is not None

    def test_date_only_becomes_utc_midnight(self):
        parsed = NYTimesSource._parse_datetime("2026-07-21")
        assert parsed == datetime(2026, 7, 21, tzinfo=UTC)

    @pytest.mark.parametrize("value", [None, ""])
    def test_missing_values_return_none(self, value):
        assert NYTimesSource._parse_datetime(value) is None

    @pytest.mark.parametrize("value", ["not-a-date", "2026-13-45T99:99:99Z"])
    def test_invalid_values_return_none_and_warn(self, value, caplog):
        with caplog.at_level(logging.WARNING):
            assert NYTimesSource._parse_datetime(value) is None
        assert "Could not parse timestamp" in caplog.text

    def test_offsets_compare_as_the_same_instant(self):
        east = NYTimesSource._parse_datetime("2026-07-21T07:34:56-0500")
        utc = NYTimesSource._parse_datetime("2026-07-21T12:34:56+0000")
        assert east == utc


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    def test_connect_stores_state_and_opens_session(self, source, fake_api):
        fake = fake_api()
        source.connect(inc_column="pub_date", max_inc_value=pub_date(0))
        assert source.session is fake
        assert source.inc_column == "pub_date"
        assert source.max_inc_value == pub_date(0)

    def test_connect_without_incremental_column(self, source, fake_api):
        fake_api()
        source.connect()
        assert source.inc_column is None
        assert source.max_inc_value is None

    def test_connect_rejects_unsupported_inc_column(self, source):
        with pytest.raises(ValueError, match="pub_date"):
            source.connect(inc_column="updated_at")

    def test_disconnect_closes_session_and_is_idempotent(self, source, fake_api):
        fake = fake_api()
        source.connect()
        source.disconnect()
        assert fake.closed
        assert source.session is None
        source.disconnect()  # second call must not blow up


# ---------------------------------------------------------------------------
# NYTimesSource._request (retries, auth, error handling)
# ---------------------------------------------------------------------------


class TestRequest:
    def test_returns_response_payload(self, source, fake_api):
        fake_api(FakeResponse(payload={"response": {"docs": [1, 2]}}))
        source.connect()
        assert source._request({"page": 0}) == {"docs": [1, 2]}

    @pytest.mark.parametrize(
        "payload", [{}, {"status": "OK"}, {"response": None}]
    )
    def test_missing_or_null_response_key_yields_empty_dict(
        self, source, fake_api, payload
    ):
        fake_api(FakeResponse(payload=payload))
        source.connect()
        assert source._request({"page": 0}) == {}

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_errors_raise_immediately_without_retry(
        self, source, fake_api, sleeps, status
    ):
        fake = fake_api(FakeResponse(status_code=status))
        source.connect()
        with pytest.raises(RuntimeError, match="authentication failed") as exc:
            source._request({"page": 0})
        assert str(status) in str(exc.value)
        assert "NYTIMES_API_KEY" in str(exc.value)  # actionable hint
        assert len(fake.calls) == 1
        assert sleeps == []

    def test_retries_on_429_honoring_retry_after(self, source, fake_api, sleeps):
        fake = fake_api(
            FakeResponse(status_code=429, headers={"Retry-After": "3"}),
            page_response([make_doc(0)]),
        )
        source.connect()
        result = source._request({"page": 0})
        assert result["docs"][0]["_id"] == "nyt://article/0"
        assert len(fake.calls) == 2
        assert sleeps == [3.0]

    def test_retries_on_429_with_default_wait(self, source, fake_api, sleeps):
        fake_api(FakeResponse(status_code=429), page_response([]))
        source.connect()
        source._request({"page": 0})
        assert sleeps == [main.RETRY_WAIT_SECONDS]

    def test_invalid_retry_after_falls_back_to_default(
        self, source, fake_api, sleeps
    ):
        fake_api(
            FakeResponse(status_code=429, headers={"Retry-After": "soon"}),
            page_response([]),
        )
        source.connect()
        source._request({"page": 0})
        assert sleeps == [main.RETRY_WAIT_SECONDS]

    def test_retries_on_server_errors(self, source, fake_api, sleeps):
        fake = fake_api(
            FakeResponse(status_code=500),
            FakeResponse(status_code=503),
            page_response([]),
        )
        source.connect()
        assert source._request({"page": 0}) == {"docs": [], "meta": {"hits": 0}}
        assert len(fake.calls) == 3
        assert len(sleeps) == 2

    def test_gives_up_after_max_retries(self, source, fake_api, sleeps):
        fake = fake_api(
            *[FakeResponse(status_code=429) for _ in range(main.MAX_RETRIES)]
        )
        source.connect()
        with pytest.raises(RuntimeError, match="kept failing") as exc:
            source._request({"page": 0})
        assert "429" in str(exc.value)
        assert len(fake.calls) == main.MAX_RETRIES
        # No sleep after the final attempt.
        assert len(sleeps) == main.MAX_RETRIES - 1

    def test_client_error_raises_with_truncated_body(self, source, fake_api):
        fake_api(FakeResponse(status_code=400, text="E" * 500))
        source.connect()
        with pytest.raises(RuntimeError, match="HTTP 400") as exc:
            source._request({"page": 0})
        assert "E" * 200 in str(exc.value)
        assert "E" * 201 not in str(exc.value)

    def test_error_messages_never_leak_the_api_key(self, source, fake_api):
        fake_api(FakeResponse(status_code=400, text="Bad Request"))
        source.connect()
        with pytest.raises(RuntimeError) as exc:
            source._request({"api-key": API_KEY, "page": 0})
        assert API_KEY not in str(exc.value)

    def test_auto_creates_session_without_connect(self, source, fake_api):
        fake = fake_api(page_response([]))
        assert source.session is None  # connect() never called
        source._request({"page": 0})
        assert source.session is fake


# ---------------------------------------------------------------------------
# NYTimesSource._fetch_page
# ---------------------------------------------------------------------------


class TestFetchPage:
    def test_sends_expected_params_and_transport(self, source, fake_api):
        fake = fake_api(page_response([]))
        source.connect()
        source._fetch_page(3, begin_date="20260701")
        call = fake.calls[0]
        assert call["url"] == main.API_ENDPOINT
        assert call["timeout"] == main.REQUEST_TIMEOUT_SECONDS
        assert call["params"] == {
            "q": "Silicon Valley",
            "api-key": API_KEY,
            "page": 3,
            "sort": "newest",
            "begin_date": "20260701",
        }

    def test_begin_date_is_omitted_when_not_set(self, source, fake_api):
        fake = fake_api(page_response([]))
        source.connect()
        source._fetch_page(0)
        assert "begin_date" not in fake.calls[0]["params"]


# ---------------------------------------------------------------------------
# NYTimesSource._iter_docs (pagination + incremental cut-off)
# ---------------------------------------------------------------------------


class TestIterDocs:
    def test_single_short_page(self, source, fake_api):
        docs = [make_doc(i) for i in range(3)]
        fake = fake_api(page_response(docs))
        source.connect()
        assert list(source._iter_docs()) == docs
        assert len(fake.calls) == 1

    def test_paginates_until_short_page(self, source, fake_api):
        page0 = [make_doc(i) for i in range(10)]
        page1 = [make_doc(10 + i) for i in range(4)]
        fake = fake_api(page_response(page0, hits=14), page_response(page1, hits=14))
        source.connect()
        result = list(source._iter_docs())
        assert result == page0 + page1
        assert len(fake.calls) == 2
        assert [c["params"]["page"] for c in fake.calls] == [0, 1]

    def test_stops_when_hits_are_exhausted(self, source, fake_api):
        # Two exactly-full pages, hits=20: no wasted third request.
        pages = [
            page_response([make_doc(i) for i in range(10)], hits=20),
            page_response([make_doc(10 + i) for i in range(10)], hits=20),
        ]
        fake = fake_api(*pages)
        source.connect()
        assert len(list(source._iter_docs())) == 20
        assert len(fake.calls) == 2

    def test_stops_at_the_api_page_cap(self, source, fake_api):
        # Every page is full and hits claims more, but the API rejects
        # page > MAX_PAGE, so paging must stop after MAX_PAGE + 1 pages.
        pages = [
            page_response(
                [make_doc(page * 10 + i) for i in range(10)], hits=99999
            )
            for page in range(main.MAX_PAGE + 1)
        ]
        fake = fake_api(*pages)
        source.connect()
        docs = list(source._iter_docs())
        assert len(docs) == (main.MAX_PAGE + 1) * main.PAGE_SIZE
        assert len(fake.calls) == main.MAX_PAGE + 1

    @pytest.mark.parametrize("docs", [[], None])
    def test_empty_or_null_docs_yield_nothing(self, source, fake_api, docs):
        fake = fake_api(
            FakeResponse(payload={"response": {"docs": docs, "meta": {"hits": 0}}})
        )
        source.connect()
        assert list(source._iter_docs()) == []
        assert len(fake.calls) == 1

    def test_missing_meta_is_tolerated(self, source, fake_api):
        docs = [make_doc(i) for i in range(3)]
        fake_api(page_response(docs, meta=False))
        source.connect()
        assert list(source._iter_docs()) == docs

    def test_incremental_run_sends_begin_date(self, source, fake_api):
        fake = fake_api(page_response([]))
        source.connect(
            inc_column="pub_date", max_inc_value="2026-07-20T15:30:00+0000"
        )
        list(source._iter_docs())
        assert fake.calls[0]["params"]["begin_date"] == "20260720"

    def test_incremental_stops_at_cutoff_mid_page(self, source, fake_api):
        cutoff = pub_date(60)
        docs = [
            make_doc(0, pub_date=pub_date(0)),  # newer -> yielded
            make_doc(1, pub_date=pub_date(30)),  # newer -> yielded
            make_doc(2, pub_date=cutoff),  # equal -> already loaded, stop
            make_doc(3, pub_date=pub_date(90)),  # older -> never reached
        ]
        fake = fake_api(page_response(docs, hits=1000))
        source.connect(inc_column="pub_date", max_inc_value=cutoff)
        result = list(source._iter_docs())
        assert [d["_id"] for d in result] == ["nyt://article/0", "nyt://article/1"]
        # The cut-off ends iteration: no second page is requested.
        assert len(fake.calls) == 1

    def test_incremental_yields_docs_with_unparseable_pub_date(
        self, source, fake_api
    ):
        docs = [
            make_doc(0, pub_date=pub_date(0)),
            make_doc(1, pub_date="garbage"),  # can't compare -> keep it
            make_doc(2, pub_date=None),  # missing -> keep it
            make_doc(3, pub_date=pub_date(120)),  # older -> stop here
        ]
        fake_api(page_response(docs, hits=1000))
        source.connect(inc_column="pub_date", max_inc_value=pub_date(60))
        result = list(source._iter_docs())
        assert [d["_id"] for d in result] == [
            "nyt://article/0",
            "nyt://article/1",
            "nyt://article/2",
        ]


# ---------------------------------------------------------------------------
# NYTimesSource._update_checkpoint
# ---------------------------------------------------------------------------


class TestUpdateCheckpoint:
    def test_sets_the_first_value(self, source):
        source._update_checkpoint(pub_date(0))
        assert source.max_inc_value == pub_date(0)

    def test_advances_on_newer_and_keeps_on_older(self, source):
        source.max_inc_value = pub_date(60)
        source._update_checkpoint(pub_date(0))  # newer
        assert source.max_inc_value == pub_date(0)
        source._update_checkpoint(pub_date(120))  # older
        assert source.max_inc_value == pub_date(0)

    def test_equal_value_does_not_replace(self, source):
        source.max_inc_value = pub_date(0)
        source._update_checkpoint(pub_date(0))
        assert source.max_inc_value == pub_date(0)

    @pytest.mark.parametrize("value", [None, "", "garbage"])
    def test_ignores_missing_or_unparseable_values(self, source, value):
        source.max_inc_value = pub_date(0)
        source._update_checkpoint(value)
        assert source.max_inc_value == pub_date(0)

    def test_replaces_an_unparseable_current_value(self, source):
        source.max_inc_value = "garbage"
        source._update_checkpoint(pub_date(0))
        assert source.max_inc_value == pub_date(0)

    def test_stores_the_raw_string_not_a_datetime(self, source):
        source._update_checkpoint("2026-07-21T12:00:00+0000")
        assert source.max_inc_value == "2026-07-21T12:00:00+0000"


# ---------------------------------------------------------------------------
# NYTimesSource.getDataBatch
# ---------------------------------------------------------------------------


class TestGetDataBatch:
    @pytest.mark.parametrize("size", [0, -1])
    def test_rejects_batch_size_below_one(self, source, size):
        gen = source.getDataBatch(size)
        with pytest.raises(ValueError, match="batch_size"):
            next(gen)

    def test_rebatches_pages_into_requested_size(self, source, fake_api):
        # 25 docs across three API pages, re-chunked into batches of 8.
        fake_api(
            page_response([make_doc(i) for i in range(10)], hits=25),
            page_response([make_doc(10 + i) for i in range(10)], hits=25),
            page_response([make_doc(20 + i) for i in range(5)], hits=25),
        )
        source.connect()
        batches = list(source.getDataBatch(8))
        assert [len(b) for b in batches] == [8, 8, 8, 1]
        ids = [d["_id"] for b in batches for d in b]
        assert ids == [f"nyt://article/{i}" for i in range(25)]

    def test_batch_size_larger_than_result_set(self, source, fake_api):
        fake_api(page_response([make_doc(i) for i in range(3)]))
        source.connect()
        batches = list(source.getDataBatch(50))
        assert [len(b) for b in batches] == [3]

    def test_documents_are_flattened(self, source, fake_api):
        fake_api(page_response([make_doc(0)]))
        source.connect()
        (batch,) = source.getDataBatch(10)
        item = batch[0]
        assert item["headline.main"] == "Headline 0"
        assert item["keywords.0.value"] == "Topic 0"
        assert "headline" not in item  # no nested values remain

    def test_tracks_seen_keys_and_advances_checkpoint(self, source, fake_api):
        fake_api(page_response([make_doc(0), make_doc(1)]))
        source.connect()
        list(source.getDataBatch(10))
        assert {"_id", "headline.main", "pub_date"} <= source._seen_keys
        # Newest pub_date seen (doc 0, newest-first) becomes the checkpoint.
        assert source.max_inc_value == pub_date(0)

    def test_no_results_yield_no_batches(self, source, fake_api):
        fake_api(page_response([]))
        source.connect()
        assert list(source.getDataBatch(10)) == []


# ---------------------------------------------------------------------------
# NYTimesSource.getSchema
# ---------------------------------------------------------------------------


class TestGetSchema:
    def test_schema_from_loaded_docs_without_api_call(self, source, fake_api):
        fake = fake_api(page_response([make_doc(0)]))
        source.connect()
        list(source.getDataBatch(10))
        calls_before = len(fake.calls)
        schema = source.getSchema()
        assert schema == sorted(flatten_dict(make_doc(0)))
        assert len(fake.calls) == calls_before  # derived from seen keys only

    def test_schema_is_union_across_documents(self, source, fake_api):
        fake_api(
            page_response([make_doc(0), make_doc(1, extra_field="x")])
        )
        source.connect()
        list(source.getDataBatch(10))
        schema = source.getSchema()
        assert "extra_field" in schema
        assert schema == sorted(schema)

    def test_schema_from_sample_page_when_nothing_loaded(self, source, fake_api):
        fake = fake_api(page_response([make_doc(0)]))
        source.connect()
        schema = source.getSchema()
        assert schema == sorted(flatten_dict(make_doc(0)))
        assert len(fake.calls) == 1
        assert fake.calls[0]["params"]["page"] == 0

    def test_static_fallback_when_api_unreachable(self, source, caplog):
        class ExplodingSession:
            def get(self, *args, **kwargs):
                raise main.requests.ConnectionError("network down")

        source.session = ExplodingSession()
        with caplog.at_level(logging.WARNING):
            schema = source.getSchema()
        assert schema == STATIC_SCHEMA
        assert "falling back" in caplog.text

    def test_static_fallback_when_api_returns_no_docs(self, source, fake_api):
        fake_api(page_response([]))
        source.connect()
        assert source.getSchema() == STATIC_SCHEMA


# ---------------------------------------------------------------------------
# End-to-end flows (mirrors the __main__ demo)
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_demo_flow(self, source, fake_api):
        fake_api(
            page_response([make_doc(i) for i in range(10)], hits=13),
            page_response([make_doc(10 + i) for i in range(3)], hits=13),
        )
        source.connect()
        try:
            batches = list(source.getDataBatch(10))
            # Every item exposes the fields the demo prints.
            for batch in batches:
                for item in batch:
                    assert item["_id"].startswith("nyt://article/")
                    assert item["headline.main"].startswith("Headline")
            schema = source.getSchema()
            assert "headline.main" in schema
        finally:
            source.disconnect()
        assert source.session is None

    def test_incremental_rerun_only_returns_new_docs(self, source, fake_api):
        # First run: full load of 3 articles.
        fake_api(page_response([make_doc(i) for i in range(3)]))
        source.connect(inc_column="pub_date", max_inc_value=None)
        first_run = [d for b in source.getDataBatch(10) for d in b]
        assert len(first_run) == 3
        checkpoint = source.max_inc_value
        assert checkpoint == pub_date(0)
        source.disconnect()

        # Second run resumes from the persisted checkpoint. The API now has
        # two newer articles; the newest previously-loaded one still appears
        # first in the (newest-first) results and must cut the run short.
        rerun = NYTimesSource()
        rerun.args = source.args
        fake = fake_api(
            page_response(
                [
                    make_doc(101, pub_date=pub_date(-2)),
                    make_doc(100, pub_date=pub_date(-1)),
                    make_doc(0, pub_date=pub_date(0)),  # == checkpoint
                    make_doc(1, pub_date=pub_date(1)),
                ],
                hits=1000,
            )
        )
        rerun.connect(inc_column="pub_date", max_inc_value=checkpoint)
        second_run = [d for b in rerun.getDataBatch(10) for d in b]
        assert [d["_id"] for d in second_run] == [
            "nyt://article/101",
            "nyt://article/100",
        ]
        assert fake.calls[0]["params"]["begin_date"] == "20260721"
        # The checkpoint advanced to the newest article of the second run.
        assert rerun.max_inc_value == pub_date(-2)
        rerun.disconnect()
