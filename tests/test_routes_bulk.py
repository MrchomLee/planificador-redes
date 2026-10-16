"""Route tests of the "Carga masiva" (routes/bulk.py + bulk.py with the real postsvc): page, CSRF, autosave
and discard, distribution, validation (row issues vs common settings, daily caps), "Programar todo"
(idempotent per batch token, rows with errors saved as drafts, TikTok consent and modes, YouTube titles) and
resuming a submit interrupted by a crash."""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import av
import pytest
from PIL import Image
from sqlalchemy import select

from planificador import bulk, config, media
from planificador.deps import set_active_brand
from planificador.models import (
    Account,
    AccountStatus,
    Brand,
    Network,
    Post,
    PostTarget,
    Setting,
    TargetStatus,
)

STATE_RE = re.compile(r"<script type=\"application/json\" data-bulk-state>(.*?)</script>", re.S)
TOKEN = "tok_" + "a" * 20


@pytest.fixture(scope="module")
def video_bytes(tmp_path_factory) -> bytes:
    path = Path(tmp_path_factory.mktemp("bulkvid")) / "clip.mp4"
    out = av.open(str(path), "w", format="mp4")
    stream = out.add_stream("libx264", rate=24)
    stream.width, stream.height, stream.pix_fmt = 360, 640, "yuv420p"
    for i in range(24):
        frame = av.VideoFrame.from_image(Image.new("RGB", (360, 640), (i * 9 % 255, 60, 200)))
        for packet in stream.encode(frame):
            out.mux(packet)
    for packet in stream.encode():
        out.mux(packet)
    out.close()
    return path.read_bytes()


@pytest.fixture
def videos(db, brand, video_bytes):
    """Three library videos (distinct bytes → distinct assets)."""
    assets = []
    for i, name in enumerate(["uno_receta.mp4", "dos-truco.mp4", "tres.mp4"]):
        data = video_bytes + b"\0" * (i + 1)  # trailing padding keeps the file playable, changes the hash
        assets.append(media.ingest(db, io.BytesIO(data), name, brand.id))
    return assets


def add_account(db, brand, network: str, **kw) -> Account:
    account = Account(
        brand_id=brand.id,
        network=network,
        external_id=f"ext-{network}",
        name=kw.pop("name", f"Cuenta {network}"),
        username=kw.pop("username", network),
        account_type=kw.pop("account_type", "business"),
        status=AccountStatus.CONNECTED,
        **kw,
    )
    account.set_tokens("tok", "ref")
    db.add(account)
    db.commit()
    return account


def make_batch(accounts: dict, rows: list, *, token: str = TOKEN, common=None, **kw) -> dict:
    batch = bulk.empty_batch(token)
    batch["accounts"] = accounts
    batch["rows"] = rows
    batch["common_hashtags"] = common or []
    batch.update(kw)
    return batch


def row(asset, rid: str, *, date="2026-11-05", time="12:00", text="Hola", hashtags=None, **kw) -> dict:
    return {
        "id": rid,
        "media_id": asset.id,
        "text": text,
        "hashtags": hashtags or [],
        "yt_title": kw.pop("yt_title", ""),
        "date": date,
        "time": time,
        "networks": kw.pop("networks", {}),
    }


def demo_conf(**kw) -> dict:
    return {"selected": True, "format": "post", "auto_publish": True, "options": {}, **kw}


def bulk_posts(db) -> list[Post]:
    db.expire_all()
    return list(db.scalars(select(Post).where(Post.source == "bulk").order_by(Post.id)))


def page_state(client) -> dict:
    r = client.get("/carga-masiva")
    assert r.status_code == 200, r.text[:500]
    return json.loads(STATE_RE.search(r.text).group(1))


# ============================================================================================ page


def test_page_renders_with_state_and_menu_entry(client, demo_mode, brand):
    r = client.get("/carga-masiva")
    assert r.status_code == 200
    html = r.text
    assert "Carga masiva" in html and "Programar todo" in html
    assert 'class="dropdown__item is-active" role="menuitem" href="/carga-masiva"' in html
    state = json.loads(STATE_RE.search(html).group(1))
    assert re.match(r"^[A-Za-z0-9_-]{16,64}$", state["batch"]["token"])
    assert state["restored"] is False and state["interrupted"] is None
    assert state["batch"]["brand_id"] == brand.id  # every save/check/submit says which brand it is for
    assert state["batch"]["distribution"]["slots"]  # suggested times (best times / general pattern)
    assert [a["key"] for a in state["accounts"]] == ["demo"]  # demo mode: pseudo Demo account
    assert state["today"] == "2026-11-03"


def test_entry_points_link_to_the_page(client):
    for path in ("/planificacion", "/planificacion/lista", "/importar"):
        assert 'href="/carga-masiva"' in client.get(path).text, path


def test_unsafe_requests_need_the_csrf_token(client, csrf_client, brand):
    body = {"batch": make_batch({}, [])}
    assert client.post("/api/carga-masiva/lote", json=body).status_code == 403
    assert client.post("/api/carga-masiva/programar", json=body).status_code == 403
    assert client.post("/api/carga-masiva/lote/descartar").status_code == 403
    assert csrf_client.post("/api/carga-masiva/lote", json=body).status_code == 200


# ======================================================================================== autosave


def test_autosave_keeps_everything_but_the_consent(
    client, csrf_client, db, demo_mode, demo_account, brand, videos
):
    key = str(demo_account.id)
    batch = make_batch(
        {key: demo_conf(options={"consent": True, "demo_fail": "none"})},
        [row(videos[0], "r1", hashtags=["#uno"]), row(videos[1], "r2", text="Dos", date="", time="")],
        common=["#comun"],
        step=3,
    )
    r = csrf_client.post("/api/carga-masiva/lote", json={"batch": batch})
    assert r.status_code == 200 and r.json()["revision"] == 1 and r.json()["rows"] == 2
    assert csrf_client.post("/api/carga-masiva/lote", json={"batch": batch}).json()["revision"] == 2

    state = page_state(client)
    assert state["restored"] is True
    stored = state["batch"]
    assert stored["token"] == TOKEN and stored["step"] == 3
    assert [r["id"] for r in stored["rows"]] == ["r1", "r2"]
    assert stored["rows"][0]["hashtags"] == ["uno"] and stored["common_hashtags"] == ["comun"]
    assert stored["accounts"][key]["options"] == {"demo_fail": "none"}  # consent is never stored
    assert set(state["media_index"]) == {str(videos[0].id), str(videos[1].id)}
    assert state["media_index"][str(videos[0].id)]["compat"]["tiktok"]["status"] in ("ok", "warning")
    assert state["media_index"][str(videos[0].id)]["orientation"] == "vertical"
    assert client.get("/api/carga-masiva/lote").json()["batch"]["rows"][1]["text"] == "Dos"


def test_autosave_rejects_invalid_batches(csrf_client, brand, videos):
    bad = make_batch({}, [row(videos[0], "r1")], token="x")
    r = csrf_client.post("/api/carga-masiva/lote", json={"batch": bad})
    assert r.status_code == 422 and r.json()["ok"] is False
    assert (
        csrf_client.post(
            "/api/carga-masiva/lote", content=b"no json", headers={"Content-Type": "application/json"}
        ).status_code
        == 422
    )


def test_discard_drops_the_batch(client, csrf_client, db, brand, videos):
    csrf_client.post("/api/carga-masiva/lote", json={"batch": make_batch({}, [row(videos[0], "r1")])})
    assert bulk.load_batch(db, brand.id) is not None
    r = csrf_client.post("/api/carga-masiva/lote/descartar")
    assert r.status_code == 200 and r.json()["batch"]["rows"] == []
    assert r.json()["batch"]["token"] != TOKEN
    db.expire_all()
    assert bulk.load_batch(db, brand.id) is None
    assert page_state(client)["restored"] is False


def test_media_endpoint_returns_compatibility(client, brand, videos):
    r = client.get(f"/api/carga-masiva/medios?ids={videos[0].id},{videos[1].id},999")
    items = r.json()["items"]
    assert set(items) == {str(videos[0].id), str(videos[1].id)}
    assert set(items[str(videos[0].id)]["compat"]) == {"facebook", "instagram", "tiktok", "youtube"}


# ==================================================================================== distribution


def test_distribute_endpoint(csrf_client, brand):
    body = {
        "distribution": {
            "start_date": "2026-11-03",
            "mode": "per_day",
            "per_day": 2,
            "slots": ["09:00", "19:00"],
        },
        "count": 3,
    }
    r = csrf_client.post("/api/carga-masiva/repartir", json=body)
    data = r.json()
    assert r.status_code == 200, data
    # Now is 10:00 in Madrid: today's 09:00 has passed.
    assert [(i["date"], i["time"]) for i in data["items"]] == [
        ("2026-11-03", "19:00"),
        ("2026-11-04", "09:00"),
        ("2026-11-04", "19:00"),
    ]
    assert data["summary"]["days"] == 2 and data["summary"]["count"] == 3

    body["distribution"]["per_day"] = 3
    r = csrf_client.post("/api/carga-masiva/repartir", json=body)
    assert r.status_code == 422 and "3 horas" in r.json()["message"]
    assert (
        csrf_client.post(
            "/api/carga-masiva/repartir", json={"distribution": body["distribution"], "count": 0}
        ).status_code
        == 422
    )


# ====================================================================================== validation


def test_check_separates_row_issues_from_common_settings(
    csrf_client, db, demo_mode, demo_account, brand, videos
):
    youtube = add_account(db, brand, Network.YOUTUBE, account_type="channel")
    batch = make_batch(
        {
            str(demo_account.id): demo_conf(),
            str(youtube.id): {
                "selected": True,
                "format": "short",
                "auto_publish": False,
                "options": {"made_for_kids": None},
            },
        },
        [
            row(videos[0], "ok"),
            row(videos[1], "nodate", date="", time=""),
            row(videos[2], "past", date="2026-11-02"),
        ],
    )
    r = csrf_client.post("/api/carga-masiva/comprobar", json={"batch": batch})
    data = r.json()
    assert r.status_code == 200
    rows = data["rows"]
    assert rows["ok"]["status"] in ("ok", "warning") and not rows["ok"]["errors"]
    assert [e["code"] for e in rows["nodate"]["errors"]] == ["date_required"]
    assert [e["code"] for e in rows["past"]["errors"]] == ["date_past"]
    # "Made for kids" is set once for the whole batch: reported once, not in every row.
    codes = [e["code"] for e in data["common"]["errors"]]
    assert codes == ["youtube_made_for_kids_required"]
    assert all(e["code"] != "youtube_made_for_kids_required" for r in rows.values() for e in r["errors"])
    assert data["can_schedule"] is False
    assert data["summary"]["count"] == 2 and data["summary"]["undated"] == 1

    batch["accounts"][str(youtube.id)]["options"]["made_for_kids"] = False
    data = csrf_client.post("/api/carga-masiva/comprobar", json={"batch": batch, "rows": ["ok"]}).json()
    assert data["common"]["errors"] == [] and data["can_schedule"] is True
    assert list(data["rows"]) == ["ok"]


def test_check_without_networks(csrf_client, brand, videos):
    data = csrf_client.post(
        "/api/carga-masiva/comprobar", json={"batch": make_batch({}, [row(videos[0], "r1")])}
    ).json()
    assert [e["code"] for e in data["common"]["errors"]] == ["no_networks"]
    assert data["rows"]["r1"]["errors"] == []  # no "Selecciona al menos una red" in every row


def test_check_reports_daily_caps(csrf_client, db, brand, videos):
    tiktok = add_account(db, brand, Network.TIKTOK, scopes=["user.info.basic", "video.upload"])
    conf = {"selected": True, "format": "video", "auto_publish": True, "options": {"mode": "direct"}}
    rows = [row(videos[i % 3], f"r{i}", date="2026-11-05", time=f"{8 + 2 * i:02d}:00") for i in range(6)]
    data = csrf_client.post(
        "/api/carga-masiva/comprobar", json={"batch": make_batch({str(tiktok.id): conf}, rows)}
    ).json()
    assert [c["code"] for c in data["caps"]] == ["cap_tiktok_inbox"]  # no video.publish: inbox forced
    assert data["consent_needed"] is False


# ========================================================================================== submit


def test_schedule_all_creates_one_post_per_row_once(
    client, csrf_client, db, demo_mode, demo_account, brand, videos
):
    key = str(demo_account.id)
    batch = make_batch(
        {key: demo_conf()},
        [
            row(videos[0], "r1", text="Primer vídeo #uno", hashtags=["#uno", "dos"]),
            row(videos[1], "r2", text="Segundo", time="19:00"),
        ],
        common=["#comun"],
    )
    csrf_client.post("/api/carga-masiva/lote", json={"batch": batch})
    r = csrf_client.post("/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"})
    result = r.json()
    assert r.status_code == 200, result
    assert result["scheduled"] == 2 and result["drafts"] == 0 and result["failed"] == 0
    assert result["links"]["calendar"] == "/planificacion?fecha=2026-11-05"
    assert result["links"]["list"] == "/planificacion/lista?desde=2026-11-05&hasta=2026-11-05"

    posts = bulk_posts(db)
    assert [p.text for p in posts] == ["Primer vídeo #uno #dos #comun", "Segundo #comun"]
    assert [p.scheduled_at for p in posts] == [
        datetime(2026, 11, 5, 11, 0, tzinfo=UTC),
        datetime(2026, 11, 5, 18, 0, tzinfo=UTC),
    ]
    assert all(t.status == TargetStatus.SCHEDULED for p in posts for t in p.targets)
    assert [[m.media_id for m in p.media_links] for p in posts] == [[videos[0].id], [videos[1].id]]
    assert [x["post_id"] for x in result["posts"]] == [p.id for p in posts]

    # Double click / resubmit: same result, nothing new.
    again = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"}
    ).json()
    assert again["duplicate"] is True and [x["post_id"] for x in again["posts"]] == [p.id for p in posts]
    assert len(bulk_posts(db)) == 2

    # The batch is gone; a late autosave of it is refused; the page offers the last result.
    db.expire_all()
    assert bulk.load_batch(db, brand.id) is None
    late = csrf_client.post("/api/carga-masiva/lote", json={"batch": batch})
    assert late.status_code == 409 and late.json()["code"] == "submitted"
    state = page_state(client)
    assert state["restored"] is False and state["batch"]["token"] != TOKEN
    assert state["last_result"]["scheduled"] == 2


def test_rows_with_errors_are_saved_as_drafts(csrf_client, db, demo_mode, demo_account, brand, videos):
    batch = make_batch(
        {str(demo_account.id): demo_conf()},
        [
            row(videos[0], "good"),
            row(videos[1], "nodate", date="", time=""),
            row(videos[2], "past", date="2026-11-01"),
        ],
    )
    result = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"}
    ).json()
    assert result["scheduled"] == 1 and result["drafts"] == 2
    by_row = {p["row_id"]: p for p in result["posts"]}
    assert by_row["good"]["status"] == "scheduled" and by_row["good"]["reasons"] == []
    assert by_row["nodate"]["status"] == "draft"
    assert by_row["nodate"]["reasons"] == ["Elige la fecha y la hora de publicación."]
    assert by_row["past"]["reasons"] == ["La fecha de publicación está en el pasado."]
    statuses = {p.id: {t.status for t in p.targets} for p in bulk_posts(db)}
    assert statuses[by_row["nodate"]["post_id"]] == {TargetStatus.DRAFT}
    assert statuses[by_row["good"]["post_id"]] == {TargetStatus.SCHEDULED}


def test_save_all_as_drafts(csrf_client, db, demo_mode, demo_account, brand, videos):
    batch = make_batch(
        {str(demo_account.id): demo_conf()}, [row(videos[0], "a"), row(videos[1], "b", date="")]
    )
    result = csrf_client.post("/api/carga-masiva/programar", json={"batch": batch, "action": "draft"}).json()
    assert result["scheduled"] == 0 and result["drafts"] == 2 and result["auto_rows"] == 0
    assert all(p["reasons"] == [] for p in result["posts"])
    assert {t.status for p in bulk_posts(db) for t in p.targets} == {TargetStatus.DRAFT}


def test_pseudo_demo_key_creates_the_demo_account(client, csrf_client, db, demo_mode, brand, videos):
    batch = make_batch({"demo": demo_conf()}, [row(videos[0], "a")])
    csrf_client.post("/api/carga-masiva/lote", json={"batch": batch})
    data = csrf_client.post("/api/carga-masiva/comprobar", json={"batch": batch}).json()
    assert data["common"]["errors"] == [] and not data["rows"]["a"]["errors"]
    demo = db.scalars(
        select(Account).where(Account.brand_id == brand.id, Account.network == Network.DEMO)
    ).one()
    # Reloading the page moves the stored settings to the account id (the composer's key).
    state = page_state(client)
    assert state["batch"]["accounts"][str(demo.id)]["selected"] is True
    result = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"}
    ).json()
    assert result["scheduled"] == 1
    assert [t.account_id for t in bulk_posts(db)[0].targets] == [demo.id]


def test_per_row_network_override(csrf_client, db, demo_mode, demo_account, brand, videos):
    youtube = add_account(db, brand, Network.YOUTUBE, account_type="channel")
    batch = make_batch(
        {
            str(demo_account.id): demo_conf(),
            str(youtube.id): {
                "selected": True,
                "format": "short",
                "auto_publish": False,
                "options": {"made_for_kids": False},
            },
        },
        [
            row(videos[0], "both", text="Título del primero\nDescripción"),
            row(videos[1], "demo-only", networks={str(youtube.id): False}),
        ],
        yt_title_source="text",
    )
    result = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"}
    ).json()
    assert result["scheduled"] == 2, result
    first, second = bulk_posts(db)
    assert sorted(str(t.network) for t in first.targets) == ["demo", "youtube"]
    assert [str(t.network) for t in second.targets] == ["demo"]
    yt = next(t for t in first.targets if t.network == Network.YOUTUBE)
    assert yt.options["title"] == "Título del primero" and yt.options["made_for_kids"] is False
    assert yt.auto_publish is False


def test_youtube_title_from_file_name(csrf_client, db, brand, videos):
    youtube = add_account(db, brand, Network.YOUTUBE, account_type="channel")
    batch = make_batch(
        {
            str(youtube.id): {
                "selected": True,
                "format": "short",
                "auto_publish": False,
                "options": {"made_for_kids": False},
            }
        },
        [row(videos[0], "a", text="Texto"), row(videos[1], "b", text="Otro", yt_title="Mi título")],
        yt_title_source="filename",
    )
    csrf_client.post("/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"})
    titles = [p.targets[0].options["title"] for p in bulk_posts(db)]
    assert titles == ["uno receta", "Mi título"]


def _tiktok(db, brand, **kw) -> Account:
    info = {
        "privacy_level_options": ["SELF_ONLY"],
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": False,
        "max_video_post_duration_sec": 600,
    }
    return add_account(db, brand, Network.TIKTOK, account_type="creator", meta={"creator_info": info}, **kw)


def test_tiktok_direct_needs_the_music_consent(csrf_client, db, brand, videos):
    tiktok = _tiktok(db, brand)
    conf = {
        "selected": True,
        "format": "video",
        "auto_publish": True,
        "options": {"mode": "direct", "privacy_level": "SELF_ONLY"},
    }
    batch = make_batch({str(tiktok.id): conf}, [row(videos[0], "a"), row(videos[1], "b")])
    data = csrf_client.post("/api/carga-masiva/comprobar", json={"batch": batch}).json()
    assert data["consent_needed"] is True
    assert [e["code"] for e in data["common"]["errors"]] == ["tiktok_consent"]
    assert all(e["code"] != "tiktok_consent" for r in data["rows"].values() for e in r["errors"])

    r = csrf_client.post("/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"})
    assert r.status_code == 422 and r.json()["code"] == "common_errors"
    assert "Confirmación de uso de música" in r.json()["message"]
    assert bulk_posts(db) == []

    r = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": batch, "action": "schedule", "tiktok_consent": True}
    )
    assert r.status_code == 200 and r.json()["scheduled"] == 2, r.json()
    targets = [t for p in bulk_posts(db) for t in p.targets]
    assert all(t.options["consent"] is True and t.options["mode"] == "direct" for t in targets)
    assert all(t.options["privacy_level"] == "SELF_ONLY" for t in targets)


def test_tiktok_privacy_is_required_once(csrf_client, db, brand, videos):
    tiktok = _tiktok(db, brand)
    conf = {"selected": True, "format": "video", "auto_publish": True, "options": {"mode": "direct"}}
    batch = make_batch({str(tiktok.id): conf}, [row(videos[0], "a"), row(videos[1], "b")])
    data = csrf_client.post(
        "/api/carga-masiva/comprobar", json={"batch": batch, "tiktok_consent": True}
    ).json()
    assert [e["code"] for e in data["common"]["errors"]] == ["tiktok_privacy_required"]


def test_tiktok_without_video_publish_goes_to_the_inbox(csrf_client, db, brand, videos):
    tiktok = _tiktok(db, brand, scopes=["user.info.basic", "video.upload"])
    conf = {"selected": True, "format": "video", "auto_publish": True, "options": {"mode": "direct"}}
    batch = make_batch({str(tiktok.id): conf}, [row(videos[0], "a")])
    result = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"}
    ).json()
    assert result["scheduled"] == 1, result
    target = bulk_posts(db)[0].targets[0]
    assert target.options["mode"] == "inbox" and "consent" not in target.options


def test_failed_rows_stay_in_a_new_batch(csrf_client, db, demo_mode, demo_account, brand, videos):
    batch = make_batch({str(demo_account.id): demo_conf()}, [row(videos[0], "ok"), row(videos[1], "gone")])
    videos[1].deleted_at = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    db.commit()
    result = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"}
    ).json()
    assert result["scheduled"] == 1 and result["failed"] == 1
    assert result["failures"][0]["row_id"] == "gone"
    remaining = result["remaining_batch"]
    assert [r["id"] for r in remaining["rows"]] == ["gone"] and remaining["token"] != TOKEN
    db.expire_all()
    assert bulk.load_batch(db, brand.id)["token"] == remaining["token"]


def test_submit_while_another_one_is_running(csrf_client, db, demo_mode, demo_account, brand, videos, clock):
    batch = make_batch({str(demo_account.id): demo_conf()}, [row(videos[0], "a")])
    owned, _ = bulk.claim_submission(db, TOKEN, brand.id, clock.now())
    assert owned
    owned_again, state = bulk.claim_submission(db, TOKEN, brand.id, clock.now())
    assert owned_again is False and state["status"] == "running"
    r = csrf_client.post("/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"})
    assert r.status_code == 409 and r.json()["code"] == "in_progress"
    assert bulk_posts(db) == []
    # Only a batch that was scheduled refuses autosaves ("submitted"); one being saved does not.
    assert csrf_client.post("/api/carga-masiva/lote", json={"batch": batch}).status_code == 200


def test_an_abandoned_submit_is_resumed_without_duplicates(
    csrf_client, db, demo_mode, demo_account, brand, videos, clock
):
    batch = make_batch({str(demo_account.id): demo_conf()}, [row(videos[0], "a"), row(videos[1], "b")])
    # First attempt: row "a" was created, then the app closed before row "b".
    first = csrf_client.post(
        "/api/carga-masiva/programar",
        json={"batch": make_batch(batch["accounts"], batch["rows"][:1]), "action": "schedule"},
    ).json()
    post_a = first["posts"][0]
    setting = db.get(Setting, bulk.submit_key(TOKEN))
    setting.value = {"status": "running", "brand_id": brand.id, "done": {"a": post_a}}
    setting.updated_at = clock.now() - timedelta(minutes=30)
    db.commit()
    result = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"}
    ).json()
    assert result["scheduled"] == 2
    assert result["posts"][0]["post_id"] == post_a["post_id"]
    assert len(bulk_posts(db)) == 2  # "a" was not created twice


def test_schedule_without_rows_or_networks(csrf_client, brand, videos):
    r = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": make_batch({}, []), "action": "schedule"}
    )
    assert r.status_code == 422 and r.json()["code"] == "no_rows"
    r = csrf_client.post(
        "/api/carga-masiva/programar",
        json={"batch": make_batch({}, [row(videos[0], "a")]), "action": "schedule"},
    )
    assert r.status_code == 422 and r.json()["code"] == "common_errors"
    r = csrf_client.post(
        "/api/carga-masiva/programar",
        json={"batch": make_batch({}, [row(videos[0], "a")]), "action": "publish_now"},
    )
    assert r.status_code == 422


def test_demo_account_needs_demo_mode(csrf_client, db, demo_account, brand, videos):
    config.set_setting(db, "demo_mode", False)
    batch = make_batch({str(demo_account.id): demo_conf()}, [row(videos[0], "a")])
    data = csrf_client.post("/api/carga-masiva/comprobar", json={"batch": batch}).json()
    assert [e["code"] for e in data["common"]["errors"]] == ["demo_mode_off"]


def test_posts_show_on_the_calendar_feed(client, csrf_client, db, demo_mode, demo_account, brand, videos):
    batch = make_batch({str(demo_account.id): demo_conf()}, [row(videos[0], "a", text="En el calendario")])
    csrf_client.post("/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"})
    events = client.get("/api/planificacion/eventos?start=2026-11-02&end=2026-11-09").json()
    assert any("En el calendario" in json.dumps(e, ensure_ascii=False) for e in events)
    assert db.scalars(select(PostTarget)).first().status == TargetStatus.SCHEDULED


# ===================================================================================== brand switch


def test_a_tab_of_another_brand_cannot_touch_the_active_brand(
    client, csrf_client, db, demo_mode, demo_account, brand, videos
):
    # Review: the bulk tab of brand A kept autosaving after switching to brand B in another tab, and replaced
    # B's pending batch with A's rows.
    key = str(demo_account.id)
    a_batch = make_batch({key: demo_conf()}, [row(videos[0], "a1", text="De la marca A")], brand_id=brand.id)
    assert csrf_client.post("/api/carga-masiva/lote", json={"batch": a_batch}).status_code == 200
    other = Brand(name="Marca B", color="#e91e63", timezone="Europe/Madrid")
    db.add(other)
    db.commit()
    set_active_brand(db, other.id)
    b_batch = make_batch(
        {}, [row(videos[1], "b1", text="Lote de la marca B")], token="tok_" + "b" * 20, brand_id=other.id
    )
    assert csrf_client.post("/api/carga-masiva/lote", json={"batch": b_batch}).status_code == 200

    a_batch["rows"][0]["text"] = "Editado en la pestaña de la marca A"
    distribution = {"start_date": "2026-11-04", "mode": "per_day", "per_day": 1, "slots": ["12:00"]}
    for url, body in [
        ("/api/carga-masiva/lote", {"batch": a_batch}),
        ("/api/carga-masiva/comprobar", {"batch": a_batch}),
        ("/api/carga-masiva/programar", {"batch": a_batch, "action": "schedule"}),
        ("/api/carga-masiva/repartir", {"distribution": distribution, "count": 1, "brand_id": brand.id}),
        ("/api/carga-masiva/lote/descartar", {"brand_id": brand.id}),
    ]:
        r = csrf_client.post(url, json=body)
        assert r.status_code == 409 and r.json()["code"] == "brand_changed", (url, r.text)
        assert "«Marca B»" in r.json()["message"]
    db.expire_all()
    assert [r["text"] for r in bulk.load_batch(db, other.id)["rows"]] == ["Lote de la marca B"]
    assert [r["text"] for r in bulk.load_batch(db, brand.id)["rows"]] == ["De la marca A"]
    assert bulk_posts(db) == []
    # Brand B's own page keeps working, and its batch says it is brand B's.
    state = page_state(client)
    assert state["batch"]["brand_id"] == other.id and state["batch"]["rows"][0]["id"] == "b1"
    assert csrf_client.post("/api/carga-masiva/lote", json={"batch": state["batch"]}).status_code == 200
    r = csrf_client.post("/api/carga-masiva/lote/descartar", json={"brand_id": other.id})
    assert r.status_code == 200 and r.json()["batch"]["brand_id"] == other.id


# ================================================================================ interrupted submit


def _crash_after(csrf_client, db, batch, done_rows: int, clock, brand) -> list[dict]:
    """Submit the first ``done_rows`` rows, then leave the claim as a process that died right after them
    (status running, claimed by an earlier boot, progress a moment ago)."""
    first = csrf_client.post(
        "/api/carga-masiva/programar",
        json={"batch": make_batch(batch["accounts"], batch["rows"][:done_rows]), "action": "schedule"},
    ).json()
    setting = db.get(Setting, bulk.submit_key(TOKEN))
    setting.value = {
        "status": "running",
        "brand_id": brand.id,
        "boot": "earlier-process",
        "done": {p["row_id"]: p for p in first["posts"]},
    }
    setting.updated_at = clock.now() - timedelta(seconds=20)
    db.commit()
    csrf_client.post("/api/carga-masiva/lote", json={"batch": batch})  # the page had the whole batch
    return first["posts"]


def test_after_a_crash_mid_submit_the_page_resumes_at_once(
    client, csrf_client, db, demo_mode, demo_account, brand, videos, clock
):
    # Review: after the app died mid-submit, every edit answered "ya se ha programado" (and autosave stopped)
    # and "Programar todo" said "ya se está guardando" for 10 minutes.
    batch = make_batch(
        {str(demo_account.id): demo_conf()},
        [row(videos[0], "a"), row(videos[1], "b", time="13:00"), row(videos[2], "c", time="14:00")],
    )
    done = _crash_after(csrf_client, db, batch, 1, clock, brand)

    state = page_state(client)
    assert state["restored"] is True
    assert state["interrupted"] == {"done": 1, "left": 2}
    assert [r["id"] for r in state["batch"]["rows"]] == ["b", "c"]  # "a" exists already: not editable
    page_batch = state["batch"]
    page_batch["rows"][0]["text"] = "Editado después del cierre"
    r = csrf_client.post("/api/carga-masiva/lote", json={"batch": page_batch})
    assert r.status_code == 200, r.text  # autosave keeps working

    result = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": page_batch, "action": "schedule"}
    ).json()
    assert result["scheduled"] == 3 and result.get("duplicate") is None, result
    assert [p["row_id"] for p in result["posts"]] == ["a", "b", "c"]
    assert result["posts"][0]["post_id"] == done[0]["post_id"]
    posts = bulk_posts(db)
    assert len(posts) == 3  # "a" was not created twice
    assert posts[1].text == "Editado después del cierre"
    state = page_state(client)
    assert state["interrupted"] is None and state["batch"]["rows"] == []


def test_an_old_tab_with_every_row_resumes_without_duplicates(
    csrf_client, db, demo_mode, demo_account, brand, videos, clock
):
    batch = make_batch({str(demo_account.id): demo_conf()}, [row(videos[0], "a"), row(videos[1], "b")])
    _crash_after(csrf_client, db, batch, 1, clock, brand)
    result = csrf_client.post("/api/carga-masiva/programar", json={"batch": batch, "action": "draft"}).json()
    assert [p["row_id"] for p in result["posts"]] == ["a", "b"]
    assert len(bulk_posts(db)) == 2


def test_a_crash_after_the_last_row_only_needs_finishing(
    client, csrf_client, db, demo_mode, demo_account, brand, videos, clock
):
    batch = make_batch({str(demo_account.id): demo_conf()}, [row(videos[0], "a"), row(videos[1], "b")])
    _crash_after(csrf_client, db, batch, 2, clock, brand)
    state = page_state(client)
    assert state["interrupted"] == {"done": 2, "left": 0} and state["batch"]["rows"] == []
    result = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": state["batch"], "action": "schedule"}
    ).json()
    assert result["scheduled"] == 2 and [p["row_id"] for p in result["posts"]] == ["a", "b"]
    assert len(bulk_posts(db)) == 2
    assert page_state(client)["last_result"]["scheduled"] == 2


def test_an_unexpected_error_mid_submit_is_resumed_at_once(
    monkeypatch, csrf_client, db, demo_mode, demo_account, brand, videos
):
    batch = make_batch({str(demo_account.id): demo_conf()}, [row(videos[0], "a"), row(videos[1], "b")])
    real = bulk._when_label
    calls: list[int] = []

    def failing(post, tz):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(post, tz)

    monkeypatch.setattr(bulk, "_when_label", failing)
    with pytest.raises(RuntimeError):
        csrf_client.post("/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"})
    db.expire_all()
    state = bulk.submission_state(db, TOKEN)
    assert state["status"] == "interrupted" and list(state["done"]) == ["a"]
    assert len(bulk_posts(db)) == 1  # "b" was rolled back

    monkeypatch.setattr(bulk, "_when_label", real)
    result = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": batch, "action": "schedule"}
    ).json()
    assert result["scheduled"] == 2 and [p["row_id"] for p in result["posts"]] == ["a", "b"]
    assert len(bulk_posts(db)) == 2


# ===================================================================================== caps + chips


def test_caps_count_the_posts_already_scheduled(csrf_client, db, brand, videos):
    # Review: two batches of 5 TikTok inbox drafts on the same day never warned (one of 10 did).
    tiktok = add_account(db, brand, Network.TIKTOK, scopes=["user.info.basic", "video.upload"])
    conf = {"selected": True, "format": "video", "auto_publish": True, "options": {"mode": "inbox"}}
    first = make_batch(
        {str(tiktok.id): conf}, [row(videos[i], f"a{i}", time=f"{9 + i:02d}:00") for i in range(3)]
    )
    result = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": first, "action": "schedule"}
    ).json()
    assert result["scheduled"] == 3, result

    def caps(day: str) -> list[dict]:
        second = make_batch(
            {str(tiktok.id): conf},
            [row(videos[i], f"b{i}", date=day, time=f"{15 + i:02d}:00") for i in range(3)],
            token="tok_" + "c" * 20,
        )
        return csrf_client.post("/api/carga-masiva/comprobar", json={"batch": second}).json()["caps"]

    same_day = caps("2026-11-05")
    assert [c["code"] for c in same_day] == ["cap_tiktok_inbox"]
    assert same_day[0]["count"] == 6 and same_day[0]["existing"] == 3
    assert "ya tenías programadas" in same_day[0]["message"]
    assert caps("2026-11-08") == []  # far from the scheduled ones
    # A post in the trash does not count (6 → 5, within the limit).
    post = bulk_posts(db)[0]
    post.deleted_at = datetime(2026, 11, 3, 9, 30, tzinfo=UTC)
    db.commit()
    assert caps("2026-11-05") == []


def test_one_line_text_with_hashtag_chips_has_no_tiktok_line_break_warning(csrf_client, db, brand, videos):
    # Review: the bulk tool's own hashtag chips added a blank line, so every TikTok video warned about it.
    tiktok = _tiktok(db, brand)
    conf = {
        "selected": True,
        "format": "video",
        "auto_publish": True,
        "options": {"mode": "direct", "privacy_level": "SELF_ONLY"},
    }
    batch = make_batch(
        {str(tiktok.id): conf},
        [
            row(videos[0], "one", text="Una sola línea", hashtags=["viaje", "playa"]),
            row(videos[1], "two", text="Línea 1\nLínea 2", hashtags=["viaje"]),
        ],
        common=["comun"],
    )
    data = csrf_client.post(
        "/api/carga-masiva/comprobar", json={"batch": batch, "tiktok_consent": True}
    ).json()
    warnings = {rid: [w["code"] for w in c["warnings"]] for rid, c in data["rows"].items()}
    assert "tiktok_newlines" not in warnings["one"]
    assert "tiktok_newlines" in warnings["two"]  # the user typed that line break
    result = csrf_client.post(
        "/api/carga-masiva/programar", json={"batch": batch, "action": "schedule", "tiktok_consent": True}
    ).json()
    assert result["scheduled"] == 2
    assert [p.text for p in bulk_posts(db)] == [
        "Una sola línea #viaje #playa #comun",
        "Línea 1\nLínea 2\n\n#viaje #comun",
    ]


def test_check_ignores_ids_of_rows_no_longer_in_the_batch(
    csrf_client, db, demo_mode, demo_account, brand, videos
):
    # A page can still list removed rows among the changed ones: they must not push real rows out of the
    # request (the last of 200 videos stayed "Comprobando…" for good).
    batch = make_batch(
        {str(demo_account.id): demo_conf()},
        [row(videos[0], "r1"), row(videos[1], "r2"), row(videos[2], "r3")],
    )
    stale = [f"gone{i}" for i in range(bulk.MAX_ROWS)]
    data = csrf_client.post(
        "/api/carga-masiva/comprobar", json={"batch": batch, "rows": [*stale, "r3"]}
    ).json()
    assert list(data["rows"]) == ["r3"]
