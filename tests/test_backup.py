import asyncio
import json
import os
import time

import pytest
from aioanylist.proto import PB
from google.protobuf.json_format import ParseDict

from groceries.backup import BackupError, FileTokens, backup, push_metrics

COLLECTIONS = {"collection_ids": [], "collections": [], "all_recipes": None}
LONG_AGO = time.time() - 400 * 86400


def recipe(rid="r1", name="Chicken au Poivre", photos=(), ingredient="whole black peppercorns"):
    r = PB.PBRecipe(identifier=rid, name=name, scaleFactor=1.5)
    r.ingredients.add(identifier="i1", name=ingredient, quantity="1 tablespoon")
    r.photoIds.extend(photos)
    return r


class Photos:
    """Photo fetcher stand-in that records what was downloaded."""

    def __init__(self, fail=False):
        self.fetched: list[str] = []
        self.fail = fail

    async def __call__(self, photo_id: str) -> bytes:
        if self.fail:
            raise OSError("photo host unreachable")
        self.fetched.append(photo_id)
        return f"jpeg bytes of {photo_id}".encode()


def run(root, recipes, photos=None, prune_before=0.0):
    return asyncio.run(
        backup(root, recipes, COLLECTIONS, photos or Photos(), prune_before=prune_before)
    )


def files(root, pattern):
    return sorted(root.glob(pattern))


def age(path):
    os.utime(path, (LONG_AGO, LONG_AGO))


def test_first_run_saves_recipe_collections_and_photos(tmp_path):
    photos = Photos()
    report = run(tmp_path, [recipe(photos=["p1"])], photos)

    [saved] = files(tmp_path, "recipes/r1/*.json")
    parsed = ParseDict(json.loads(saved.read_bytes()), PB.PBRecipe())
    assert parsed == recipe(photos=["p1"])  # restorable as it was
    assert len(files(tmp_path, "collections/*.json")) == 1
    assert len(files(tmp_path, "photos/*.jpg")) == 1
    assert photos.fetched == ["p1"]
    assert (report.recipes, report.photos, len(report.written)) == (1, 1, 3)
    assert not report.hash_failures


def test_unchanged_run_writes_nothing_and_downloads_nothing(tmp_path):
    run(tmp_path, [recipe(photos=["p1"])])
    photos = Photos()
    report = run(tmp_path, [recipe(photos=["p1"])], photos)
    assert not report.written
    assert photos.fetched == []


def test_changed_recipe_adds_a_version(tmp_path):
    run(tmp_path, [recipe()])
    report = run(tmp_path, [recipe(ingredient="Black Peppercorns")])
    assert len(files(tmp_path, "recipes/r1/*.json")) == 2
    assert len(report.written) == 1


def test_corrupt_current_file_is_rewritten_and_reported(tmp_path):
    run(tmp_path, [recipe()])
    [saved] = files(tmp_path, "recipes/r1/*.json")
    saved.write_bytes(b"bit rot")
    report = run(tmp_path, [recipe()])
    assert report.hash_failures == {saved}
    assert json.loads(saved.read_bytes())["name"] == "Chicken au Poivre"


def test_corrupt_old_version_is_reported_but_kept(tmp_path):
    run(tmp_path, [recipe()])
    [old] = files(tmp_path, "recipes/r1/*.json")
    run(tmp_path, [recipe(ingredient="Black Peppercorns")])
    old.write_bytes(b"bit rot")
    report = run(tmp_path, [recipe(ingredient="Black Peppercorns")])
    assert report.hash_failures == {old}
    assert old.exists()


def test_missing_photo_is_downloaded_again(tmp_path):
    run(tmp_path, [recipe(photos=["p1"])])
    [photo] = files(tmp_path, "photos/*.jpg")
    photo.unlink()
    photos = Photos()
    report = run(tmp_path, [recipe(photos=["p1"])], photos)
    assert photos.fetched == ["p1"]
    assert photo.exists()
    assert report.hash_failures == {photo}


def test_prune_drops_old_versions_but_keeps_each_recipes_newest(tmp_path):
    run(tmp_path, [recipe(), recipe("gone", name="Deleted recipe")])
    for path in files(tmp_path, "recipes/*/*.json"):
        age(path)
    report = run(tmp_path, [recipe(ingredient="Black Peppercorns")], prune_before=time.time() - 1)

    [current] = files(tmp_path, "recipes/r1/*.json")
    assert json.loads(current.read_bytes())["ingredients"][0]["name"] == "Black Peppercorns"
    assert len(files(tmp_path, "recipes/gone/*.json")) == 1  # deleted in AnyList, kept
    assert report.pruned == 1


def test_prune_keeps_versions_newer_than_the_cutoff(tmp_path):
    run(tmp_path, [recipe()])
    run(tmp_path, [recipe(ingredient="Black Peppercorns")], prune_before=LONG_AGO)
    assert len(files(tmp_path, "recipes/r1/*.json")) == 2


def test_prune_drops_photos_no_remaining_version_uses(tmp_path):
    run(tmp_path, [recipe(photos=["p1"])])
    for path in files(tmp_path, "recipes/r1/*.json"):
        age(path)
    run(tmp_path, [recipe(photos=["p2"])], prune_before=time.time() - 1)
    index = json.loads((tmp_path / "photos/index.json").read_text())
    assert list(index) == ["p2"]
    assert [p.name for p in files(tmp_path, "photos/*.jpg")] == [f"{index['p2']}.jpg"]


def test_shared_photo_content_is_stored_once(tmp_path):
    async def same(_photo_id):
        return b"one image"

    asyncio.run(backup(tmp_path, [recipe(photos=["p1", "p2"])], COLLECTIONS, same, prune_before=0))
    assert len(files(tmp_path, "photos/*.jpg")) == 1


def test_no_recipes_is_a_failure(tmp_path):
    with pytest.raises(BackupError):
        run(tmp_path, [])


def test_failed_photo_download_fails_the_run(tmp_path):
    with pytest.raises(OSError):
        run(tmp_path, [recipe(photos=["p1"])], Photos(fail=True))


def test_unsafe_recipe_id_is_refused(tmp_path):
    with pytest.raises(BackupError):
        run(tmp_path, [recipe("../escape")])


def test_tokens_file_is_private(tmp_path):
    tokens = FileTokens(tmp_path / "tokens.json")
    assert tokens.load() is None
    tokens.save('{"access_token": "x"}')
    assert tokens.load() == '{"access_token": "x"}'
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600
    tokens.save(None)
    assert tokens.load() is None


def test_metrics_are_pushed_with_the_success_timestamp(tmp_path, monkeypatch):
    pushed = {}

    def fake_push(url, job, registry):
        pushed.update(
            url=url,
            job=job,
            samples={s.name: s.value for m in registry.collect() for s in m.samples},
        )

    monkeypatch.setattr("groceries.backup.push_to_gateway", fake_push)
    report = run(tmp_path, [recipe(photos=["p1"])])
    push_metrics("http://pushgateway:9091", report)

    samples = pushed["samples"]
    assert pushed["job"] == "groceries_recipe_backup"
    assert samples["groceries_recipe_backup_recipes"] == 1
    assert samples["groceries_recipe_backup_hash_failures"] == 0
    assert abs(samples["groceries_recipe_backup_last_success_timestamp_seconds"] - time.time()) < 60
