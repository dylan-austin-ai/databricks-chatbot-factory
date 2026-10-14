"""Searchable text: table headers carried with their pages, titles in, repeats out, evidence verbatim."""
import json
import pathlib

from conftest import FakeSql
from factory import search_text as st
from factory.answering import evidence_units

ROOT = pathlib.Path(__file__).resolve().parents[1]
# A premium table split over two sections (synthetic, shaped like the QA document).
TABLE_START = ("Medical premiums per pay period.\n| Coverage level | Plan A | Plan B |\n|---|---|---|\n"
               "| Employee only | $40 | $25 |")
TABLE_REST = "| Employee + spouse | $95 | $60 |\n| Family | $140 | $90 |\nRates change each January."
AFTER = "Dental premiums are separate."


def chunk(pos, text, pages, embed=None, name="Benefits_Guide-2026.pdf"):
    return {"chunk_id": f"c{pos}", "chunk_position": pos, "doc_name": name, "page_ids": pages,
            "chunk_to_retrieve": text, "chunk_to_embed": embed if embed is not None else text}


def test_a_section_that_continues_a_table_gets_its_header_and_its_page():
    changed = {c["chunk_id"]: c for c in st.refine([chunk(1, TABLE_REST, [5]), chunk(0, TABLE_START, [4]),
                                                    chunk(2, AFTER, [5])])}
    rest = changed["c1"]
    assert rest["chunk_to_retrieve"] == "| Coverage level | Plan A | Plan B |\n|---|---|---|\n" + TABLE_REST
    assert rest["page_ids"] == [4, 5]                                     # where the header came from, too
    lines = [rest["chunk_to_retrieve"][a:b] for a, b in evidence_units(rest["chunk_to_retrieve"])]
    assert lines[0] == "| Coverage level | Plan A | Plan B |" and "| Employee + spouse | $95 | $60 |" in lines
    assert changed["c2"]["chunk_to_retrieve"] == AFTER                    # not a table: only its search text changes
    assert changed["c0"]["chunk_to_retrieve"] == TABLE_START


def test_a_header_is_not_carried_past_the_end_of_its_table():
    changed = {c["chunk_id"]: c for c in st.refine([chunk(0, TABLE_START, [4]), chunk(1, AFTER, [5]),
                                                    chunk(2, "| Orphan row | $1 | $2 |", [6])])}
    assert changed["c2"]["chunk_to_retrieve"] == "| Orphan row | $1 | $2 |" and changed["c2"]["page_ids"] == [6]


def test_html_tables_too():
    start = "<table><tr><th>Coverage</th><th>Cost</th></tr><tr><td>Employee</td><td>$40</td></tr>"
    rest = "<tr><td>Family</td><td>$140</td></tr></table>"
    assert st.table_header(start) == "<tr><th>Coverage</th><th>Cost</th></tr>"
    assert st.continues_a_table(rest) and not st.continues_a_table(start)
    changed = {c["chunk_id"]: c for c in st.refine([chunk(0, start, [2]), chunk(1, rest, [3])])}
    assert changed["c1"]["chunk_to_retrieve"].startswith("<tr><th>Coverage</th><th>Cost</th></tr>\n<tr><td>Family")


def test_the_index_searches_the_title_and_each_repeated_line_once():
    screenshot = ("Settings icon\nSettings icon\nHelp icon\nSettings icon\n"
                  "Open Benefits, choose Submitted Elections, then click Edit.\nHelp icon")
    (c,) = st.refine([chunk(0, screenshot, [1], name="myCigna_spouse_registration.png")])
    assert c["chunk_to_embed"] == ("Document: myCigna spouse registration\nSettings icon\nHelp icon\n"
                                   "Open Benefits, choose Submitted Elections, then click Edit.")
    assert c["chunk_to_retrieve"] == screenshot                           # the evidence is left as it was
    assert st.searchable("x.pdf", c["chunk_to_embed"].replace("myCigna spouse registration", "x")) .count("Document:") == 1
    assert st.title("Open_Enrollment--2026 Guide.PDF") == "Open Enrollment 2026 Guide"


def test_nothing_is_written_for_sections_that_are_already_right_and_pages_may_come_as_text():
    done = chunk(0, AFTER, json.dumps([5]), embed=f"Document: Benefits Guide 2026\n{AFTER}")
    assert st.refine([done]) == []


def test_the_pipeline_refines_after_chunking_in_one_statement(settings):
    from factory.ingestion import BotPaths
    from factory.pipeline import Pipeline

    rows = [{**chunk(0, TABLE_START, [4]), "doc_id": "d1", "doc_version": 2},
            {**chunk(1, TABLE_REST, [5]), "doc_id": "d1", "doc_version": 2}]
    sql = FakeSql(answers=[(r"SELECT c\.chunk_id, c\.doc_id, c\.doc_version, c\.chunk_position", rows)])
    pipe = Pipeline.__new__(Pipeline)
    pipe.sql = sql
    assert pipe.refine_search_text(BotPaths(settings, "claims_chatbot"), ["d1"]) == 2
    merges = [s for s, _ in sql.statements if s.lstrip().startswith("MERGE INTO")]
    assert len(merges) == 1 and "UPDATE SET chunk_to_retrieve = s.chunk_to_retrieve" in merges[0]
    source = (ROOT / "src" / "factory" / "pipeline.py").read_text()
    assert source.index("self.refine_search_text(p, ids)") < source.index("chunks = self._current_chunks(p, ids)")
