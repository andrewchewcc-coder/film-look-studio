from PIL import Image

from filmlook import claude_ai, engine, grade_ai


def test_answer_is_clamped_and_kept_to_known_controls(monkeypatch, tmp_path):
    seen = {}

    def fake_ask(system, content, schema, **k):
        seen["schema"] = schema
        sliders = {k: 0 for k in grade_ai.SLIDERS} | {"strength": 1, "warmth": 999, "exposure": "x"}
        return {"look": "not-a-look", "sliders": sliders,
                "mixer": [{"band": "blue", "hue": 0, "sat": -5, "lum": 0}, {"band": "teal", "hue": 9, "sat": 1, "lum": 1}],
                "masks": [{"kind": "sky", "invert": False, "exposure": -9, "contrast": 0, "warmth": 0, "saturation": 0},
                          {"kind": "moon", "invert": False, "exposure": 1, "contrast": 0, "warmth": 0, "saturation": 0}],
                "explanation": ""}, 0.02

    monkeypatch.setattr(claude_ai, "ask_json", fake_ask)
    monkeypatch.setattr(engine, "render_preview", lambda *a, **k: Image.new("RGB", (64, 48)))
    monkeypatch.setattr(grade_ai.edits, "get", lambda p: None)
    r = grade_ai.suggest(tmp_path / "x.dng", "warmer", "neutral", {"curve": [[0, 0], [0.5, 0.6], [1, 1]]})
    a = r["adjust"]
    assert r["look"] == "neutral" and a["warmth"] == 50 and a["exposure"] == 0.0     # clamped / unreadable -> 0
    assert a["hsl"] == {"blue": {"sat": -1}} and [m["kind"] for m in a["masks"]] == ["sky"]
    assert a["masks"][0]["exposure"] == -2 and a["curve"] == [[0, 0], [0.5, 0.6], [1, 1]]   # your curve is kept
    assert r["explanation"].startswith("Changed:") and "neutral" in seen["schema"]["properties"]["look"]["enum"]
