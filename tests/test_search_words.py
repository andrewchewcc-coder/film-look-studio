import numpy as np
from PIL import Image

from filmlook.library import colours, db, search


def test_colour_measure_and_words():
    red = colours.measure(Image.new("RGB", (100, 100), (210, 30, 30)))
    grey = colours.measure(Image.new("RGB", (100, 100), (128, 128, 128)))
    assert red["red"] > 0.5 and red["blue"] == 0 and grey["red"] == 0 and grey["chroma"] < red["chroma"]
    assert colours.find_words("red umbrella") == (["red"], False, "umbrella")
    assert colours.find_words("black and white portrait") == ([], True, "portrait")


def test_understand_splits_the_search(tmp_path):
    con = db.connect(tmp_path / "t.db")
    con.execute("INSERT INTO drives (id, uuid, name) VALUES (1, 'u', 'D')")
    for i, name in enumerate(["2025 10 17 Portugal trip", "2024 02 10 Chinatown CNY", "2023 05 01 Tokyo",
                              "2022 Kyoto", "2021 Osaka"], 1):
        con.execute("INSERT INTO folders (id, drive_id, rel_path) VALUES (?, 1, ?)", (i, name))
    u = search.understand(con, "red umbrella Portugal 2025 october")
    assert u["year"] == "2025" and u["month"] == 10 and u["colours"] == ["red"]
    assert u["folders"] == {1} and u["picture"] == "umbrella"
    assert search.understand(con, "may")["month"] is None          # just a word without a year
    assert search.understand(con, "trip")["folders"] == set()     # too common a folder word
