"""Back up every AnyList recipe, with its photos, to a content-addressed directory.

A CronJob runs `python -m groceries.backup` every 30 minutes, on a volume of its own that
VolSync backs up. Layout under BACKUP_DIR (default /backup):

    recipes/<recipe id>/<sha256>.json   one file per distinct version of a recipe
    collections/<sha256>.json           recipe collections and their order
    photos/<sha256>.jpg                 recipe photos
    photos/index.json                   photo id -> sha256
    tokens.json                         cached AnyList sign-in

Each file is named by the sha256 of its bytes, so checking whether a version is saved is
a file lookup. A recipe file is its full PBRecipe as canonical JSON, so it can be parsed
back and saved as it was. Every run re-hashes each file it touches and rewrites one that
doesn't match, then re-hashes every other file. Any mismatch counts as a hash failure.

A file's mtime is the last time it was current. Versions that haven't been current for
PRUNE_DAYS are deleted, except each recipe's newest file, so a recipe deleted in AnyList
keeps its last version. Photos that no remaining recipe file uses are deleted too.

The last step, after everything else succeeded, pushes the metrics to the Pushgateway at
PUSHGATEWAY_URL. An alert fires when groceries_recipe_backup_last_success_timestamp_seconds
is more than a day old. Restore is not implemented; the README has the planned procedure.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from google.protobuf.json_format import MessageToDict
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

from . import anylist

log = logging.getLogger(__name__)

FetchPhoto = Callable[[str], Awaitable[bytes]]
JOB = "groceries_recipe_backup"
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")  # recipe ids become directory names


class BackupError(Exception):
    pass


@dataclass
class Report:
    recipes: int = 0
    photos: int = 0
    written: set[Path] = field(default_factory=set)
    hash_failures: set[Path] = field(default_factory=set)
    pruned: int = 0


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _message(message) -> dict:
    return MessageToDict(message, preserving_proto_field_name=True)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _matches(path: Path) -> bool:
    """Whether a content-addressed file's bytes still hash to its name."""
    return path.exists() and _sha256(path.read_bytes()) == path.name.split(".")[0]


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class FileTokens:
    """AnyList tokens in a file only this job can read."""

    def __init__(self, path: Path):
        self._path = path

    def load(self) -> str | None:
        return self._path.read_text() if self._path.exists() else None

    def save(self, value: str | None) -> None:
        if value is None:
            self._path.unlink(missing_ok=True)
        else:
            _atomic_write(self._path, value.encode(), mode=0o600)


class _Store:
    def __init__(self, root: Path, report: Report):
        self.root = root
        self.report = report

    def put(self, directory: Path, data: bytes, suffix: str) -> str:
        """Save `data` as <sha256><suffix>, and mark it current. Returns the digest."""
        digest = _sha256(data)
        path = directory / f"{digest}{suffix}"
        if _matches(path):
            os.utime(path)
            return digest
        if path.exists():
            log.warning("%s failed its hash check, rewriting it", path)
            self.report.hash_failures.add(path)
        _atomic_write(path, data)
        if not _matches(path):
            raise BackupError(f"{path} doesn't match its hash after writing")
        self.report.written.add(path)
        return digest


async def backup(
    root: Path,
    recipes: Iterable,
    collections: dict,
    fetch_photo: FetchPhoto,
    *,
    prune_before: float,
) -> Report:
    """Save the recipes, collections and photos given, then prune and verify everything."""
    report = Report()
    store = _Store(root, report)
    recipes = list(recipes)
    if not recipes:
        raise BackupError("AnyList returned no recipes; refusing to record a success")

    for recipe in recipes:
        if not _SAFE_ID.fullmatch(recipe.identifier):
            raise BackupError(f"unexpected recipe id {recipe.identifier!r}")
        store.put(root / "recipes" / recipe.identifier, canonical_json(_message(recipe)), ".json")
    store.put(root / "collections", canonical_json(collections), ".json")
    report.recipes = len(recipes)

    photos = root / "photos"
    index_path = photos / "index.json"
    index: dict[str, str] = json.loads(index_path.read_text()) if index_path.exists() else {}
    for photo_id in sorted({p for r in recipes for p in r.photoIds}):
        known = index.get(photo_id)
        if known is not None:
            path = photos / f"{known}.jpg"
            if _matches(path):
                os.utime(path)
                continue
            log.warning("photo %s (%s) is missing or corrupt, downloading it again", photo_id, path)
            report.hash_failures.add(path)
        index[photo_id] = store.put(photos, await fetch_photo(photo_id), ".jpg")
    report.photos = len({p for r in recipes for p in r.photoIds})

    report.pruned = _prune(root, index, prune_before)
    _atomic_write(index_path, canonical_json(index))
    _verify_all(root, report)
    return report


def _prune(root: Path, index: dict[str, str], before: float) -> int:
    """Delete versions last current before `before`, keeping each set's newest file, then
    photos no remaining recipe file uses. Drops pruned photos from `index`."""
    pruned = 0
    for tmp in root.rglob(".*.tmp"):
        tmp.unlink()
    recipes = root / "recipes"
    sets = [d for d in recipes.iterdir() if d.is_dir()] if recipes.exists() else []
    sets.append(root / "collections")
    for directory in sets:
        files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-1]:
            if old.stat().st_mtime < before:
                old.unlink()
                pruned += 1

    used: set[str] = set()
    for path in recipes.glob("*/*.json"):
        try:
            used.update(json.loads(path.read_bytes()).get("photoIds", []))
        except ValueError:
            log.error("%s isn't valid JSON; keeping its photos would need its ids", path)
    keep = {index[p] for p in used if p in index}
    for photo_id in [p for p in index if p not in used]:
        del index[photo_id]
    for path in (root / "photos").glob("*.jpg"):
        if path.name.split(".")[0] not in keep:
            path.unlink()
            pruned += 1
    return pruned


def _verify_all(root: Path, report: Report) -> None:
    """Re-hash every stored file. A bad one can't be rebuilt, so it's only reported."""
    for pattern in ("recipes/*/*.json", "collections/*.json", "photos/*.jpg"):
        for path in root.glob(pattern):
            if not _matches(path):
                log.error("%s doesn't match its hash", path)
                report.hash_failures.add(path)


def push_metrics(url: str, report: Report) -> None:
    registry = CollectorRegistry()

    def gauge(name: str, doc: str) -> Gauge:
        return Gauge(f"{JOB}_{name}", doc, registry=registry)

    gauge("recipes", "Recipes backed up in the last successful run").set(report.recipes)
    gauge("photos", "Recipe photos backed up in the last successful run").set(report.photos)
    gauge("files_written", "Files written in the last successful run").set(len(report.written))
    gauge("pruned", "Files pruned in the last successful run").set(report.pruned)
    gauge(
        "hash_failures", "Stored files that didn't match their hash in the last successful run"
    ).set(len(report.hash_failures))
    gauge("last_success_timestamp_seconds", "When a backup last succeeded").set_to_current_time()
    push_to_gateway(url, job=JOB, registry=registry)


async def _main(env: Mapping[str, str]) -> Report:
    root = Path(env.get("BACKUP_DIR", "/backup"))
    root.mkdir(parents=True, exist_ok=True)
    prune_before = time.time() - float(env.get("PRUNE_DAYS", "90")) * 86400
    client = await anylist.connect(
        FileTokens(root / "tokens.json"), env["ANYLIST_EMAIL"], env["ANYLIST_PASSWORD"]
    )
    try:
        state = client.state
        collections = {
            "collection_ids": list(state.recipe_collection_ids),
            "collections": sorted(
                (_message(c) for c in state.recipe_collections.values()),
                key=lambda c: c.get("identifier", ""),
            ),
            "all_recipes": _message(state.all_recipes_collection)
            if state.all_recipes_collection is not None
            else None,
        }
        async with aiohttp.ClientSession(raise_for_status=True) as http:

            async def fetch_photo(photo_id: str) -> bytes:
                async with http.get(client.photos.url(photo_id)) as resp:
                    return await resp.read()

            return await backup(
                root,
                state.recipes.values(),
                collections,
                fetch_photo,
                prune_before=prune_before,
            )
    finally:
        await client.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    report = asyncio.run(_main(os.environ))
    log.info(
        "backed up %d recipes and %d photos: %d files written, %d pruned, %d hash failures",
        report.recipes,
        report.photos,
        len(report.written),
        report.pruned,
        len(report.hash_failures),
    )
    url = os.environ.get("PUSHGATEWAY_URL")
    if url:
        push_metrics(url, report)
    else:
        log.warning("PUSHGATEWAY_URL is unset, so this success isn't reported")


if __name__ == "__main__":
    main()
