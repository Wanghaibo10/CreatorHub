"""抖音纯协议发布:不打网络的结构单测。"""
from __future__ import annotations

import json

from application.douyin.api import (
    CREATOR_AID, IMAGEX_APP_ID, VISIBILITY, _multipart, _sigv4,
    build_create_v2, cookies_from_state, new_creation_id)


def test_cookies_from_state_只收抖音域():
    state = json.dumps({
        "cookies": [
            {"name": "sessionid", "value": "s1", "domain": ".douyin.com"},
            {"name": "sid_tt", "value": "t1", "domain": "www.douyin.com"},
            {"name": "other", "value": "x", "domain": ".kuaishou.com"},
        ]
    })
    ck = cookies_from_state(state)
    assert ck["sessionid"] == "s1"
    assert ck["sid_tt"] == "t1"
    assert "other" not in ck


def test_空state给空dict():
    assert cookies_from_state("") == {}
    assert cookies_from_state("{") == {}


def test_可见性枚举():
    assert VISIBILITY["public"] == 0
    assert VISIBILITY["private"] == 1
    assert VISIBILITY["friends"] == 2
    assert CREATOR_AID == "1128"
    assert IMAGEX_APP_ID == "2906"


def test_create_v2_包形对得上实发抓包():
    cid = new_creation_id(1786974189281)
    assert cid.endswith("1786974189281")
    assert len(cid) == 8 + 13
    payload = build_create_v2(
        "v1e00fgi0000da1gvrvog65utlehjp20", "协议探测2", "第二次，等到真成功",
        visibility="private", allow_save=False, creation_id=cid)
    item = payload["item"]
    common = item["common"]
    assert set(item) == {
        "common", "cover", "mix", "selected_member", "chapter",
        "anchor", "sync", "open_platform", "assistant",
    }
    assert common["item_title"] == "协议探测2"
    assert common["caption"] == "第二次，等到真成功"
    assert common["text"] == "协议探测2 第二次，等到真成功"
    assert common["visibility_type"] == 1
    assert common["download"] == 0
    assert common["media_type"] == 4
    assert common["video_id"] == "v1e00fgi0000da1gvrvog65utlehjp20"
    assert common["creation_id"] == cid
    assert common["music_id"] is None
    assert "poster" not in item["cover"]
    pub = build_create_v2(
        "v0d00fg10000da1hfqvog65n02bljnmg", "巧克力小笼包让我秒懂菠萝披萨",
        "换我也破防，意大利人看了都得点头。直到我刷到巧克力小笼包。",
        visibility="public", allow_save=True,
        poster="tos-cn-i-jm8ajry58r/10d93cc5dbcc4e0ab8e7ecb61f81ca44")
    assert pub["item"]["common"]["visibility_type"] == 0
    assert pub["item"]["common"]["download"] == 1
    assert pub["item"]["cover"]["poster"].startswith("tos-cn-i-jm8ajry58r/")


def test_multipart_顺序与边界():
    body, ct = _multipart([
        ("video_id", "vid1"),
        ("item_title", "标题"),
        ("poster", ("p.jpg", b"\xff\xd8", "image/jpeg")),
    ])
    assert ct.startswith("multipart/form-data; boundary=")
    assert b'name="video_id"' in body
    assert b"vid1" in body
    assert body.find(b"video_id") < body.find(b"item_title")
    assert body.endswith(b"--\r\n")


def test_浏览器抓包过滤创作域():
    from application.douyin.publish import _SNIFF_HOST, _redact

    assert _SNIFF_HOST.search("https://creator.douyin.com/web/api/media/upload/auth/v5/")
    assert _SNIFF_HOST.search("https://vod.bytedanceapi.com/top/v1?Action=ApplyUploadInner")
    assert not _SNIFF_HOST.search("https://www.douyin.com/aweme/v1/web/aweme/detail")
    assert "***" in _redact("Cookie: sessionid=abc123")


def test_create_v2_query_带_aid1128_和_read_aid2906():
    from application.douyin.api import DouyinAPI
    api = DouyinAPI({"sessionid": "x"})
    url = api._signed_url("/web/api/media/aweme/create_v2/",
                          extra={"read_aid": IMAGEX_APP_ID})
    assert "aid=1128" in url
    assert "read_aid=2906" in url


def test_sigv4_头齐全():
    sts = {
        "access_key_id": "AKID",
        "secret_access_key": "SECRET",
        "session_token": "TOK",
    }
    h = _sigv4("GET", "/top/v1", {"Action": "ApplyUploadInner", "SpaceName": "aweme"},
               sts)
    assert h["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")
    assert "sdwdmwlll/vod/aws4_request" in h["authorization"]
    assert h["x-amz-security-token"] == "TOK"
    assert "x-amz-date" in h
