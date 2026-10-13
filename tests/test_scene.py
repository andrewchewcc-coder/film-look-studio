from PIL import Image

from filmlook.library import scene


def analysis(**kw):
    a = {"size": [1600, 1067], "people": [], "faces": [], "poses": [], "subjects": []}
    a.update(kw)
    return a


def test_people_merges_detector_pose_and_face():
    a = analysis(people=[{"box": [0.4, 0.2, 0.2, 0.7], "conf": 0.9}],
                 faces=[{"box": [0.47, 0.22, 0.05, 0.07], "quality": 0.6, "eyes": 0.3, "yaw": 0.0}],
                 poses=[{"nose": [0.5, 0.25, 0.9], "neck": [0.5, 0.3, 0.9], "left_ankle": [0.45, 0.88, 0.8],
                         "right_ankle": [0.56, 0.88, 0.8], "root": [0.5, 0.55, 0.9]}])
    assert len(scene.people(a)) == 1


def test_gesture_rewards_stride_and_raised_hands():
    standing = {"nose": [0.5, 0.1, 1], "left_ankle": [0.49, 0.9, 1], "right_ankle": [0.51, 0.9, 1],
                "left_shoulder": [0.45, 0.2, 1], "left_wrist": [0.45, 0.45, 1]}
    striding = {**standing, "left_ankle": [0.3, 0.9, 1], "right_ankle": [0.7, 0.9, 1]}
    waving = {**standing, "left_wrist": [0.3, 0.05, 1]}
    assert scene._gesture(striding) > scene._gesture(standing)
    assert scene._gesture(waving) > scene._gesture(standing)


def test_edge_cut_ignores_small_background_figures_and_waist_crops():
    pv = Image.new("RGB", (800, 533), (120, 120, 120))
    main = {"box": [0.35, 0.1, 0.3, 0.85], "conf": 0.9}
    tiny_at_edge = {"box": [0.97, 0.4, 0.03, 0.1], "conf": 0.9}
    f = scene.features(analysis(people=[main, tiny_at_edge]), pv)
    assert f["edge_cut"] == 0.0
    waist_crop = scene.features(analysis(people=[{"box": [0.3, 0.2, 0.4, 0.8], "conf": 0.9}]), pv)
    assert waist_crop["edge_cut"] <= 0.1
    sliced = scene.features(analysis(people=[main, {"box": [0.0, 0.1, 0.25, 0.85], "conf": 0.9}]), pv)
    assert sliced["edge_cut"] >= 0.5
