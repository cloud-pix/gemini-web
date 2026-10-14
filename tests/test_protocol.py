import json
from gemini_web import protocol as P
from gemini_web import cookies as C


def _frame(obj):
    s = json.dumps(obj)
    return f"{len(s) + 1}\n{s}"  # Google counts the newline in the length


def test_extract_init_values():
    html = ('"SNlM0e":"AT123","cfb2h":"bl456","FdrFJe":"sid789",'
            '"TuX5cc":"en","qKIAYe":"push0"')
    v = P.extract_init_values(html)
    assert v == {"access_token": "AT123", "build_label": "bl456",
                 "session_id": "sid789", "language": "en", "push_id": "push0"}
    assert P.extract_init_values("<html></html>")["access_token"] is None


def test_frame_parser_roundtrip():
    inner = json.dumps([None, {"x": 1}])
    payload = ")]}'\n" + _frame([["wrb.fr", None, inner]]) + _frame([["di", 1]])
    parts = P.parse_frames(payload)
    assert parts[0][0] == "wrb.fr" and parts[1][0] == "di"


def test_iter_candidates_and_best():
    inner = json.dumps([0, 0, 0, 0, [["rc1", ["short", "x"]], ["rc2", ["a much longer reply text here", "x"]]]])
    parts = [["wrb.fr", None, inner]]
    got = list(P.iter_candidates(parts))
    assert [r for r, _ in got] == ["rc1", "rc2"]
    assert P.best_text(parts) == "a much longer reply text here"


def test_error_code_surfaces():
    parts = [[None, None, None, None, None, [None, None, [[0, [1037]]]]]]
    try:
        list(P.iter_candidates(parts))
        assert False, "should raise"
    except RuntimeError as e:
        assert "1037" in str(e)


def test_build_shapes():
    inner = P.build_inner_request("hi", [[["/id1"], "v.mp4"]])
    assert len(inner) == 81
    assert inner[0] == ["hi", 0, None, [[["/id1"], "v.mp4"]], None, None, 0]
    assert inner[7] == 1 and inner[30] == [4] and inner[80] == 1
    assert P.build_inner_request("hi", thinking="medium")[80] == 4
    assert P.build_inner_request("hi", thinking="high")[80] == 2
    assert P.build_inner_request("hi", thinking=True)[80] == 2
    assert isinstance(inner[59], str) and len(inner[59]) > 10
    body = P.build_generate_body("AT", inner)
    assert body["at"] == "AT" and json.loads(body["f.req"])[1]
    params = P.build_generate_params("en", 100, "bl", "sid")
    assert params == {"hl": "en", "_reqid": 100, "rt": "c", "bl": "bl", "f.sid": "sid"}
    fd = P.build_file_data(["/a", "/b"], ["x.mp4", "y.mp4"])
    assert fd == [[["/a"], "x.mp4"], [["/b"], "y.mp4"]]


def test_cookie_loaders(tmp_path):
    ns = tmp_path / "c.txt"
    ns.write_text("# Netscape\n.google.com\tTRUE\t/\tTRUE\t0\t__Secure-1PSID\tABC\n.google.com\tTRUE\t/\tTRUE\t0\tother\tZZZ\n")
    assert C.load(str(ns)) == {"__Secure-1PSID": "ABC"}
    js = tmp_path / "c.json"
    js.write_text(json.dumps({"__Secure-1PSID": "A", "__Secure-1PSIDTS": "B"}))
    assert C.load(str(js)) == {"__Secure-1PSID": "A", "__Secure-1PSIDTS": "B"}
    assert C.load(str(tmp_path / "nope")) == {}


def test_cli_builds():
    from gemini_web.cli import build
    p = build()
    assert p.parse_args(["check"]).cmd == "check"
    assert p.parse_args(["models"]).cmd == "models"
    assert p.parse_args(["analyze", "v.mp4"]).cmd == "analyze"
    assert p.parse_args(["ask", "hi", "--model", "gemini-flash"]).model == "gemini-flash"
    assert p.parse_args(["analyze-batch", "a.mp4", "b.mp4", "--delay", "3"]).delay == 3


def _synth_part(body):
    return ["wrb.fr", P.RPC_GET_USER_STATUS, json.dumps(body)]


def test_compute_capacity():
    assert P.compute_capacity([], []) == (1, 12)
    assert P.compute_capacity([8], []) == (2, 12)
    assert P.compute_capacity([], [115]) == (4, 12)
    assert P.compute_capacity([16], []) == (3, 12)
    assert P.compute_capacity([22], []) == (2, 13)


def test_model_from_rpc_and_header():
    item = ["abc123", "Flash", "fast model"] + [None] * 6 + [None, None, "Gemini Flash",
            "Fast everyday help", None, None, None, None, 1, None, "Gemini Flash 2.0"]
    m = P.model_from_rpc(item, 1, 12)
    assert m["model_id"] == "abc123" and m["model_number"] == 1
    assert "gemini-flash" in m["aliases"]
    h = m["header"][P.MODEL_HEADER_KEY]
    assert '"abc123"' in h and h.rstrip("]").endswith(",1")


def test_parse_models():
    body = [None] * 15 + [[
        ["id1", "Pro", "pro desc"] + [None] * 6 + [None, None, "Gemini Pro",
         "Pro help", None, None, None, None, 3, None, "Gemini 3 Pro"],
        ["id2", "Flash", "flash desc"] + [None] * 6 + [None, None, "Gemini Flash",
         "Flash help", None, None, None, None, 1, None, "Gemini 2 Flash"],
    ]] + [[]] * 2
    s = json.dumps(body)
    frame = json.dumps([["wrb.fr", P.RPC_GET_USER_STATUS, s]])
    payload = ")]}'" + "\n" + str(len(frame) + 1) + "\n" + frame
    parts = P.parse_frames(payload)
    models = P.parse_models(parts)
    assert [m["model_name"] for m in models] == ["gemini-pro", "gemini-flash"]
    assert all(m["is_available"] for m in models)
    assert models[0]["model_number"] == 3


def test_batch_f_req_shape():
    f = P.batch_f_req(P.RPC_GET_USER_STATUS, "[]")
    assert json.loads(f) == [[[P.RPC_GET_USER_STATUS, "[]", None, "generic"]]]


def _synth_candidate(url="https://lh3.googleusercontent.com/x=s1024-rj",
                     alt="a cat", image_id="img9"):
    # item[0][3] = [None,None,alt,url]; item[1] = [image_id]; field7 = [items]
    item = [[None, None, None, [None, None, alt, url]], [image_id]]
    field7 = [item]          # field value list; [0] -> items... no: [0] must BE items
    field7 = [[item]]        # field value list whose [0] is the items list
    rich = [None] * 7 + [field7]
    assert rich[7][0] == [item], rich[7]
    return ["rc9", ["some text"], None, None, None, None, None, None, None, None, None, None, rich]


def test_extract_generated_images():
    cand = _synth_candidate()
    imgs = P.extract_generated_images(cand)
    assert len(imgs) == 1
    assert imgs[0]["url"].endswith("=s1024-rj") and imgs[0]["image_id"] == "img9"
    assert P.extract_generated_images(["rc", ["t"]]) == []


def test_upscale_url():
    assert P.upscale_url("https://x/y=s1024-rj", True) == "https://x/y=s2048-rj"
    assert P.upscale_url("https://x/y", True) == "https://x/y=s2048-rj"
    assert P.upscale_url("https://x/y=s2048-rj", False) == "https://x/y=s1024-rj"


def test_full_size_payload_shape():
    import json as _j
    p = P.full_size_image_payload("img9", "r1", "rc1", "c1")
    assert p[0][1] == ["img9", 0] and p[1] == ["r1", "rc1", "c1", None, ""]
    _j.dumps(p)


def test_iter_candidate_full_tracks_ids():
    inner = json.dumps([None, ["cid7", "rid8"], 0, 0, [["rc9", ["hello"]]]])
    frame = json.dumps([["wrb.fr", None, inner]])
    payload = ")]}'" + "\n" + str(len(frame) + 1) + "\n" + frame
    got = list(P.iter_candidate_full(P.parse_frames(payload)))
    assert got[0][:3] == ("cid7", "rid8", "rc9")
