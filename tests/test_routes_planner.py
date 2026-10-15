"""Planificación routes: calendar feed (floating wall times, DST), filters, drag & drop validation, Lista bulk
actions, Ideas stage moves, Papelera restore/purge, brand time zone change and best times.

Post mutations go through ``planificador.postsvc`` (Composer agent). When that module (or one of the functions
used here) is not available yet, a small contract-faithful fake is installed for these tests only.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planificador.models import (
    Account,
    AccountStatus,
    Brand,
    IdeaStage,
    Post,
    PostEvent,
    PostKind,
    PostTarget,
    TargetStatus,
)
from planificador.routes import planner

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)  # conftest clock: Tuesday 10:00 in Europe/Madrid
WEEK = {"start": "2026-11-02T00:00:00Z", "end": "2026-11-09T00:00:00Z"}
HTMX = {"HX-Request": "true"}
JSON_ACCEPT = {"Accept": "application/json"}

# ----------------------------------------------------------------------------------------- postsvc


class _FakePostError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_MOVABLE = {TargetStatus.DRAFT, TargetStatus.SCHEDULED}


def _fake_postsvc() -> types.ModuleType:
    """Minimal stand-in following the postsvc contract (only what the planner calls)."""
    mod = types.ModuleType("planificador.postsvc")
    mod.PostError = _FakePostError  # type: ignore[attr-defined]

    def reschedule_post(db: Session, post_id: int, new_scheduled_at: datetime, clock: Any) -> Post:
        post = db.get(Post, post_id)
        assert post is not None
        if new_scheduled_at <= clock.now():
            raise _FakePostError("La fecha de publicación está en el pasado.")
        active = [t for t in post.targets if t.status != TargetStatus.CANCELLED]
        if any(t.status not in _MOVABLE for t in active):
            raise _FakePostError("Solo se pueden reprogramar borradores o publicaciones programadas.")
        post.scheduled_at = new_scheduled_at
        for t in active:
            t.scheduled_at = new_scheduled_at
        db.flush()
        return post

    def duplicate_post(db: Session, post_id: int, clock: Any) -> Post:
        src = db.get(Post, post_id)
        assert src is not None
        copy = Post(
            brand_id=src.brand_id,
            kind=src.kind,
            idea_stage=src.idea_stage,
            text=src.text,
            scheduled_at=src.scheduled_at,
            notes=src.notes,
            source="duplicate",
        )
        copy.set_media([link.media_id for link in src.media_links])
        for t in src.targets:
            copy.targets.append(
                PostTarget(
                    account_id=t.account_id,
                    network=t.network,
                    format=t.format,
                    text_override=t.text_override,
                    options=dict(t.options or {}),
                    auto_publish=t.auto_publish,
                    status=TargetStatus.DRAFT,
                    scheduled_at=t.scheduled_at,
                )
            )
        db.add(copy)
        db.flush()
        return copy

    def delete_post(db: Session, post_id: int, clock: Any) -> None:
        post = db.get(Post, post_id)
        assert post is not None
        post.deleted_at = clock.now()
        for t in post.targets:
            if t.status == TargetStatus.SCHEDULED:
                t.status = TargetStatus.CANCELLED
        db.flush()

    def restore_post(db: Session, post_id: int, clock: Any) -> None:
        post = db.get(Post, post_id)
        assert post is not None
        post.deleted_at = None
        for t in post.targets:
            if t.status == TargetStatus.CANCELLED:
                t.status = TargetStatus.DRAFT
        db.flush()

    mod.reschedule_post = reschedule_post  # type: ignore[attr-defined]
    mod.duplicate_post = duplicate_post  # type: ignore[attr-defined]
    mod.delete_post = delete_post  # type: ignore[attr-defined]
    mod.restore_post = restore_post  # type: ignore[attr-defined]
    return mod


_REQUIRED = ("PostError", "reschedule_post", "duplicate_post", "delete_post", "restore_post")


@pytest.fixture(autouse=True)
def svc(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """The real ``postsvc`` when it implements the contract, else the fake above."""
    try:
        from planificador import postsvc as real  # type: ignore[attr-defined]
    except ImportError:
        real = None
    if real is not None and all(hasattr(real, name) for name in _REQUIRED):
        return real
    fake = _fake_postsvc()
    import planificador

    monkeypatch.setitem(sys.modules, "planificador.postsvc", fake)
    monkeypatch.setattr(planificador, "postsvc", fake, raising=False)
    return fake


# ------------------------------------------------------------------------------------------ data


def account_for(db: Session, brand: Brand, network: str) -> Account:
    acc = db.scalar(select(Account).where(Account.brand_id == brand.id, Account.network == network))
    if acc is None:
        acc = Account(
            brand_id=brand.id,
            network=network,
            external_id=f"{network}-{brand.id}",
            name=f"Cuenta {network}",
            status=AccountStatus.CONNECTED,
        )
        db.add(acc)
        db.flush()
    return acc


def make_post(
    db: Session,
    brand: Brand,
    *,
    at: datetime | None = None,
    text: str = "Publicación de prueba",
    nets: tuple[tuple[str, str], ...] = (("facebook", "scheduled"),),
    kind: PostKind = PostKind.SCHEDULED,
    stage: IdeaStage | None = None,
    deleted_at: datetime | None = None,
    notes: str | None = None,
) -> Post:
    post = Post(
        brand_id=brand.id,
        kind=kind,
        idea_stage=stage,
        text=text,
        scheduled_at=at,
        notes=notes,
        deleted_at=deleted_at,
    )
    for network, status in nets:
        acc = account_for(db, brand, network)
        post.targets.append(
            PostTarget(account_id=acc.id, network=network, format="post", status=status, scheduled_at=at)
        )
    db.add(post)
    db.commit()
    return post


def other_brand(db: Session) -> Brand:
    b = Brand(name="Otra marca", color="#e8590c", timezone="Europe/Madrid")
    db.add(b)
    db.commit()
    return b


def feed(client: Any, **params: Any) -> list[dict[str, Any]]:
    query = {**WEEK, **params}
    r = client.get("/api/planificacion/eventos", params=query)
    assert r.status_code == 200, r.text
    return r.json()


def toast_of(response: Any) -> dict[str, Any]:
    return json.loads(response.headers["hx-trigger"])["toast"]


# ----------------------------------------------------------------------------------------- pages


def test_calendar_page_layout(client, db, brand) -> None:
    r = client.get("/planificacion")
    assert r.status_code == 200
    html = r.text
    # Sub-tabs with the active one marked, and the brand clock "10:00 - Europe/Madrid".
    for href, label in (
        ("/planificacion", "Calendario"),
        ("/planificacion/lista", "Lista"),
        ("/ideas", "Ideas"),
        ("/papelera", "Papelera"),
    ):
        assert f'href="{href}"' in html and f"<span>{label}</span>" in html
    assert 'class="tab is-active" href="/planificacion" aria-current="page"' in html
    assert 'data-tz-clock="Europe/Madrid">10:00</span>' in html
    assert '<span class="tz-picker__name">Europe/Madrid</span>' in html
    assert 'action="/planificacion/zona-horaria"' in html
    # Toolbar pieces and the calendar mount point + vendored FullCalendar (es) + planner.js.
    for needle in (
        'placeholder="Buscar"',
        "Mejores horas",
        "Crear publicación",
        'hx-get="/publicaciones/nueva" hx-target="#modal-root"',
        'id="planner-calendar"',
        "/static/vendor/fullcalendar/index.global.min.js",
        "/static/vendor/fullcalendar/locales/es.global.min.js",
        "/static/js/planner.js",
        "/static/css/planner.css",
        'href="/importar"',
    ):
        assert needle in html, needle
    config = json.loads(html.split('id="planner-config">', 1)[1].split("</script>", 1)[0])
    assert config["tz"] == "Europe/Madrid"
    assert config["initialDate"] == "2026-11-03"
    assert config["eventsUrl"] == "/api/planificacion/eventos"
    assert config["statusLabels"]["scheduled"] == "Programado"


def test_calendar_page_query_and_default_best_network(client, db, brand) -> None:
    account_for(db, brand, "tiktok")
    db.commit()
    html = client.get("/planificacion?vista=mes&fecha=2026-12-01").text
    config = json.loads(html.split('id="planner-config">', 1)[1].split("</script>", 1)[0])
    assert config["initialView"] == "dayGridMonth"
    assert config["initialDate"] == "2026-12-01"
    assert config["defaultBest"] == "tiktok"  # first connected network shades the grid by default


def test_tab_counts(client, db, brand) -> None:
    make_post(db, brand, kind=PostKind.IDEA, stage=IdeaStage.IDEA, text="Idea 1")
    make_post(db, brand, kind=PostKind.IDEA, stage=IdeaStage.LISTA, text="Idea 2")
    make_post(db, brand, at=NOW + timedelta(days=1), deleted_at=NOW - timedelta(days=1))
    html = client.get("/planificacion/lista").text
    ideas_tab = html.split('href="/ideas"', 1)[1].split("</a>", 1)[0]
    trash_tab = html.split('href="/papelera"', 1)[1].split("</a>", 1)[0]
    assert '<span class="count-pill" title="2">2</span>' in ideas_tab
    assert '<span class="count-pill" title="1">1</span>' in trash_tab


def test_timezones_api(client) -> None:
    zones = client.get("/api/planificacion/zonas-horarias").json()
    names = [z["name"] for z in zones]
    assert names[0] == "Europe/Madrid"
    assert "America/Mexico_City" in names and "Asia/Tokyo" in names
    assert len(names) == len(set(names))
    madrid = zones[0]
    assert madrid["offset"] == "UTC+1" and madrid["common"] is True  # 3 Nov 2026: CET
    kolkata = next(z for z in zones if z["name"] == "Asia/Kolkata")
    assert kolkata["offset"] == "UTC+5:30"


# ----------------------------------------------------------------------------------------- feed


def test_feed_floating_wall_times(client, db, brand) -> None:
    acc_post = make_post(
        db,
        brand,
        at=datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
        text="Lanzamiento de la colección de otoño con descuentos para toda la semana y sorteo final",
        nets=(("instagram", "scheduled"), ("facebook", "scheduled")),
    )
    early = make_post(db, brand, at=datetime(2026, 11, 1, 23, 30, tzinfo=UTC), text="Primera hora del lunes")
    make_post(db, brand, at=datetime(2026, 11, 1, 22, 30, tzinfo=UTC), text="Domingo por la noche")
    events = feed(client)
    by_id = {e["extendedProps"]["postId"]: e for e in events}
    assert set(by_id) == {acc_post.id, early.id}
    ev = by_id[acc_post.id]
    assert ev["start"] == "2026-11-03T10:00:00"  # 09:00 UTC = 10:00 CET, no offset
    assert ev["allDay"] is False
    assert ev["startEditable"] is True and ev["durationEditable"] is False
    assert ev["classNames"] == ["st-scheduled"]
    props = ev["extendedProps"]
    assert props["networks"] == ["facebook", "instagram"]
    assert (
        props["time"] == "10:00" and props["status"] == "scheduled" and props["statusLabel"] == "Programado"
    )
    assert len(props["text"]) <= planner.EXCERPT_CHARS and props["text"].endswith("…")
    assert by_id[early.id]["start"] == "2026-11-02T00:30:00"
    assert client.get("/api/planificacion/eventos", params=WEEK).headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("tz", "instant", "wall"),
    [
        # Madrid, autumn fold (25 Oct 2026, 03:00 CEST → 02:00 CET)
        ("Europe/Madrid", datetime(2026, 10, 24, 8, 0, tzinfo=UTC), "2026-10-24T10:00:00"),
        ("Europe/Madrid", datetime(2026, 10, 25, 9, 0, tzinfo=UTC), "2026-10-25T10:00:00"),
        ("Europe/Madrid", datetime(2026, 10, 25, 0, 30, tzinfo=UTC), "2026-10-25T02:30:00"),  # 1st 02:30
        ("Europe/Madrid", datetime(2026, 10, 25, 1, 30, tzinfo=UTC), "2026-10-25T02:30:00"),  # 2nd 02:30
        # Madrid, spring gap (29 Mar 2026, 02:00 CET → 03:00 CEST)
        ("Europe/Madrid", datetime(2026, 3, 29, 0, 30, tzinfo=UTC), "2026-03-29T01:30:00"),
        ("Europe/Madrid", datetime(2026, 3, 29, 1, 30, tzinfo=UTC), "2026-03-29T03:30:00"),
        # Mexico City has no DST since 2022 (UTC−6 all year); New York switches on 1 Nov 2026.
        ("America/Mexico_City", datetime(2026, 10, 24, 15, 0, tzinfo=UTC), "2026-10-24T09:00:00"),
        ("America/New_York", datetime(2026, 11, 1, 14, 0, tzinfo=UTC), "2026-11-01T09:00:00"),
        ("America/New_York", datetime(2026, 10, 31, 13, 0, tzinfo=UTC), "2026-10-31T09:00:00"),
    ],
)
def test_feed_dst_conversion(client, db, brand, tz: str, instant: datetime, wall: str) -> None:
    brand.timezone = tz
    db.commit()
    post = make_post(db, brand, at=instant, nets=(("facebook", "published"),))
    day = wall[:10]
    events = feed(client, start=f"{day}T00:00:00Z", end=f"{day}T23:59:59Z")
    assert [(e["extendedProps"]["postId"], e["start"]) for e in events] == [(post.id, wall)]


def test_feed_range_bounds_are_local_days_across_dst(client, db, brand) -> None:
    """[start, end) are brand wall times; the week holding the fold has 169 hours."""
    inside_first = make_post(db, brand, at=datetime(2026, 10, 18, 22, 0, tzinfo=UTC))  # Mon 19 Oct 00:00 CEST
    inside_last = make_post(db, brand, at=datetime(2026, 10, 25, 22, 59, tzinfo=UTC))  # Sun 25 Oct 23:59 CET
    next_week = make_post(db, brand, at=datetime(2026, 10, 25, 23, 0, tzinfo=UTC))  # Mon 26 Oct 00:00 CET
    before = make_post(db, brand, at=datetime(2026, 10, 18, 21, 59, tzinfo=UTC))  # Sun 18 Oct 23:59 CEST
    ids = {
        e["extendedProps"]["postId"]
        for e in feed(client, start="2026-10-19T00:00:00Z", end="2026-10-26T00:00:00Z")
    }
    assert ids == {inside_first.id, inside_last.id}
    ids = {
        e["extendedProps"]["postId"]
        for e in feed(client, start="2026-10-26T00:00:00Z", end="2026-11-02T00:00:00Z")
    }
    assert next_week.id in ids and before.id not in ids


def test_feed_filters_and_search(client, db, brand) -> None:
    at = NOW + timedelta(days=1)
    social = make_post(
        db,
        brand,
        at=at,
        text="Café de especialidad",
        nets=(("facebook", "scheduled"), ("instagram", "scheduled")),
    )
    video = make_post(db, brand, at=at, text="Vídeo del taller", nets=(("tiktok", "draft"),))
    failed = make_post(
        db,
        brand,
        at=at,
        text="Oferta",
        nets=(("instagram", "error"), ("facebook", "published")),
        notes="cafetería",
    )

    def ids(**params: Any) -> set[int]:
        return {e["extendedProps"]["postId"] for e in feed(client, **params)}

    assert ids() == {social.id, video.id, failed.id}
    assert ids(redes="instagram") == {social.id, failed.id}
    assert ids(redes="facebook,tiktok") == {social.id, video.id, failed.id}
    assert ids(redes=["tiktok", "youtube"]) == {video.id}
    assert ids(estados="draft") == {video.id}
    assert ids(estados="published") == {failed.id}  # any target status matches
    assert ids(estados="error", redes="facebook") == {failed.id}
    assert ids(q="CAFE") == {social.id, failed.id}  # accent/case-insensitive, notes included
    assert ids(q="taller") == {video.id}
    assert ids(redes="desconocida") == {social.id, video.id, failed.id}  # unknown values ignored
    event = next(e for e in feed(client) if e["extendedProps"]["postId"] == failed.id)
    assert event["extendedProps"]["status"] == "error" and event["startEditable"] is False


def test_feed_excludes_trash_ideas_undated_and_other_brands(client, db, brand) -> None:
    at = NOW + timedelta(days=1)
    visible = make_post(db, brand, at=at)
    make_post(db, brand, at=at, deleted_at=NOW)
    make_post(db, brand, at=at, kind=PostKind.IDEA, stage=IdeaStage.IDEA)
    make_post(db, brand, at=None, nets=(("facebook", "draft"),))
    make_post(db, other_brand(db), at=at)
    assert [e["extendedProps"]["postId"] for e in feed(client)] == [visible.id]


def test_feed_rejects_bad_range(client) -> None:
    assert client.get("/api/planificacion/eventos", params={"start": "ayer", "end": "hoy"}).status_code == 422
    r = client.get("/api/planificacion/eventos", params={"start": "2026-11-09", "end": "2026-11-02"})
    assert r.status_code == 422
    assert client.get("/api/planificacion/eventos").status_code == 200  # defaults around today


# ------------------------------------------------------------------------------------- best times


def test_best_times_fallback_pattern(client, monkeypatch) -> None:
    from planificador import analytics

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    monkeypatch.setattr(analytics, "best_times", broken, raising=False)
    data = client.get("/api/planificacion/mejores-horas", params={"red": "instagram"}).json()
    assert data["network"] == "instagram" and data["label"] == "Instagram"
    assert data["source"] == "patron_general" and "Patrón general" in data["sourceLabel"]
    matrix, levels = data["matrix"], data["levels"]
    assert len(matrix) == 7 and all(len(row) == 24 for row in matrix)
    assert all(0.0 <= v <= 1.0 for row in matrix for v in row)
    assert {lv for row in levels for lv in row} <= {1, 2, 3, 4, 5}
    assert levels[1][20] > levels[1][4]  # Tuesday 20:00 beats 04:00
    assert data["levelLabels"] == list(planner.BEST_LEVEL_LABELS)


def test_best_times_uses_analytics(client, db, brand, monkeypatch) -> None:
    from planificador import analytics

    calls: list[tuple[Any, ...]] = []

    def fake(db_: Any, brand_id: int, network: str | None, clock: Any) -> dict[str, Any]:
        calls.append((brand_id, network))
        matrix = [[0.0] * 24 for _ in range(7)]
        matrix[0][9] = 1.0
        matrix[0][10] = 0.5
        return {"matrix": matrix, "source": "datos", "posts": 14}

    monkeypatch.setattr(analytics, "best_times", fake, raising=False)
    data = client.get("/api/planificacion/mejores-horas", params={"red": "facebook"}).json()
    assert calls == [(brand.id, "facebook")]
    assert data["source"] == "datos" and data["posts"] == 14 and "(14)" in data["sourceLabel"]
    assert data["levels"][0][9] == 5 and data["levels"][0][10] == 3 and data["levels"][3][3] == 1

    # A malformed answer falls back to the general pattern.
    monkeypatch.setattr(analytics, "best_times", lambda *a, **k: {"matrix": [[1.0]]}, raising=False)
    assert (
        client.get("/api/planificacion/mejores-horas", params={"red": "tiktok"}).json()["source"]
        == "patron_general"
    )


def test_best_times_validation(client) -> None:
    assert client.get("/api/planificacion/mejores-horas", params={"red": "myspace"}).status_code == 422
    data = client.get("/api/planificacion/mejores-horas", params={"red": "todas"}).json()
    assert data["network"] is None and data["label"] == "Todas las redes"


def test_default_best_times_pattern() -> None:
    for network in (None, "facebook", "instagram", "tiktok", "youtube"):
        matrix = planner.default_best_times(network)
        assert len(matrix) == 7 and all(len(r) == 24 for r in matrix)
        flat = [v for r in matrix for v in r]
        assert max(flat) == 1.0 and min(flat) >= 0.0
        assert matrix[2][3] < 0.1  # Wednesday 03:00 is a bad time everywhere
    tiktok, facebook = planner.default_best_times("tiktok"), planner.default_best_times("facebook")
    assert tiktok[1][22] > facebook[1][22] and facebook[1][10] > tiktok[1][10]
    assert [planner.best_level(v) for v in (0.0, 0.19, 0.2, 0.45, 0.79, 0.8, 1.0)] == [1, 1, 2, 3, 4, 5, 5]


# ---------------------------------------------------------------------------------- drag & drop


def reschedule(csrf_client: Any, post_id: int, start: str, **headers: str) -> Any:
    return csrf_client.post(
        f"/api/planificacion/publicaciones/{post_id}/reprogramar",
        data={"start": start},
        headers=headers or None,
    )


def test_reschedule_moves_post_in_brand_time(csrf_client, db, brand) -> None:
    post = make_post(
        db,
        brand,
        at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
        nets=(("facebook", "scheduled"), ("instagram", "draft"), ("tiktok", "cancelled")),
    )
    r = reschedule(csrf_client, post.id, "2026-11-06T12:30:00Z")  # FullCalendar sends wall time + "Z"
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["message"] == "Publicación movida al viernes 6 de noviembre, 12:30."
    assert body["event"]["start"] == "2026-11-06T12:30:00"
    db.expire_all()
    post = db.get(Post, post.id)
    assert post.scheduled_at == datetime(2026, 11, 6, 11, 30, tzinfo=UTC)  # 12:30 CET
    by_net = {str(t.network): t for t in post.targets}
    assert by_net["facebook"].scheduled_at == post.scheduled_at
    assert by_net["instagram"].scheduled_at == post.scheduled_at
    assert by_net["facebook"].status == TargetStatus.SCHEDULED


def test_reschedule_across_dst_and_into_the_gap(csrf_client, db, brand) -> None:
    post = make_post(db, brand, at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC), nets=(("facebook", "draft"),))
    # Summer time target: 10:00 CEST = 08:00 UTC.
    assert reschedule(csrf_client, post.id, "2027-06-01T10:00").status_code == 200
    db.expire_all()
    assert db.get(Post, post.id).scheduled_at == datetime(2027, 6, 1, 8, 0, tzinfo=UTC)
    # 02:30 on 28 Mar 2027 does not exist in Madrid: shifted forward to 03:30 CEST and explained.
    r = reschedule(csrf_client, post.id, "2027-03-28T02:30")
    assert r.status_code == 200
    assert "cambio de horario" in r.json()["message"]
    db.expire_all()
    assert db.get(Post, post.id).scheduled_at == datetime(2027, 3, 28, 1, 30, tzinfo=UTC)


def test_reschedule_rejects_past_dates(csrf_client, db, brand) -> None:
    at = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    post = make_post(db, brand, at=at)
    for start in ("2026-11-03T09:59", "2026-11-03T10:00", "2026-10-01T12:00"):  # now = 10:00 Madrid
        r = reschedule(csrf_client, post.id, start)
        assert r.status_code == 422, start
        assert r.json()["detail"] == "No se puede mover una publicación a una fecha pasada."
    assert reschedule(csrf_client, post.id, "2026-11-03T10:01").status_code == 200


def test_reschedule_rejects_published_and_running_posts(csrf_client, db, brand) -> None:
    at = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    published = make_post(db, brand, at=at, nets=(("facebook", "published"),))
    mixed = make_post(db, brand, at=at, nets=(("facebook", "published"), ("instagram", "scheduled")))
    running = make_post(db, brand, at=at, nets=(("instagram", "publishing"),))
    all_cancelled = make_post(db, brand, at=at, nets=(("instagram", "cancelled"),))
    for post in (published, mixed, running, all_cancelled):
        r = reschedule(csrf_client, post.id, "2026-11-10T10:00")
        assert r.status_code == 409, post
        assert "duplícala" in r.json()["detail"]
    db.expire_all()
    assert db.get(Post, mixed.id).scheduled_at == at
    # htmx callers get the message in X-Error-Message (toast).
    r = reschedule(csrf_client, published.id, "2026-11-10T10:00", **HTMX)
    assert r.status_code == 409 and "duplícala" in unquote(r.headers["x-error-message"])


def test_reschedule_not_found_cases(csrf_client, db, brand) -> None:
    at = NOW + timedelta(days=2)
    foreign = make_post(db, other_brand(db), at=at)
    trashed = make_post(db, brand, at=at, deleted_at=NOW)
    idea = make_post(db, brand, kind=PostKind.IDEA, stage=IdeaStage.IDEA)
    assert reschedule(csrf_client, foreign.id, "2026-11-10T10:00").status_code == 404
    assert reschedule(csrf_client, trashed.id, "2026-11-10T10:00").status_code == 404
    assert reschedule(csrf_client, idea.id, "2026-11-10T10:00").status_code == 404
    assert reschedule(csrf_client, 999_999, "2026-11-10T10:00").status_code == 404
    post = make_post(db, brand, at=at)
    assert reschedule(csrf_client, post.id, "no-es-fecha").status_code == 422
    assert reschedule(csrf_client, post.id, "").status_code == 422


def test_reschedule_requires_csrf(client, db, brand) -> None:
    post = make_post(db, brand, at=NOW + timedelta(days=2))
    r = client.post(
        f"/api/planificacion/publicaciones/{post.id}/reprogramar", data={"start": "2026-11-10T10:00"}
    )
    assert r.status_code == 403


def test_reschedule_reports_postsvc_errors(csrf_client, db, brand, svc, monkeypatch) -> None:
    post = make_post(db, brand, at=NOW + timedelta(days=2))

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise svc.PostError("La cuenta de Facebook necesita reconexión.")

    monkeypatch.setattr(svc, "reschedule_post", refuse)
    r = reschedule(csrf_client, post.id, "2026-11-10T10:00")
    assert r.status_code == 409 and r.json()["detail"] == "La cuenta de Facebook necesita reconexión."
    db.expire_all()
    assert db.get(Post, post.id).scheduled_at == NOW + timedelta(days=2)


# ------------------------------------------------------------------------------------- time zone


def test_change_timezone(csrf_client, db, brand) -> None:
    post = make_post(db, brand, at=datetime(2026, 11, 3, 15, 0, tzinfo=UTC))
    assert feed(csrf_client)[0]["start"] == "2026-11-03T16:00:00"
    r = csrf_client.post(
        "/planificacion/zona-horaria",
        data={"timezone": "America/Mexico_City", "next": "/planificacion/lista"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/planificacion/lista"
    db.expire_all()
    assert db.get(Brand, brand.id).timezone == "America/Mexico_City"
    # Same instant, new wall time; the page shows the new zone and a confirmation toast.
    assert [(e["extendedProps"]["postId"], e["start"]) for e in feed(csrf_client)] == [
        (post.id, "2026-11-03T09:00:00")
    ]
    html = csrf_client.get("/planificacion").text
    assert 'data-tz-clock="America/Mexico_City">3:00</span>' in html
    flashes = json.loads(html.split('id="flash-data">', 1)[1].split("</script>", 1)[0])
    assert [f["message"] for f in flashes] == ["Zona horaria de «Mi marca»: America/Mexico City."]
    # htmx callers reload the page; open redirects are neutralised.
    r = csrf_client.post("/planificacion/zona-horaria", data={"timezone": "Europe/Madrid"}, headers=HTMX)
    assert r.status_code == 204 and r.headers["hx-refresh"] == "true"
    r = csrf_client.post(
        "/planificacion/zona-horaria",
        data={"timezone": "Europe/Madrid", "next": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/planificacion"


@pytest.mark.parametrize("value", ["", "Marte/Olympus", "EST5EDT", "posixrules", "../../etc/passwd"])
def test_change_timezone_rejects_invalid(csrf_client, db, brand, value: str) -> None:
    r = csrf_client.post("/planificacion/zona-horaria", data={"timezone": value})
    assert r.status_code == 422
    db.expire_all()
    assert db.get(Brand, brand.id).timezone == "Europe/Madrid"


def test_change_timezone_requires_csrf(client, db, brand) -> None:
    r = client.post("/planificacion/zona-horaria", data={"timezone": "UTC"})
    assert r.status_code == 403


# ------------------------------------------------------------------------------------------- Lista


def test_list_empty_state(client, brand) -> None:
    r = client.get("/planificacion/lista")
    assert r.status_code == 200
    html = r.text
    for needle in (
        "Fecha",
        "Contenido de la publicación",
        "Redes",
        "Estado",
        "Acciones en bloque",
        "Aún no tienes publicaciones planificadas",
        "Usa el botón de arriba a la derecha para crear tu primera publicación.",
        'class="tab is-active" href="/planificacion/lista"',
        # default range: last week + next 30 days (brand-local dates)
        'name="desde" x-ref="desde" x-model="desde"',
        "27 oct 2026 - 3 dic 2026",
    ):
        assert needle in html, needle


def test_list_rows_range_search_and_filters(client, db, brand) -> None:
    soon = make_post(
        db,
        brand,
        at=datetime(2026, 11, 4, 17, 0, tzinfo=UTC),
        text="Receta de otoño",
        nets=(("instagram", "scheduled"), ("facebook", "error")),
        notes="Revisar foto",
    )
    undated = make_post(db, brand, at=None, text="Borrador sin fecha", nets=(("tiktok", "draft"),))
    later = make_post(db, brand, at=datetime(2026, 12, 20, 9, 0, tzinfo=UTC), text="Navidad")
    old = make_post(db, brand, at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC), text="Antigua")
    html = client.get("/planificacion/lista").text
    assert f'id="plan-row-{soon.id}"' in html and f'id="plan-row-{undated.id}"' in html
    assert f'id="plan-row-{later.id}"' not in html and f'id="plan-row-{old.id}"' not in html
    assert html.index(f'id="plan-row-{undated.id}"') < html.index(f'id="plan-row-{soon.id}"')  # undated first
    row = html.split(f'id="plan-row-{soon.id}"', 1)[1].split("</tr>", 1)[0]
    assert "4 nov 2026" in row and "mié · 18:00" in row  # brand-local date/time
    assert "Receta de otoño" in row and "Tiene notas internas" in row
    assert "Instagram · Programado" in row and "Facebook · Error" in row
    assert "badge st-error" in row  # derived status: error wins
    assert f'hx-get="/publicaciones/{soon.id}" hx-target="#drawer-root"' in row

    def rows(**params: Any) -> str:
        r = client.get("/planificacion/lista", params=params, headers={**HTMX, "HX-Target": "plan-list"})
        assert r.status_code == 200 and "<html" not in r.text.lower()
        return r.text

    frag = rows(desde="2026-12-01", hasta="2026-12-31")
    assert f'id="plan-row-{later.id}"' in frag and f'id="plan-row-{soon.id}"' not in frag
    assert f'id="plan-row-{undated.id}"' not in frag  # undated drafts only when the range covers today
    assert "1 dic 2026 - 31 dic 2026" in frag
    frag = rows(q="RECETA")
    assert f'id="plan-row-{soon.id}"' in frag and f'id="plan-row-{undated.id}"' not in frag
    frag = rows(redes="tiktok")
    assert f'id="plan-row-{undated.id}"' in frag and f'id="plan-row-{soon.id}"' not in frag
    frag = rows(estados="error")
    assert f'id="plan-row-{soon.id}"' in frag and f'id="plan-row-{undated.id}"' not in frag
    frag = rows(q="nada que coincida")
    assert "No hay publicaciones que coincidan" in frag
    # Reversed ranges are swapped instead of failing.
    assert "1 dic 2026 - 31 dic 2026" in rows(desde="2026-12-31", hasta="2026-12-01")


def test_bulk_duplicate(csrf_client, db, brand) -> None:
    a = make_post(db, brand, at=NOW + timedelta(days=1), text="Uno")
    b = make_post(db, brand, at=NOW + timedelta(days=2), text="Dos")
    r = csrf_client.post(
        "/planificacion/lista/acciones",
        data={
            "accion": "duplicar",
            "ids": [str(a.id), str(b.id)],
            "desde": "2026-11-01",
            "hasta": "2026-11-30",
        },
        headers=HTMX,
    )
    assert r.status_code == 200, r.text
    assert "<html" not in r.text.lower() and 'class="plan-table"' in r.text
    assert toast_of(r) == {"message": "2 publicaciones duplicadas.", "kind": "success"}
    db.expire_all()
    texts = sorted(p.text for p in db.scalars(select(Post).where(Post.deleted_at.is_(None))))
    assert texts == ["Dos", "Dos", "Uno", "Uno"]
    assert r.text.count('class="plan-row"') == 4
    assert "1 nov 2026 - 30 nov 2026" in r.text  # the list keeps the filters it was sent


def test_bulk_trash_and_single_row_action(csrf_client, db, brand) -> None:
    a = make_post(db, brand, at=NOW + timedelta(days=1), text="Uno")
    b = make_post(db, brand, at=NOW + timedelta(days=2), text="Dos")
    c = make_post(db, brand, at=NOW + timedelta(days=3), text="Tres")
    r = csrf_client.post(
        "/planificacion/lista/acciones",
        data={"accion": "papelera", "ids": [str(a.id), str(b.id)]},
        headers=HTMX,
    )
    assert r.status_code == 200
    assert toast_of(r)["message"] == "2 publicaciones movidas a la papelera."
    assert f'id="plan-row-{a.id}"' not in r.text and f'id="plan-row-{c.id}"' in r.text
    db.expire_all()
    assert db.get(Post, a.id).deleted_at is not None and db.get(Post, b.id).deleted_at is not None
    assert db.get(Post, c.id).deleted_at is None
    # Pending targets are no longer scheduled.
    assert all(t.status != TargetStatus.SCHEDULED for t in db.get(Post, a.id).targets)
    # Single row menu action (same endpoint, one id) and JSON callers.
    r = csrf_client.post(
        "/planificacion/lista/acciones", data={"accion": "papelera", "ids": str(c.id)}, headers=JSON_ACCEPT
    )
    assert r.json() == {"ok": True, "done": 1, "errors": [], "message": "Publicación movida a la papelera."}


def test_bulk_action_validation(csrf_client, client, db, brand) -> None:
    mine = make_post(db, brand, at=NOW + timedelta(days=1))
    foreign = make_post(db, other_brand(db), at=NOW + timedelta(days=1))
    trashed = make_post(db, brand, at=NOW + timedelta(days=1), deleted_at=NOW)
    url = "/planificacion/lista/acciones"
    assert csrf_client.post(url, data={"accion": "publicar", "ids": str(mine.id)}).status_code == 422
    r = csrf_client.post(url, data={"accion": "duplicar"}, headers=HTMX)
    assert (
        r.status_code == 422
        and unquote(r.headers["x-error-message"]) == "Selecciona al menos una publicación."
    )
    assert (
        csrf_client.post(url, data={"accion": "duplicar", "ids": [str(mine.id), str(foreign.id)]}).status_code
        == 404
    )
    assert csrf_client.post(url, data={"accion": "papelera", "ids": str(trashed.id)}).status_code == 404
    assert client.post(url, data={"accion": "duplicar", "ids": str(mine.id)}).status_code == 403
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Post)) == 3  # nothing duplicated


def test_bulk_action_partial_failure(csrf_client, db, brand, svc, monkeypatch) -> None:
    a = make_post(db, brand, at=NOW + timedelta(days=1), text="Uno")
    b = make_post(db, brand, at=NOW + timedelta(days=2), text="Dos")
    original = svc.duplicate_post

    def flaky(db_: Session, post_id: int, clock: Any) -> Any:
        if post_id == b.id:
            raise svc.PostError("No se puede duplicar esta publicación.")
        return original(db_, post_id, clock)

    monkeypatch.setattr(svc, "duplicate_post", flaky)
    r = csrf_client.post(
        "/planificacion/lista/acciones",
        data={"accion": "duplicar", "ids": [str(a.id), str(b.id)]},
        headers=HTMX,
    )
    assert r.status_code == 200
    assert toast_of(r) == {
        "message": "Publicación duplicada. No se puede duplicar esta publicación.",
        "kind": "warning",
    }


# ------------------------------------------------------------------------------------------- Ideas


def test_ideas_board(client, db, brand) -> None:
    first = make_post(db, brand, kind=PostKind.IDEA, stage=IdeaStage.IDEA, text="Serie de recetas", nets=())
    wip = make_post(
        db, brand, kind=PostKind.IDEA, stage=IdeaStage.EN_PROGRESO, text="Vídeo detrás de cámaras",
        nets=(("tiktok", "draft"),), notes="Grabar el jueves",
    )  # fmt: skip
    no_stage = make_post(db, brand, kind=PostKind.IDEA, stage=None, text="Sin columna", nets=())
    make_post(db, brand, kind=PostKind.IDEA, stage=IdeaStage.LISTA, text="Borrada", deleted_at=NOW)
    make_post(db, brand, at=NOW + timedelta(days=1), text="Programada")
    r = client.get("/ideas")
    assert r.status_code == 200
    html = r.text
    assert 'class="tab is-active" href="/ideas"' in html
    assert "Nueva idea" in html and 'href="/publicaciones/nueva?tipo=idea"' in html
    columns = {}
    for key in ("idea", "en_progreso", "lista"):
        columns[key] = html.split(f'data-stage="{key}"', 1)[1].split("</section>", 1)[0]
    assert f'id="idea-{first.id}"' in columns["idea"] and f'id="idea-{no_stage.id}"' in columns["idea"]
    assert (
        f'id="idea-{wip.id}"' in columns["en_progreso"] and "Tiene notas internas" in columns["en_progreso"]
    )
    assert "idea-card" not in columns["lista"] and "is-empty" in html.split('data-stage="lista"', 1)[0][-200:]
    assert "Borrada" not in html and "Programada" not in html
    assert 'href="/publicaciones/nueva?tipo=idea&amp;etapa=en_progreso"' in html
    # Card menu offers the other columns only.
    card = html.split(f'id="idea-{first.id}"', 1)[1].split("</article>", 1)[0]
    assert '"etapa": "en_progreso"' in card and '"etapa": "lista"' in card and '"etapa": "idea"' not in card


def test_idea_stage_move(csrf_client, db, brand, clock) -> None:
    idea = make_post(db, brand, kind=PostKind.IDEA, stage=IdeaStage.IDEA, text="Mover", nets=())
    clock.advance(minutes=5)
    r = csrf_client.post(f"/ideas/{idea.id}/etapa", data={"etapa": "en_progreso"}, headers=JSON_ACCEPT)
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "stage": "en_progreso",
        "label": "En progreso",
        "message": "Idea movida a «En progreso».",
    }
    db.expire_all()
    moved = db.get(Post, idea.id)
    assert moved.idea_stage == IdeaStage.EN_PROGRESO and moved.updated_at == clock.now()
    # htmx (card menu) gets the re-rendered board.
    r = csrf_client.post(
        f"/ideas/{idea.id}/etapa", data={"etapa": "lista"}, headers={**HTMX, "HX-Target": "ideas-board"}
    )
    assert r.status_code == 200 and "<html" not in r.text.lower()
    assert f'id="idea-{idea.id}"' in r.text.split('data-stage="lista"', 1)[1]
    assert toast_of(r)["message"] == "Idea movida a «Lista»."


def test_idea_stage_validation(csrf_client, client, db, brand) -> None:
    idea = make_post(db, brand, kind=PostKind.IDEA, stage=IdeaStage.IDEA, nets=())
    scheduled = make_post(db, brand, at=NOW + timedelta(days=1))
    foreign = make_post(db, other_brand(db), kind=PostKind.IDEA, stage=IdeaStage.IDEA, nets=())
    assert csrf_client.post(f"/ideas/{idea.id}/etapa", data={"etapa": "publicada"}).status_code == 422
    assert csrf_client.post(f"/ideas/{scheduled.id}/etapa", data={"etapa": "lista"}).status_code == 404
    assert csrf_client.post(f"/ideas/{foreign.id}/etapa", data={"etapa": "lista"}).status_code == 404
    assert client.post(f"/ideas/{idea.id}/etapa", data={"etapa": "lista"}).status_code == 403
    db.expire_all()
    assert db.get(Post, idea.id).idea_stage == IdeaStage.IDEA


def test_idea_card_actions(csrf_client, db, brand) -> None:
    idea = make_post(db, brand, kind=PostKind.IDEA, stage=IdeaStage.LISTA, text="Copiar", nets=())
    r = csrf_client.post(f"/ideas/{idea.id}/acciones", data={"accion": "duplicar"}, headers=HTMX)
    assert r.status_code == 200 and toast_of(r)["message"] == "Idea duplicada."
    assert r.text.count("Copiar") >= 2
    r = csrf_client.post(f"/ideas/{idea.id}/acciones", data={"accion": "papelera"}, headers=HTMX)
    assert r.status_code == 200 and toast_of(r)["message"] == "Idea movida a la papelera."
    db.expire_all()
    assert db.get(Post, idea.id).deleted_at is not None
    assert csrf_client.post(f"/ideas/{idea.id}/acciones", data={"accion": "papelera"}).status_code == 404
    assert csrf_client.post(f"/ideas/{idea.id}/acciones", data={"accion": "otra"}).status_code == 422


# ----------------------------------------------------------------------------------------- Papelera


def test_trash_page(client, db, brand) -> None:
    gone = make_post(
        db, brand, at=NOW + timedelta(days=1), text="Eliminada hace poco", deleted_at=NOW - timedelta(days=5)
    )
    old = make_post(
        db, brand, kind=PostKind.IDEA, stage=IdeaStage.IDEA, text="Idea vieja", nets=(),
        deleted_at=NOW - timedelta(days=44, hours=12),
    )  # fmt: skip
    make_post(db, brand, at=NOW + timedelta(days=1), text="Sigue viva")
    html = client.get("/papelera").text
    assert 'class="tab is-active" href="/papelera"' in html
    assert "45 días" in html and "Vaciar papelera" in html
    assert "Sigue viva" not in html
    assert html.index(f'id="trash-row-{gone.id}"') < html.index(f'id="trash-row-{old.id}"')  # newest first
    row = html.split(f'id="trash-row-{gone.id}"', 1)[1].split("</tr>", 1)[0]
    assert "Se borrará en 40 días" in row and f'hx-post="/papelera/{gone.id}/restaurar"' in row
    old_row = html.split(f'id="trash-row-{old.id}"', 1)[1].split("</tr>", 1)[0]
    assert "Se borrará mañana" in old_row and "Idea" in old_row


def test_trash_empty_state(client, brand) -> None:
    html = client.get("/papelera").text
    assert "La papelera está vacía" in html and "Vaciar papelera" not in html


def test_trash_restore(csrf_client, db, brand) -> None:
    post = make_post(db, brand, at=NOW + timedelta(days=3), text="Recuperable")
    r = csrf_client.post("/planificacion/lista/acciones", data={"accion": "papelera", "ids": str(post.id)})
    assert r.status_code == 200
    assert [e for e in feed(csrf_client) if e["extendedProps"]["postId"] == post.id] == []
    r = csrf_client.post(f"/papelera/{post.id}/restaurar", headers={**HTMX, "HX-Target": "trash-list"})
    assert r.status_code == 200 and toast_of(r)["message"] == "Publicación restaurada en el calendario."
    assert "La papelera está vacía" in r.text
    db.expire_all()
    restored = db.get(Post, post.id)
    assert restored.deleted_at is None and restored.scheduled_at == NOW + timedelta(days=3)
    assert [e["extendedProps"]["postId"] for e in feed(csrf_client)] == [post.id]
    # Not in the trash any more → 404.
    assert csrf_client.post(f"/papelera/{post.id}/restaurar").status_code == 404


def test_trash_purge(csrf_client, db, brand) -> None:
    post = make_post(db, brand, at=NOW + timedelta(days=1), deleted_at=NOW, nets=(("facebook", "cancelled"),))
    post_id, target_id = post.id, post.targets[0].id
    db.add(PostEvent(target_id=target_id, event="cancelled", message="Cancelada"))
    db.commit()
    alive = make_post(db, brand, at=NOW + timedelta(days=1))
    assert csrf_client.post(f"/papelera/{alive.id}/eliminar").status_code == 404
    r = csrf_client.post(f"/papelera/{post_id}/eliminar", headers=HTMX)
    assert r.status_code == 200 and toast_of(r)["message"] == "Publicación eliminada definitivamente."
    db.expunge_all()
    assert db.get(Post, post_id) is None
    assert db.get(PostTarget, target_id) is None
    assert db.scalar(select(func.count()).select_from(PostEvent)) == 0
    assert db.get(Post, alive.id) is not None


def test_trash_empty_all(csrf_client, client, db, brand) -> None:
    mine = [make_post(db, brand, at=NOW, deleted_at=NOW).id for _ in range(2)]
    foreign = make_post(db, other_brand(db), at=NOW, deleted_at=NOW).id
    assert client.post("/papelera/vaciar").status_code == 403
    r = csrf_client.post("/papelera/vaciar", headers=HTMX)
    assert r.status_code == 200
    assert toast_of(r)["message"] == "Se han eliminado 2 publicaciones definitivamente."
    db.expunge_all()
    assert all(db.get(Post, pid) is None for pid in mine)
    assert db.get(Post, foreign) is not None
    r = csrf_client.post("/papelera/vaciar", headers=JSON_ACCEPT)
    assert r.json() == {"ok": True, "message": "La papelera ya estaba vacía."}


# ------------------------------------------------------------------------------------------- units


def test_helpers() -> None:
    assert planner.normalize_text("Árbol CAFÉ ñu") == "arbol cafe nu"
    assert planner.split_multi(["facebook,instagram", " TikTok ", "facebook", "x"], planner.ALL_NETWORKS) == [
        "facebook",
        "instagram",
        "tiktok",
    ]
    assert planner.excerpt("  uno\n dos  ", 60) == "uno dos"
    assert planner.excerpt("a" * 70, 10) == "a" * 9 + "…"
    from datetime import date

    today = date(2026, 11, 3)
    assert planner.list_range(None, None, today) == (date(2026, 10, 27), date(2026, 12, 3))
    assert planner.list_range("2026-12-31", "2026-12-01", today) == (date(2026, 12, 1), date(2026, 12, 31))
    assert planner.list_range("basura", "2027-01-10", today) == (date(2026, 10, 27), date(2027, 1, 10))
    start, end = planner.list_range("2020-01-01", "2030-01-01", today)
    assert (end - start).days == planner.LIST_MAX_SPAN_DAYS
    assert planner.range_label(date(2026, 9, 16), date(2026, 10, 15)) == "16 sept 2026 - 15 oct 2026"


def test_actions_without_postsvc_answer_cleanly(csrf_client, db, brand, monkeypatch) -> None:
    """Until the Composer's postsvc is installed, actions fail with a clear 503 instead of a 500."""
    import planificador

    monkeypatch.setitem(sys.modules, "planificador.postsvc", None)
    monkeypatch.delattr(planificador, "postsvc", raising=False)
    post = make_post(db, brand, at=NOW + timedelta(days=1))
    r = reschedule(csrf_client, post.id, "2026-11-10T10:00")
    assert r.status_code == 503 and "todavía no está disponible" in r.json()["detail"]
    r = csrf_client.post(
        "/planificacion/lista/acciones", data={"accion": "duplicar", "ids": str(post.id)}, headers=HTMX
    )
    assert r.status_code == 503 and "x-error-message" in r.headers


def test_event_card_never_spills_into_the_next_day() -> None:
    from datetime import datetime as dt

    assert planner._event_end(dt(2026, 11, 3, 10, 30)) == "2026-11-03T11:30:00"
    assert (
        planner._event_end(dt(2026, 11, 3, 23, 30)) == "2026-11-04T00:00:00"
    )  # exclusive end: same day only
    assert planner._event_end(None) is None
