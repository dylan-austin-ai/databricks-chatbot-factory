"""Every quality message tells the owner how sure it is and what to do next."""
from factory import qa
from factory.llm import chat_json, image_type
from factory.qa import guidance, next_step


def test_every_message_the_checks_can_produce_has_a_next_step():
    thresholds = {"min_chars_per_page": 200, "yellow_min_confidence": 0.5, "green_min_confidence": 0.8}
    produced = []
    produced += qa.classify({"n_pages": 4, "text_pages": [0], "total_chars": 100, "mean_confidence": 0.3,
                             "errors_json": '[{"page_id": 1}]'}, thresholds, expected_pages=6).messages
    produced += qa.classify({"n_pages": 2, "text_pages": [0, 1], "total_chars": 9000, "mean_confidence": 0.7},
                            thresholds).messages
    produced += qa.merge_visual(qa.QaResult("green"), [{"page": 3, "severity": "major", "issue": "table lost"}]).messages
    produced += ["Reviewer note: figure descriptions clutter the text", "Visual check unavailable.",
                 "2 section(s) contain text that looks like instructions to an AI. They were held back; review "
                 "them under Sections and unflag any that are fine.",
                 "Blocked: this document appears to contain restricted data (1 Social Security numbers). "
                 "Remove it and upload a clean copy.",
                 "Contains personal information (3 email addresses). Make sure that's intended."]
    assert len(produced) >= 10
    for message in produced:
        g = guidance(message)
        assert g["kind"] in ("problem", "check", "opinion", "info"), message
        assert g["next"], f"no next step for: {message}"


def test_reviewer_notes_and_visual_findings_are_opinions_not_problems():
    assert guidance("Reviewer note: multiple figure descriptions clutter the text")["kind"] == "opinion"
    assert "not a confirmed" in guidance("Reviewer note: x")["next"]
    assert guidance("Page 3: the table is scrambled")["kind"] == "opinion"
    assert guidance("We couldn't read page 4.")["kind"] == "problem"
    assert guidance("Some parts of this document were hard to read.")["kind"] == "check"


def test_raw_api_errors_are_not_shown():
    old = ("Visual check unavailable: Error code: 400 - {'error_code': 'BAD_REQUEST', 'message': "
           "'messages.0.content.1.image.source.base64: The image was specified using the image/png media type'}")
    g = guidance(old)
    assert g == {"kind": "info", "text": "The automatic visual check couldn't run.", "next": g["next"]}
    assert "400" not in g["text"] + g["next"] and "says nothing about the document" in g["next"]


def test_unrecognised_messages_still_get_guidance():
    g = guidance("Something new the checks started saying.")
    assert g["kind"] == "check" and g["next"]


def test_next_step_follows_the_most_serious_finding():
    hard = "Some parts of this document were hard to read."
    note = "Reviewer note: cluttered"
    assert next_step("red", "pending_review", ["We couldn't read page 2.", note]).startswith("Fix the problem")
    assert next_step("yellow", "pending_review", [hard, note]).startswith("Check the points")
    assert "automatic reviewer left suggestions" in next_step("yellow", "pending_review", [note])
    assert next_step("green", "approved", ["Extracted cleanly."]).startswith("Nothing to do")
    assert next_step(None, "pending_review", []).startswith("Still being read")
    assert next_step("red", "flagged", []).startswith("Fix the problem")
    assert next_step("green", "pending_review", ["Extracted cleanly."]) == "Skim it, then press Approve."


def test_image_type_comes_from_the_bytes():
    assert image_type(b"\xff\xd8\xff\xe0" + b"0" * 20) == "image/jpeg"
    assert image_type(b"\x89PNG\r\n\x1a\n" + b"0" * 20) == "image/png"
    assert image_type(b"GIF89a" + b"0" * 20) == "image/gif"
    assert image_type(b"RIFF0000WEBPVP8 ") == "image/webp"


def test_page_images_are_sent_with_their_real_type(monkeypatch):
    sent = {}

    def fake_chat(client, endpoint, messages, max_tokens=0):
        sent["content"] = messages[0]["content"]
        return '{"findings": []}', {}

    monkeypatch.setattr("factory.llm.chat", fake_chat)
    chat_json(None, "endpoint", "check this page", images=[b"\xff\xd8\xff\xe0jpegbytes"])
    assert sent["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
