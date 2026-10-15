"""Comments inbox service (SPEC §13): polling, threading, read/resolved state and replies.

Fully offline: the Demo provider (demo mode) and a fake Facebook provider installed in the registry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from planificador import config, inbox
from planificador.models import (
    Account,
    AccountStatus,
    InboxComment,
    MediaAsset,
    MediaKind,
    Network,
    Post,
    PostMetric,
    PostTarget,
    TargetStatus,
)
from planificador.providers import registry
from planificador.providers.base import CommentData, ErrorKind, FormatSpec, Provider, ProviderError

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


class FakeCommentsProvider(Provider):
    """Facebook stand-in: returns ``self.comments`` and records replies."""

    network = "facebook"
    display_name = "Facebook"
    formats = [FormatSpec("post", "Publicación", "images_or_video")]
    supports_comments = True

    def __init__(self) -> None:
        self.comments: list[CommentData] = []
        self.fetch_error: ProviderError | None = None
        self.reply_error: ProviderError | None = None
        self.since_calls: list[datetime] = []
        self.replies: list[tuple[str, str]] = []

    def publish_step(self, ctx: Any) -> Any:  # pragma: no cover - not used
        raise NotImplementedError

    def fetch_comments(self, account, settings, http, since):  # type: ignore[override]
        self.since_calls.append(since)
        if self.fetch_error is not None:
            raise self.fetch_error
        return [c for c in self.comments if c.created_time >= since]

    def reply_comment(self, account, settings, http, comment_external_id, text):  # type: ignore[override]
        if self.reply_error is not None:
            raise self.reply_error
        self.replies.append((comment_external_id, text))
        return f"reply-{len(self.replies)}"


def cd(
    ext: str,
    text: str,
    minutes_ago: int,
    *,
    parent: str | None = None,
    obj: str = "page_1",
    author: str = "Ana",
    from_us: bool = False,
    raw: dict | None = None,
) -> CommentData:
    return CommentData(
        external_id=ext,
        parent_external_id=parent,
        object_external_id=obj,
        object_permalink=f"https://www.facebook.com/{obj}",
        author_name=author,
        author_id=f"id-{author}",
        text=text,
        created_time=NOW - timedelta(minutes=minutes_ago),
        from_us=from_us,
        raw=raw or {"id": ext},
    )


@pytest.fixture
def fake() -> FakeCommentsProvider:
    return registry.register(FakeCommentsProvider())  # type: ignore[return-value]


@pytest.fixture
def fb_account(db, brand) -> Account:
    account = Account(
        brand_id=brand.id,
        network=Network.FACEBOOK,
        external_id="page",
        name="Mi Página",
        account_type="page",
        status=AccountStatus.CONNECTED,
    )
    account.set_tokens("tok")
    db.add(account)
    db.commit()
    return account


def settings(db):
    return config.provider_settings(db)


def rows(db) -> list[InboxComment]:
    return list(db.query(InboxComment).order_by(InboxComment.id))


# ----------------------------------------------------------------------------------------- polling


def test_poll_demo_provider_stores_comments_once(db, clock, demo_mode, demo_account):
    new = inbox.poll_all(db, None, clock, settings(db))
    assert new > 0
    assert len(rows(db)) == new
    assert all(r.account_id == demo_account.id and not r.from_us and r.read_at is None for r in rows(db))
    # Second poll: deduplicated by (account, external id).
    assert inbox.poll_all(db, None, clock, settings(db)) == 0
    assert len(rows(db)) == new
    state = inbox.poll_state(db)[str(demo_account.id)]
    assert state["last_ok"] == NOW.isoformat() and "error" not in state


def test_poll_skips_demo_accounts_without_demo_mode(db, clock, demo_mode_off, demo_account):
    assert inbox.poll_all(db, None, clock, settings(db)) == 0
    assert rows(db) == []


def test_poll_skips_disconnected_and_tiktok(db, clock, brand, fake, fb_account):
    fb_account.status = AccountStatus.DISCONNECTED
    db.add(Account(brand_id=brand.id, network=Network.TIKTOK, external_id="tt", name="tt"))
    db.commit()
    fake.comments = [cd("c1", "Hola", 5)]
    assert inbox.poll_all(db, None, clock, settings(db)) == 0
    assert fake.since_calls == []


def test_poll_window_is_30_days_then_overlap(db, clock, fake, fb_account):
    inbox.poll_all(db, None, clock, settings(db))
    assert fake.since_calls[-1] == NOW - timedelta(days=30)
    clock.advance(minutes=15)
    inbox.poll_all(db, None, clock, settings(db))
    # Later polls re-read 24 h before the last success.
    assert fake.since_calls[-1] == NOW - timedelta(hours=24)


def test_poll_skips_future_comments(db, clock, fake, fb_account):
    fake.comments = [cd("c1", "ok", 5), cd("c2", "futuro", -60)]
    assert inbox.poll_all(db, None, clock, settings(db)) == 1
    assert [r.external_id for r in rows(db)] == ["c1"]


def test_poll_error_is_recorded_per_account_and_cleared(db, clock, demo_mode, demo_account, fake, fb_account):
    fake.fetch_error = ProviderError(ErrorKind.AUTH, "fb_auth", "Facebook ha caducado la sesión.")
    new = inbox.poll_all(db, None, clock, settings(db))
    assert new > 0  # the demo account was still polled
    state = inbox.poll_state(db)
    assert state[str(fb_account.id)]["error"]["kind"] == "auth"
    assert inbox.account_problem(fb_account, state) == "Facebook ha caducado la sesión."
    assert inbox.last_error(fb_account, state) == "Facebook ha caducado la sesión."
    fake.fetch_error = None
    inbox.poll_all(db, None, clock, settings(db))
    state = inbox.poll_state(db)
    assert "error" not in state[str(fb_account.id)]
    assert inbox.account_problem(fb_account, state) is None


def test_poll_unexpected_exception_does_not_propagate(db, clock, fake, fb_account):
    fake.fetch_error = RuntimeError("boom access_token=SECRET123")  # type: ignore[assignment]
    assert inbox.poll_all(db, None, clock, settings(db)) == 0
    error = inbox.poll_state(db)[str(fb_account.id)]["error"]
    assert error["kind"] == "internal"
    assert "SECRET123" not in error["detail"]


def test_account_problem_for_needs_reconnect(db, fb_account):
    fb_account.status = AccountStatus.NEEDS_RECONNECT
    fb_account.last_error = "Token revocado"
    assert inbox.account_problem(fb_account, {}) == "Token revocado"


def test_upsert_keeps_app_state_and_updates_text(db, clock, fake, fb_account):
    fake.comments = [cd("c1", "Hola", 30)]
    inbox.poll_all(db, None, clock, settings(db))
    row = rows(db)[0]
    thread = inbox.get_thread(db, row.id, fb_account.brand_id)
    inbox.set_resolved(db, thread, True, NOW)
    fake.comments = [cd("c1", "Hola (editado)", 30)]
    clock.advance(minutes=1)
    assert inbox.poll_all(db, None, clock, settings(db)) == 0
    db.refresh(row)
    assert row.text == "Hola (editado)"
    assert row.raw["_app"]["resolved_at"] == NOW.isoformat()
    assert row.read_at == NOW


def test_own_reply_found_on_network_marks_thread_answered(db, clock, fake, fb_account, brand):
    fake.comments = [
        cd("c1", "¿Abrís el domingo?", 60),
        cd("c2", "Sí, de 9 a 14", 30, parent="c1", author="Mi Página", from_us=True),
    ]
    inbox.poll_all(db, None, clock, settings(db))
    [thread] = inbox.load_threads(db, brand.id, clock)
    assert [c.external_id for c in thread.comments] == ["c1", "c2"]
    assert thread.is_answered and not thread.needs_reply
    parent = next(r for r in rows(db) if r.external_id == "c1")
    assert parent.replied_at == NOW - timedelta(minutes=30)
    our = next(r for r in rows(db) if r.external_id == "c2")
    assert our.read_at is not None


# ----------------------------------------------------------------------------------------- threads


def test_threads_group_replies_and_skip_our_own_roots(db, clock, fake, fb_account, brand):
    fake.comments = [
        cd("a", "Primero", 300),
        cd("a1", "Respuesta de otra persona", 200, parent="a", author="Luis"),
        cd("b", "Segundo", 100, obj="page_2"),
        cd("top", "Comentario de la página", 50, author="Mi Página", from_us=True),
        cd("x", "Top-level whose parent is the post", 20, parent="page_1"),
    ]
    inbox.poll_all(db, None, clock, settings(db))
    threads = inbox.load_threads(db, brand.id, clock)
    roots = [t.root.external_id for t in threads]
    assert roots == ["x", "b", "a"]  # newest activity first; "top" (only ours) skipped
    a = threads[-1]
    assert [c.external_id for c in a.comments] == ["a", "a1"]
    assert a.unread_count == 2 and a.is_unread
    assert a.last_other.external_id == "a1"
    assert a.excerpt == "Respuesta de otra persona"


def test_filter_tabs_and_search(db, clock, fake, fb_account, brand):
    fake.comments = [
        cd("a", "¿Tenéis envíos a Canarias?", 300, author="Lucía"),
        cd("b", "Me encanta la canción", 100, author="Pedro"),
    ]
    inbox.poll_all(db, None, clock, settings(db))
    threads = inbox.load_threads(db, brand.id, clock)
    a = next(t for t in threads if t.root.external_id == "a")
    inbox.mark_read(db, a, NOW)
    threads = inbox.load_threads(db, brand.id, clock)
    assert len(inbox.filter_threads(threads, "todos")) == 2
    assert [t.root.external_id for t in inbox.filter_threads(threads, "no_leidos")] == ["b"]
    assert len(inbox.filter_threads(threads, "sin_responder")) == 2
    # Accent- and case-insensitive; fewer than 3 characters is ignored.
    assert [t.root.external_id for t in inbox.filter_threads(threads, "todos", "ENVIOS")] == ["a"]
    assert [t.root.external_id for t in inbox.filter_threads(threads, "todos", "lucia")] == ["a"]
    assert len(inbox.filter_threads(threads, "todos", "ca")) == 2
    assert inbox.filter_threads(threads, "todos", "zzz-no") == []
    assert inbox.tab_counts(threads) == {"sin_responder": 2, "no_leidos": 1, "todos": 2}
    assert inbox.normalize_tab("raro") == "sin_responder"


def test_network_filter(db, clock, demo_mode, demo_account, fake, fb_account, brand):
    fake.comments = [cd("a", "Hola", 30)]
    inbox.poll_all(db, None, clock, settings(db))
    only_fb = inbox.load_threads(db, brand.id, clock, network="facebook", demo_mode=True)
    assert {t.network for t in only_fb} == {"facebook"}
    everything = inbox.load_threads(db, brand.id, clock, demo_mode=True)
    assert {t.network for t in everything} == {"facebook", "demo"}
    assert {t.network for t in inbox.load_threads(db, brand.id, clock, demo_mode=False)} == {"facebook"}


def test_resolve_and_reopen_on_new_comment(db, clock, fake, fb_account, brand):
    fake.comments = [cd("a", "Hola", 60)]
    inbox.poll_all(db, None, clock, settings(db))
    [thread] = inbox.load_threads(db, brand.id, clock)
    inbox.set_resolved(db, thread, True, NOW)
    [thread] = inbox.load_threads(db, brand.id, clock)
    assert thread.is_resolved and not thread.needs_reply and not thread.is_unread
    # A new comment in the thread re-opens it.
    clock.advance(minutes=30)
    fake.comments.append(cd("a2", "¿Me respondéis?", -20, parent="a"))
    inbox.poll_all(db, None, clock, settings(db))
    [thread] = inbox.load_threads(db, brand.id, clock)
    assert not thread.is_resolved and thread.needs_reply
    inbox.set_resolved(db, thread, False, NOW)
    assert "_app" not in thread.root.raw


def test_mark_unread(db, clock, fake, fb_account, brand):
    fake.comments = [cd("a", "Hola", 60), cd("a1", "Otra", 30, parent="a")]
    inbox.poll_all(db, None, clock, settings(db))
    [thread] = inbox.load_threads(db, brand.id, clock)
    assert inbox.mark_read(db, thread, NOW) == 2
    assert inbox.mark_read(db, thread, NOW, read=False) == 1
    [thread] = inbox.load_threads(db, brand.id, clock)
    assert thread.unread_count == 1


def test_get_thread_checks_brand(db, clock, fake, fb_account, brand):
    fake.comments = [cd("a", "Hola", 60), cd("a1", "Otra", 30, parent="a")]
    inbox.poll_all(db, None, clock, settings(db))
    reply_row = next(r for r in rows(db) if r.external_id == "a1")
    thread = inbox.get_thread(db, reply_row.id, brand.id)
    assert thread.root.external_id == "a"
    with pytest.raises(LookupError):
        inbox.get_thread(db, reply_row.id, brand.id + 99)
    with pytest.raises(LookupError):
        inbox.get_thread(db, 999_999, brand.id)


def test_post_context_from_our_post_and_metrics(db, clock, fake, fb_account, brand, data_dir):
    asset = MediaAsset(
        brand_id=brand.id,
        kind=MediaKind.IMAGE,
        original_name="foto.jpg",
        rel_path="x/foto.jpg",
        sha256="ab" * 32,
    )
    db.add(asset)
    post = Post(brand_id=brand.id, text="Nuestro post")
    post.set_media([asset])
    db.add(post)
    db.flush()
    db.add(
        PostTarget(
            post_id=post.id,
            account_id=fb_account.id,
            network=Network.FACEBOOK,
            status=TargetStatus.PUBLISHED,
            external_id="page_1",
            external_url="https://www.facebook.com/page_1",
        )
    )
    db.add(
        PostMetric(
            account_id=fb_account.id,
            external_id="page_2",
            caption="Publicación leída de la red",
            thumbnail_url="https://cdn.example/thumb.jpg",
            permalink="https://www.facebook.com/page_2",
        )
    )
    db.commit()
    fake.comments = [
        cd("a", "Hola", 60, obj="page_1"),
        cd("b", "Hey", 30, obj="page_2"),
        cd("c", "?", 10, obj="z"),
    ]
    inbox.poll_all(db, None, clock, settings(db))
    threads = {t.root.external_id: t for t in inbox.load_threads(db, brand.id, clock)}
    inbox.attach_contexts(db, list(threads.values()))
    ours = threads["a"].context
    assert ours.caption == "Nuestro post" and ours.post_id == post.id
    assert ours.thumb_url == f"/medios/{asset.id}/miniatura"
    assert ours.permalink == "https://www.facebook.com/page_1"
    theirs = threads["b"].context
    assert theirs.caption == "Publicación leída de la red"
    assert theirs.thumb_url == "https://cdn.example/thumb.jpg"
    assert threads["c"].context.permalink == "https://www.facebook.com/z"  # comment's object permalink
    assert threads["c"].context.caption is None


def test_youtube_context_fallbacks(db, clock, brand):
    account = Account(brand_id=brand.id, network=Network.YOUTUBE, external_id="UC1", name="Canal")
    db.add(account)
    db.flush()
    db.add(
        InboxComment(
            account_id=account.id,
            external_id="yc1",
            object_external_id="VID",
            author_name="Fan",
            text="Gran vídeo",
            created_time=NOW - timedelta(hours=1),
            raw={"can_reply": False},
        )
    )
    db.commit()
    [thread] = inbox.load_threads(db, brand.id, clock)
    inbox.attach_contexts(db, [thread])
    assert thread.context.thumb_url == "https://i.ytimg.com/vi/VID/mqdefault.jpg"
    assert thread.context.permalink == "https://www.youtube.com/watch?v=VID"
    assert thread.reply_block_reason == "YouTube no permite responder a este comentario."


# ------------------------------------------------------------------------------------------ replies


def test_reply_stores_our_comment_in_the_thread(db, clock, fake, fb_account, brand):
    fake.comments = [
        cd("a", "¿Precio?", 60),
        cd("a1", "Yo también quiero saberlo", 30, parent="a", author="Luis"),
    ]
    inbox.poll_all(db, None, clock, settings(db))
    [thread] = inbox.load_threads(db, brand.id, clock)
    luis = thread.comments[1]
    clock.advance(minutes=5)
    row = inbox.reply(db, None, clock, settings(db), thread, "  Son 3 €  ", reply_to=luis)
    assert fake.replies == [("a1", "Son 3 €")]  # the provider resolves the network-specific parent
    assert row.from_us and row.external_id == "reply-1" and row.text == "Son 3 €"
    assert row.parent_external_id == "a" and row.raw["_app"]["thread_root"] == "a"
    assert row.author_name == "Mi Página"
    [thread] = inbox.load_threads(db, brand.id, clock)
    assert thread.is_answered and not thread.is_unread and not thread.needs_reply
    assert thread.comments[-1].id == row.id
    # The next poll returns our reply with network data: still one row, still ours and grouped.
    fake.comments.append(cd("reply-1", "Son 3 €", -5, parent="a", author="Mi Página", from_us=True))
    inbox.poll_all(db, None, clock, settings(db))
    [thread] = inbox.load_threads(db, brand.id, clock)
    assert len(thread.comments) == 3


def test_reply_validation_and_provider_errors(db, clock, fake, fb_account, brand):
    fake.comments = [cd("a", "Hola", 60)]
    inbox.poll_all(db, None, clock, settings(db))
    [thread] = inbox.load_threads(db, brand.id, clock)
    with pytest.raises(inbox.InboxError) as err:
        inbox.reply(db, None, clock, settings(db), thread, "   ")
    assert err.value.code == "empty"
    with pytest.raises(inbox.InboxError) as err:
        inbox.reply(db, None, clock, settings(db), thread, "x" * 9000)
    assert err.value.code == "too_long"
    fake.reply_error = ProviderError(ErrorKind.PERMANENT, "fb_closed", "Los comentarios están cerrados.")
    with pytest.raises(inbox.InboxError) as err:
        inbox.reply(db, None, clock, settings(db), thread, "Hola")
    assert err.value.message == "Los comentarios están cerrados." and err.value.status_code == 409
    fake.reply_error = ProviderError(ErrorKind.TRANSIENT, "fb_down", "Facebook no responde.")
    with pytest.raises(inbox.InboxError) as err:
        inbox.reply(db, None, clock, settings(db), thread, "Hola")
    assert err.value.status_code == 502
    fake.reply_error = ProviderError(ErrorKind.AUTH, "fb_auth", "Reconecta Facebook.")
    with pytest.raises(inbox.InboxError):
        inbox.reply(db, None, clock, settings(db), thread, "Hola")
    assert inbox.account_problem(fb_account, inbox.poll_state(db)) == "Reconecta Facebook."
    assert [r.external_id for r in rows(db)] == ["a"]


def test_reply_blocked_when_account_needs_reconnect(db, clock, fake, fb_account, brand):
    fake.comments = [cd("a", "Hola", 60)]
    inbox.poll_all(db, None, clock, settings(db))
    fb_account.status = AccountStatus.NEEDS_RECONNECT
    db.commit()
    [thread] = inbox.load_threads(db, brand.id, clock)
    assert not thread.can_reply
    with pytest.raises(inbox.InboxError) as err:
        inbox.reply(db, None, clock, settings(db), thread, "Hola")
    assert err.value.code == "cannot_reply"
    assert fake.replies == []


def test_reply_with_demo_provider(db, clock, demo_mode, demo_account, brand):
    inbox.poll_all(db, None, clock, settings(db))
    thread = inbox.load_threads(db, brand.id, clock, demo_mode=True)[0]
    row = inbox.reply(db, None, clock, settings(db), thread, "¡Gracias por comentar!")
    assert row.external_id.startswith("demo-reply-")
    assert inbox.summary(db, brand.id, clock, demo_mode=True)["todos"] >= 1


def test_supports_comments():
    assert inbox.supports_comments("demo")
    assert not inbox.supports_comments("tiktok")
    assert not inbox.supports_comments("nope")
