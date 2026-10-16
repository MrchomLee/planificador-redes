"""Unit tests of bulk.py ("Carga masiva"): date distribution (slots in turns, N per day, every N days,
weekends, past instants, DST in Europe/Madrid and America/Mexico_City), caption + hashtags composition,
YouTube titles, daily-limit warnings, best-time slot suggestions and the batch schema."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from planificador import bulk
from planificador.bulk import BatchError, CapEntry, DistributionSpec

MADRID = "Europe/Madrid"
MEXICO = "America/Mexico_City"
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)  # Tuesday 10:00 in Madrid, 03:00 in Mexico City


def spec(start: date, **kw) -> DistributionSpec:
    slots = tuple(time.fromisoformat(s) for s in kw.pop("slots", ("12:00", "19:00")))
    return DistributionSpec(start_date=start, slots=slots, **kw)


def walls(picks) -> list[str]:
    return [f"{p.date_iso} {p.time_hm}" for p in picks]


# ===================================================================================== distribution


def test_per_day_uses_every_slot_each_day_in_order():
    picks = bulk.distribute(5, spec(date(2026, 11, 4), per_day=2), MADRID, NOW)
    assert walls(picks) == [
        "2026-11-04 12:00",
        "2026-11-04 19:00",
        "2026-11-05 12:00",
        "2026-11-05 19:00",
        "2026-11-06 12:00",
    ]
    # Brand wall time → UTC (CET = UTC+1 in November).
    assert picks[0].when == datetime(2026, 11, 4, 11, 0, tzinfo=UTC)
    assert all(not p.shifted for p in picks)


def test_fewer_posts_per_day_than_slots_alternates_the_times():
    picks = bulk.distribute(4, spec(date(2026, 11, 4), per_day=1), MADRID, NOW)
    assert walls(picks) == ["2026-11-04 12:00", "2026-11-05 19:00", "2026-11-06 12:00", "2026-11-07 19:00"]


def test_three_slots_two_per_day_rotate_and_stay_sorted_within_a_day():
    picks = bulk.distribute(
        6, spec(date(2026, 11, 4), per_day=2, slots=("09:00", "13:00", "20:00")), MADRID, NOW
    )
    assert walls(picks) == [
        "2026-11-04 09:00",
        "2026-11-04 13:00",
        "2026-11-05 09:00",  # slots 3 and 1 of the cycle, sorted
        "2026-11-05 20:00",
        "2026-11-06 13:00",
        "2026-11-06 20:00",
    ]


def test_one_every_n_days():
    picks = bulk.distribute(3, spec(date(2026, 11, 4), mode="every_days", every_days=3), MADRID, NOW)
    assert [p.date_iso for p in picks] == ["2026-11-04", "2026-11-07", "2026-11-10"]
    assert [p.time_hm for p in picks] == ["12:00", "19:00", "12:00"]


def test_skip_weekends_per_day():
    # 2026-11-06 is a Friday.
    picks = bulk.distribute(3, spec(date(2026, 11, 6), per_day=1, skip_weekends=True), MADRID, NOW)
    assert [p.date_iso for p in picks] == ["2026-11-06", "2026-11-09", "2026-11-10"]
    assert all(datetime.fromisoformat(p.date_iso).weekday() < 5 for p in picks)


def test_skip_weekends_every_n_days_moves_to_monday_and_keeps_the_cadence():
    # Thu 5 → (+2) Sat 7 → Monday 9 → (+2) Wed 11.
    picks = bulk.distribute(
        3,
        spec(date(2026, 11, 5), mode="every_days", every_days=2, skip_weekends=True, slots=("10:00",)),
        MADRID,
        NOW,
    )
    assert [p.date_iso for p in picks] == ["2026-11-05", "2026-11-09", "2026-11-11"]


def test_past_and_too_close_instants_are_skipped():
    # Now is 10:00 in Madrid: 09:00 is past, 10:05 is closer than MIN_LEAD (10 min), 10:30 is fine.
    picks = bulk.distribute(
        3, spec(date(2026, 11, 3), per_day=3, slots=("09:00", "10:05", "10:30")), MADRID, NOW
    )
    assert walls(picks) == ["2026-11-03 10:30", "2026-11-04 09:00", "2026-11-04 10:05"]


def test_count_zero_and_errors():
    assert bulk.distribute(0, spec(date(2026, 11, 4)), MADRID, NOW) == []
    with pytest.raises(BatchError):
        bulk.distribute(2, spec(date(2026, 11, 4), per_day=3), MADRID, NOW)
    with pytest.raises(BatchError):
        bulk.distribute(2, spec(date(2026, 11, 4), slots=()), MADRID, NOW)


def test_madrid_dst_end_keeps_wall_time_and_changes_utc_offset():
    # Last Sunday of October 2026 (25th): CEST (UTC+2) → CET (UTC+1).
    now = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    picks = bulk.distribute(3, spec(date(2026, 10, 24), per_day=1, slots=("12:00",)), MADRID, now)
    assert walls(picks) == ["2026-10-24 12:00", "2026-10-25 12:00", "2026-10-26 12:00"]
    assert [p.when.hour for p in picks] == [10, 11, 11]


def test_madrid_dst_gap_moves_the_time_forward_with_a_note():
    # 2027-03-28 02:30 does not exist in Madrid (02:00 → 03:00).
    now = datetime(2027, 3, 20, 8, 0, tzinfo=UTC)
    picks = bulk.distribute(2, spec(date(2027, 3, 28), per_day=1, slots=("02:30",)), MADRID, now)
    assert walls(picks) == ["2027-03-28 03:30", "2027-03-29 02:30"]
    assert picks[0].shifted and not picks[1].shifted
    assert picks[0].when == datetime(2027, 3, 28, 1, 30, tzinfo=UTC)
    note = bulk.pick_json(picks[0], MADRID)["note"]
    assert "02:30" in note and "03:30" in note and "cambio de hora" in note


def test_madrid_dst_fold_uses_the_first_occurrence():
    now = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    picks = bulk.distribute(1, spec(date(2026, 10, 25), slots=("02:30",)), MADRID, now)
    assert picks[0].when == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST
    assert not picks[0].shifted


def test_mexico_city_has_no_dst_since_2022():
    # The old switch dates (first Sunday of April, last Sunday of October) no longer move the clock.
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    for start in (date(2026, 4, 4), date(2026, 10, 24)):
        picks = bulk.distribute(3, spec(start, per_day=1, slots=("12:00",)), MEXICO, now)
        assert [p.time_hm for p in picks] == ["12:00"] * 3
        assert {p.when.hour for p in picks} == {18}  # always UTC-6
        assert not any(p.shifted for p in picks)


def test_mexico_city_today_starts_at_the_next_future_slot():
    # NOW is 03:00 in Mexico City on 3 Nov: both slots today are still ahead.
    picks = bulk.distribute(2, spec(date(2026, 11, 3), per_day=2), MEXICO, NOW)
    assert walls(picks) == ["2026-11-03 12:00", "2026-11-03 19:00"]
    assert picks[0].when == datetime(2026, 11, 3, 18, 0, tzinfo=UTC)


def test_parse_spec_validates_and_clamps():
    today = date(2026, 11, 3)
    s = bulk.parse_spec(
        {"start_date": "2026-10-01", "mode": "per_day", "per_day": "2", "slots": ["19h", "9:05", "12.30"]},
        today=today,
    )
    assert s.start_date == today  # a past start is moved to today
    assert s.slots == (time(9, 5), time(12, 30), time(19, 0))
    with pytest.raises(BatchError, match="al menos 3 horas"):
        bulk.parse_spec({"per_day": 3, "slots": ["12:00", "19:00"]}, today=today)
    with pytest.raises(BatchError):
        bulk.parse_spec({"slots": ["25:00"]}, today=today)
    with pytest.raises(BatchError):
        bulk.parse_spec({"slots": []}, today=today)
    with pytest.raises(BatchError):
        bulk.parse_spec({"mode": "monthly", "slots": ["12:00"]}, today=today)
    with pytest.raises(BatchError):
        bulk.parse_spec({"mode": "every_days", "every_days": 0, "slots": ["12:00"]}, today=today)


def test_schedule_summary_counts_per_local_day():
    picks = bulk.distribute(5, spec(date(2026, 11, 7), per_day=2), MADRID, NOW)
    summary = bulk.schedule_summary([p.when for p in picks] + [None], MADRID)
    assert summary["count"] == 5 and summary["undated"] == 1
    assert summary["days"] == 3 and summary["max_per_day"] == 2
    assert [d["count"] for d in summary["per_day"]] == [2, 2, 1]
    assert summary["per_day"][0]["weekend"] is True  # Saturday 7
    assert summary["first_date"] == "2026-11-07" and summary["last_date"] == "2026-11-09"
    assert summary["first_label"].startswith("sábado 7 de noviembre de 2026, 12:00")


def test_suggested_slots_from_best_times():
    from planificador import analytics

    assert bulk.suggested_slots(analytics.general_pattern(None)) == ["13:00", "20:00"]
    matrix = [[0.0] * 24 for _ in range(7)]
    for d in range(5):
        matrix[d][9] = 1.0
        matrix[d][10] = 0.95  # too close to 9:00
        matrix[d][18] = 0.9
    assert bulk.suggested_slots(matrix) == ["09:00", "18:00"]
    assert bulk.suggested_slots(None) == list(bulk.DEFAULT_SLOTS)
    assert bulk.suggested_slots([[1, 2]]) == list(bulk.DEFAULT_SLOTS)


# ========================================================================================= captions


def test_normalize_hashtags():
    assert bulk.normalize_hashtags("#viaje, #Playa verano;#playa ##sol") == [
        "viaje",
        "Playa",
        "verano",
        "sol",
    ]
    assert bulk.normalize_hashtags(["#hola-mundo", "café", "", None, 12]) == ["holamundo", "café", "12"]
    assert bulk.normalize_hashtags(" ".join(f"t{i}" for i in range(100)), limit=5) == [
        f"t{i}" for i in range(5)
    ]
    assert bulk.normalize_hashtags(None) == [] and bulk.normalize_hashtags("") == []


def test_compose_caption_appends_row_then_common_hashtags():
    caption = bulk.compose_caption("Hola mundo\nSegunda línea\n", ["#viaje", "playa"], ["#verano"])
    assert caption == "Hola mundo\nSegunda línea\n\n#viaje #playa #verano"


def test_compose_caption_keeps_a_one_line_text_on_one_line():
    # No line break the user did not type (TikTok would warn that it may join the lines).
    assert bulk.compose_caption("Hola mundo\n", ["#viaje"], ["#verano"]) == "Hola mundo #viaje #verano"


def test_compose_caption_skips_tags_already_in_the_text_and_repeats():
    caption = bulk.compose_caption("Tacos #Receta", ["receta", "tacos", "TACOS"], "#tacos #mexico")
    assert caption == "Tacos #Receta #tacos #mexico"


def test_compose_caption_edge_cases():
    assert bulk.compose_caption("", ["a"], ["b"]) == "#a #b"
    assert bulk.compose_caption("Solo texto  \r\n", [], None) == "Solo texto"
    assert bulk.compose_caption(None, None, None) == ""
    assert bulk.compose_caption("Línea 1\r\nLínea 2", "x", "") == "Línea 1\nLínea 2\n\n#x"


def test_youtube_title_sources():
    row = {"text": "\n  Primera <línea>\nsegunda", "yt_title": ""}
    assert bulk.youtube_title(row, "text", "mi_video-final_02.mp4") == "Primera línea"
    assert bulk.youtube_title(row, "filename", "mi_video-final_02.mp4") == "mi video final 02"
    assert bulk.youtube_title({"text": ""}, "text", "clip.mov") == "clip"  # no text: file name
    assert bulk.youtube_title({"text": "x", "yt_title": " Mío <b> "}, "filename", "a.mp4") == "Mío b"
    assert len(bulk.youtube_title({"text": "a" * 300}, "text", None)) == 100


def test_clean_filename():
    assert bulk.clean_filename("receta_tacos-al-pastor.mp4") == "receta tacos al pastor"
    assert bulk.clean_filename("C:/x/y.z/Vídeo  2026.final.MOV") == "Vídeo 2026 final"
    assert bulk.clean_filename(None) == ""


# ============================================================================================= caps


def _entries(n: int, network: str, *, every: timedelta, **kw) -> list[CapEntry]:
    start = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
    return [CapEntry(account_key="7", network=network, when=start + every * i, **kw) for i in range(n)]


def test_tiktok_inbox_more_than_five_drafts_in_24h():
    warnings = bulk.caps_warnings(_entries(6, "tiktok", every=timedelta(hours=3), mode="inbox"), MADRID)
    assert [w["code"] for w in warnings] == ["cap_tiktok_inbox"]
    assert warnings[0]["count"] == 6 and warnings[0]["limit"] == 5
    assert "5 borradores" in warnings[0]["message"]
    assert bulk.caps_warnings(_entries(5, "tiktok", every=timedelta(hours=3), mode="inbox"), MADRID) == []


def test_caps_use_a_rolling_window():
    # Six inbox drafts one every 5 h: never more than 5 within 24 h.
    assert bulk.caps_warnings(_entries(6, "tiktok", every=timedelta(hours=5), mode="inbox"), MADRID) == []
    # Seven one every 5 h: still at most 5 in any 24 h window.
    assert bulk.caps_warnings(_entries(7, "tiktok", every=timedelta(hours=5), mode="inbox"), MADRID) == []
    busy = bulk.caps_warnings(_entries(7, "tiktok", every=timedelta(hours=4), mode="inbox"), MADRID)
    assert busy and busy[0]["count"] == 6


def test_instagram_facebook_youtube_caps():
    ig = bulk.caps_warnings(_entries(51, "instagram", every=timedelta(minutes=20)), MADRID)
    assert [w["code"] for w in ig] == ["cap_instagram"]
    assert bulk.caps_warnings(_entries(50, "instagram", every=timedelta(minutes=20)), MADRID) == []
    fb = bulk.caps_warnings(_entries(31, "facebook", every=timedelta(minutes=30), format="reel"), MADRID)
    assert [w["code"] for w in fb] == ["cap_facebook_reels"]
    assert (
        bulk.caps_warnings(_entries(31, "facebook", every=timedelta(minutes=30), format="post"), MADRID) == []
    )
    yt = bulk.caps_warnings(_entries(11, "youtube", every=timedelta(hours=2)), MADRID)
    assert [w["code"] for w in yt] == ["cap_youtube_uploads"]
    tt = bulk.caps_warnings(_entries(16, "tiktok", every=timedelta(hours=1)), MADRID)
    assert [w["code"] for w in tt] == ["cap_tiktok_direct"]


def test_assisted_posts_do_not_count_but_tiktok_inbox_always_does():
    assert bulk.caps_warnings(_entries(60, "instagram", every=timedelta(minutes=5), auto=False), MADRID) == []
    inbox = bulk.caps_warnings(
        _entries(6, "tiktok", every=timedelta(hours=1), mode="inbox", auto=True), MADRID
    )
    assert inbox  # inbox sends itself (BatchAccount.sends_itself)


def test_posts_already_scheduled_count_towards_the_caps():
    # Review: a second batch of 5 TikTok inbox drafts on the day of a first one never warned.
    old = _entries(5, "tiktok", every=timedelta(hours=1), mode="inbox", existing=True)
    new = [
        CapEntry(account_key="7", network="tiktok", when=e.when + timedelta(minutes=30), mode="inbox")
        for e in _entries(5, "tiktok", every=timedelta(hours=1))
    ]
    assert bulk.caps_warnings(new, MADRID) == []
    warnings = bulk.caps_warnings(old + new, MADRID)
    assert [w["code"] for w in warnings] == ["cap_tiktok_inbox"]
    assert warnings[0]["count"] == 10 and warnings[0]["existing"] == 5
    assert "Incluye 5 publicaciones que ya tenías programadas" in warnings[0]["message"]


def test_caps_only_warn_for_windows_with_a_row_of_the_batch():
    # Already over the limit on Wednesday, but this batch only adds posts two days later.
    old = _entries(8, "tiktok", every=timedelta(hours=1), mode="inbox", existing=True)
    late = [
        CapEntry(account_key="7", network="tiktok", when=old[-1].when + timedelta(days=2), mode="inbox"),
    ]
    assert bulk.caps_warnings(old, MADRID) == []
    assert bulk.caps_warnings(old + late, MADRID) == []
    # One more row inside the busy day does warn (the existing ones are counted with it).
    inside = [CapEntry(account_key="7", network="tiktok", when=old[2].when, mode="inbox")]
    warnings = bulk.caps_warnings(old + inside, MADRID)
    assert warnings[0]["count"] == 9 and warnings[0]["existing"] == 8
    one = bulk.caps_warnings(_entries(5, "tiktok", every=timedelta(hours=1), mode="inbox") + old[:1], MADRID)
    assert one[0]["existing"] == 1 and "Incluye 1 publicación que ya tenías programada" in one[0]["message"]


# ========================================================================================== claims


def test_claim_is_stale():
    now = NOW
    fresh = {"status": "running", "boot": bulk.BOOT_ID}
    assert bulk.claim_is_stale(fresh, now - timedelta(minutes=1), now) is False
    assert bulk.claim_is_stale(fresh, now - bulk.STALE_CLAIM, now) is True  # no progress for 10 min
    # Claimed by an earlier process (the app was closed or crashed mid-way): resumable at once.
    assert bulk.claim_is_stale({"status": "running", "boot": "earlier"}, now, now) is True
    assert bulk.claim_is_stale({"status": "running"}, now, now) is True  # claims from before boot ids
    assert bulk.claim_is_stale({"status": "interrupted", "boot": bulk.BOOT_ID}, now, now) is True
    assert bulk.claim_is_stale({"status": "done", "boot": "earlier"}, now, now) is False


# ===================================================================================== batch schema


def test_batch_brand_id_and_ensure_brand():
    class FakeBrand:
        id = 2
        name = "Marca B"

    assert bulk.sanitize_batch(_raw_batch(brand_id=3))["brand_id"] == 3
    assert bulk.sanitize_batch(_raw_batch(brand_id="3"))["brand_id"] == 3
    for bad in (None, "x", True, 0, -1):
        assert bulk.sanitize_batch(_raw_batch(brand_id=bad))["brand_id"] is None
    bulk.ensure_brand({"brand_id": 2}, FakeBrand())
    bulk.ensure_brand({}, FakeBrand())  # pages opened before batches had a brand
    with pytest.raises(BatchError) as exc:
        bulk.ensure_brand({"brand_id": 1}, FakeBrand())
    assert exc.value.code == "brand_changed" and exc.value.status_code == 409
    assert "«Marca B»" in exc.value.message


def _raw_batch(**kw):
    raw = {
        "token": "T" * 24,
        "step": 9,
        "accounts": {
            "3": {
                "selected": True,
                "format": "video",
                "auto_publish": "no",
                "options": {"privacy_level": "SELF_ONLY", "consent": True, "evil": "x", "_priv": 1},
            },
            "bad key": {"selected": True},
        },
        "common_hashtags": "#uno dos",
        "yt_title_source": "nope",
        "distribution": {
            "start_date": "2026-11-04",
            "mode": "x",
            "per_day": 99,
            "slots": ["9", "19h", "bad"],
        },
        "rows": [
            {
                "id": "a1",
                "media_id": "5",
                "text": "Hola\r\n",
                "hashtags": "#x y",
                "date": "2026-11-04",
                "time": "9:05",
            },
            {
                "id": "a1",
                "media_id": 6,
                "date": "nope",
                "time": "99:99",
                "networks": {"3": False, "x y": True},
            },
        ],
    }
    raw.update(kw)
    return raw


def test_sanitize_batch_cleans_types_and_unknown_keys():
    batch = bulk.sanitize_batch(_raw_batch(), networks={"3": "tiktok"})
    assert batch["step"] == 4
    assert list(batch["accounts"]) == ["3"]
    acc = batch["accounts"]["3"]
    assert acc["auto_publish"] is False
    assert acc["options"] == {"privacy_level": "SELF_ONLY"}  # consent never stored; unknown keys dropped
    assert batch["common_hashtags"] == ["uno", "dos"]
    assert batch["yt_title_source"] == "text"
    assert batch["distribution"]["mode"] == "per_day" and batch["distribution"]["per_day"] == bulk.MAX_PER_DAY
    assert batch["distribution"]["slots"] == ["09:00", "19:00"]
    first, second = batch["rows"]
    assert first["media_id"] == 5 and first["text"] == "Hola\n" and first["hashtags"] == ["x", "y"]
    assert first["time"] == "09:05"
    assert second["id"] != "a1"  # duplicated ids are renamed
    assert second["date"] == "" and second["time"] == ""
    assert second["networks"] == {"3": False}


def test_sanitize_batch_rejects_bad_input():
    with pytest.raises(BatchError):
        bulk.sanitize_batch(_raw_batch(token="short"))
    with pytest.raises(BatchError):
        bulk.sanitize_batch("nope")
    with pytest.raises(BatchError, match="como máximo"):
        bulk.sanitize_batch(_raw_batch(rows=[{"media_id": i} for i in range(bulk.MAX_ROWS + 1)]))
    with pytest.raises(BatchError):
        bulk.sanitize_batch(_raw_batch(rows=[{"media_id": "x"}]))
    with pytest.raises(BatchError, match="demasiado largo"):
        bulk.sanitize_batch(_raw_batch(rows=[{"media_id": 1, "text": "x" * (bulk.MAX_TEXT + 1)}]))


def test_sanitize_batch_fixes_an_unknown_format():
    for fmt in ("photo", "carrusel"):  # not YouTube formats → the first enabled one
        batch = bulk.sanitize_batch(
            _raw_batch(accounts={"4": {"selected": True, "format": fmt, "options": {}}}),
            networks={"4": "youtube"},
        )
        assert batch["accounts"]["4"]["format"] == "video"
