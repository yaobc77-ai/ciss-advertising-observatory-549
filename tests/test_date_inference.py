"""Inferred publication dates: derivation, opt-in use and server-written disclosure."""

from datetime import date
from types import SimpleNamespace

from test_research_tools import make_catalog
from test_statistics_answers import record, statistics_service

from observatory.date_inference import BASIS_NOTE, _sources, url_date
from observatory.models import Filters
from observatory.service import Service, _date_inference_note


def test_url_dates_need_a_full_valid_path_date():
    assert url_date("https://www.cnbc.com/advertorial/2016/09/29/explaining-energy.html") == date(2016, 9, 29)
    assert url_date("https://example.org/2016/02/30/not-a-day/") is None
    assert url_date("https://example.org/sponsored/2016/story") is None
    assert url_date(None) is None


def test_label_says_dates_are_supplemented_and_names_the_method():
    assert _date_inference_note(False, {}) == ""
    unused = _date_inference_note(False, {"A": 19})
    assert "19 of these records have no publication date in the source data" in unused and "not used here" in unused
    used = _date_inference_note(True, {"A": 19, "C": 2})
    assert "supplemented" in used
    assert "19 from a date in the article URL (tier A)" in used and "2 from a web search (tier C)" in used
    assert "supplemented" in BASIS_NOTE


def undated_rows():
    return [
        record("d1", "native", "CNBC", "totalenergies", when=None,
               date_basis="inferred:url_path", inferred_date="2016-09-29", inferred_tier="A"),
        record("d2", "native", "CNBC", "totalenergies", when=None, date_basis="missing"),
        record("k1", "native", "CNBC", "totalenergies", when="2018-01-02", date_basis="source"),
    ]


def test_statistics_answer_always_carries_the_server_disclosure():
    service, _, _ = statistics_service(undated_rows())
    from observatory.structured_queries import QuestionPlan
    plan = QuestionPlan("ready", kind="count", filters=Filters(dataset="native", date_presence="missing"),
                        scope_notes=("Stored records only.",))
    answer = service._statistics_answer(plan)
    assert answer.structured_result["date_inference"]["tiers"] == {"A": 1}
    assert "1 of these records have no publication date in the source data but have a supplemented date" in answer.answer
    # The research-agent path rebuilds the text from tool data and keeps the note.
    rebuilt = Service._tool_statistics_answer(answer.structured_result, base_filters=Filters(dataset="native"))
    assert "supplemented date" in rebuilt.answer


def test_records_expose_date_basis_to_tools():
    service, _, _ = statistics_service(undated_rows())
    from observatory.structured_queries import QuestionPlan
    plan = QuestionPlan("ready", kind="count", filters=Filters(dataset="native"), scope_notes=())
    rows = {r["record_id"]: r for r in service._statistics_answer(plan).structured_result["records"]}
    assert rows["d1"]["date_basis"] == "inferred:url_path" and rows["d1"]["inferred_date"] == "2016-09-29"
    assert rows["k1"]["date_basis"] == "source"


def test_inferred_dates_are_opt_in_through_the_trusted_selection():
    catalog, _ = make_catalog()
    default = catalog.narrow(None)
    assert default.include_inferred_dates is False
    from observatory.research_tools import FiltersRequest
    opted_catalog, _ = make_catalog(Filters(include_inferred_dates=True))
    opted = opted_catalog.narrow(FiltersRequest(include_inferred_dates=True))
    assert opted.include_inferred_dates is True


def test_web_search_sources_come_only_from_provider_metadata():
    response = SimpleNamespace(output=[
        SimpleNamespace(type="web_search_call", action=SimpleNamespace(sources=[{"url": "https://a.example/x"}])),
        SimpleNamespace(type="message", content=[SimpleNamespace(annotations=[SimpleNamespace(url="https://b.example/y")])]),
    ])
    assert _sources(response) == {"https://a.example/x", "https://b.example/y"}


def test_cited_passages_label_inferred_dates_and_carry_the_notice():
    catalog, db = make_catalog()
    for row in db.rows:
        row["date_basis"] = "source" if row["date"] else "missing"
    db.rows[0].update(date=None, date_basis="inferred:url_path", inferred_date="2016-09-29", inferred_tier="A")
    result = catalog.call("search_records", {"query": "emissions"})
    labels = {item["record_id"]: item["date_basis"] for item in result["evidence"]}
    assert labels["r1"] == "inferred:url_path"
    assert result["evidence"][0]["inferred_date"] == "2016-09-29"
    assert result["date_notice"] == BASIS_NOTE
    plain, _ = make_catalog()
    assert "date_notice" not in plain.call("search_records", {"query": "emissions"})


def test_cited_answer_disclosure_is_decided_by_the_server_and_never_breaks_answers():
    from observatory.config import Settings
    from observatory.models import Answer, Evidence

    evidence = [Evidence(evidence_id="e1", record_id="r1", version_id="v1", dataset="native",
                         title="t", text="x", start=0, end=1)]
    answer = Answer(status="answered", answer="Claim [1]", evidence=evidence)

    class DB:
        def __init__(self, rows=None, fail=False):
            self.rows, self.fail = rows or [], fail

        def public_rows(self, filters):
            if self.fail:
                raise RuntimeError("database unavailable")
            assert filters.record_ids == ["r1"]
            return self.rows

    inferred = Service(Settings(), db=DB([{"record_id": "r1", "date_basis": "inferred:url_path"}]), rag=object())
    assert inferred._cites_inferred_dates(answer, Filters())
    sourced = Service(Settings(), db=DB([{"record_id": "r1", "date_basis": "source"}]), rag=object())
    assert not sourced._cites_inferred_dates(answer, Filters())
    broken = Service(Settings(), db=DB(fail=True), rag=object())
    assert broken._cites_inferred_dates(answer, Filters()) is None  # unknown, not "none"
    from observatory.date_inference import UNCHECKED_NOTE
    assert "could not be checked" in UNCHECKED_NOTE



def test_same_site_check_ignores_www_and_rejects_other_hosts():
    from observatory.date_inference import same_site
    assert same_site("https://www.washingtonpost.com/a", "https://washingtonpost.com/b")
    assert not same_site("https://www.chevron.com/newsroom/x", "https://www.nytimes.com/paidpost/x")
    assert not same_site(None, "https://www.nytimes.com/x")


def test_missing_date_count_explains_source_gap_and_supplemented_dates():
    rows = undated_rows() + [record("d3", "native", "CNBC", "totalenergies", when=None, date_basis="missing")]
    service, db, _ = statistics_service(rows)
    original = type(db).dashboard.__get__(db)

    def dashboard(filters, *args, **kwargs):
        # The double knows no date presence; emulate it, with d1 dated by its supplement.
        from observatory.service import summarize
        result = original(filters)  # count every row, not one page
        kept = [r for r in result["page"]["rows"] if not r["date"]] if filters.date_presence == "missing" else result["page"]["rows"]
        if filters.include_inferred_dates:
            kept = [r for r in kept if r["record_id"] != "d1"]
        return {"stats": summarize(kept), "page": {"rows": kept}}

    db.dashboard = dashboard
    from observatory.structured_queries import QuestionPlan
    plan = QuestionPlan("ready", kind="count", scope_notes=(),
                        filters=Filters(dataset="native", date_presence="missing", include_inferred_dates=True))
    answer = service._statistics_answer(plan)
    assert answer.structured_result["collections"][0]["total"] == 2
    assert "3 records have no publication date in the source data; 1 of them have a supplemented date, so 2 remain without any date." in answer.answer
