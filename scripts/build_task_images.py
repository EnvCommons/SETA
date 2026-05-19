#!/usr/bin/env python3
"""Build and push per-task SETA Dockerfiles to Docker Hub.

Each task at Dataset/<task_id>/ has its own Dockerfile. This script builds
that Dockerfile into an image tagged `generalreasoning/eigent-seta:<task_id>`,
pushes it, and writes the resulting digest plus the resolved FROM-image digest
to Dataset/<task_id>/image_sha.txt so the SETA env can pin SandboxSettings.image
to a stable per-task digest at runtime.

Cache hit logic (so re-runs don't rebuild unchanged tasks):

  Cached image_sha.txt contains task_image_digest, base_image_digest,
  dockerfile_sha256. Skip rebuild iff:
    - sha256(Dockerfile) matches dockerfile_sha256, AND
    - current digest of the FROM-line image matches base_image_digest.

  --check-base skips the Dockerfile-sha check and only re-checks base drift.
  Run this periodically to catch upstream FROM-image updates.

  --force ignores the cache entirely.

Modeled on env-endless-terminals/scripts/build_images.py.
"""

import os

# Silence noisy gRPC startup logging before any google-cloud imports.
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GLOG_minloglevel", "2")

import argparse
import asyncio
import hashlib
import io
import json
import subprocess
import tarfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from google.cloud import storage
from google.cloud.devtools import cloudbuild_v1


REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE_PREFIX = "generalreasoning/eigent-seta"

# Source of truth for task data: the OpenReward bucket where the env runtime
# mounts /orwd_data from. Path layout is:
#   gs://<SETA_SOURCE_BUCKET>/<SETA_SOURCE_PREFIX>/<task_id>/{Dockerfile,tests/,...}
#
# Concrete values are operator-specific (depend on which OR workspace this env
# lives in) and are loaded from environment variables — see ./scripts/run.md
# for the actual values to export before invoking this script.
SOURCE_BUCKET = os.environ.get("SETA_SOURCE_BUCKET", "")
SOURCE_PREFIX = os.environ.get("SETA_SOURCE_PREFIX", "")

# Where the env-server (and this build script) reads/writes the digest map.
TASK_IMAGES_PATH = REPO_ROOT / "task_images.json"
TASK_INDEX_PATH = REPO_ROOT / "task_index.json"

# Transient local cache for task data fetched from the source bucket.
# Gitignored; lets repeated runs avoid re-downloading.
LOCAL_CACHE = REPO_ROOT / ".cache" / "seta-tasks"

# Files inside a task dir that should NOT be uploaded to the build context.
# Even though current Dockerfiles don't `COPY` them, exclude defensively so
# an accidental `COPY . /app` in a future Dockerfile can't bake the
# reference solution into the image.
CONTEXT_EXCLUDES = {"solution.sh", "task.yaml", "draft_spec.md"}


class RateLimiter:
    """Token-bucket rate limiter for async operations."""

    def __init__(self, max_per_minute: float, headroom: float = 0.9):
        self.max_per_minute = max_per_minute * headroom
        self.window = 60.0
        self.timestamps: "deque[float]" = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self.timestamps and now - self.timestamps[0] > self.window:
                self.timestamps.popleft()
            if len(self.timestamps) >= self.max_per_minute:
                sleep_time = self.window - (now - self.timestamps[0]) + 0.1
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                    now = time.monotonic()
                    while self.timestamps and now - self.timestamps[0] > self.window:
                        self.timestamps.popleft()
            self.timestamps.append(now)


# --- discovery -----------------------------------------------------------


def discover_tasks(filter_substring: Optional[str] = None) -> list[str]:
    """Return task ids from task_index.json, sorted numerically when possible.

    task_index.json is the authoritative list (1376 tasks). Per-task data is
    fetched from the source bucket on demand.
    """
    with open(TASK_INDEX_PATH) as f:
        index = json.load(f)
    task_ids = list(index.keys())
    if filter_substring is not None:
        task_ids = [t for t in task_ids if filter_substring in t]

    def sort_key(name: str) -> tuple[int, str]:
        try:
            return (int(name), "")
        except ValueError:
            return (10**9, name)

    task_ids.sort(key=sort_key)
    return task_ids


def image_name(task_id: str) -> str:
    return f"{IMAGE_PREFIX}:{task_id}"


# --- local cache (downloaded task data) ----------------------------------


_cache_locks: dict[str, threading.Lock] = {}
_cache_locks_guard = threading.Lock()


def _lock_for(task_id: str) -> threading.Lock:
    """Per-task lock so concurrent workers don't fight over the same dir."""
    with _cache_locks_guard:
        lock = _cache_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _cache_locks[task_id] = lock
        return lock


def ensure_local_task_data(task_id: str, storage_client: storage.Client) -> Path:
    """Download `Dataset/<task_id>/*` from the source bucket into LOCAL_CACHE
    if not already present, and return the local path. Idempotent + thread-safe."""
    local_dir = LOCAL_CACHE / task_id
    sentinel = local_dir / ".complete"
    if sentinel.exists():
        return local_dir

    with _lock_for(task_id):
        if sentinel.exists():  # double-check after lock
            return local_dir

        src_bucket = storage_client.bucket(SOURCE_BUCKET)
        prefix = f"{SOURCE_PREFIX}/{task_id}/"
        blobs = list(src_bucket.list_blobs(prefix=prefix))
        if not blobs:
            raise FileNotFoundError(
                f"No data for task {task_id} at gs://{SOURCE_BUCKET}/{prefix}"
            )
        local_dir.mkdir(parents=True, exist_ok=True)
        for blob in blobs:
            rel = blob.name[len(prefix):]
            if not rel or rel.endswith("/"):
                continue  # directory marker
            dst = local_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(dst))
        sentinel.write_text("ok\n")
        return local_dir


# --- task_images.json (the central digest map) ---------------------------


_images_map_lock = asyncio.Lock()


def read_images_map() -> dict[str, dict[str, str]]:
    """Load task_images.json; return {} if absent."""
    if not TASK_IMAGES_PATH.exists():
        return {}
    with open(TASK_IMAGES_PATH) as f:
        return json.load(f)


def write_images_map_atomic(data: dict[str, dict[str, str]]) -> None:
    """Atomic write so partial writes don't corrupt the file on Ctrl-C."""
    tmp = TASK_IMAGES_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(TASK_IMAGES_PATH)


# --- Dockerfile parsing --------------------------------------------------


def dockerfile_sha_from_path(dockerfile_path: Path) -> str:
    return hashlib.sha256(dockerfile_path.read_bytes()).hexdigest()


def parse_from_line(dockerfile_path: Path) -> Optional[str]:
    """Return the first FROM reference in the Dockerfile, or None."""
    for raw in dockerfile_path.read_text(errors="replace").splitlines():
        stripped = raw.strip()
        if stripped.lower().startswith("from "):
            ref = stripped.split(None, 1)[1].strip()
            if " AS " in ref:
                ref = ref.split(" AS ")[0].strip()
            elif " as " in ref:
                ref = ref.split(" as ")[0].strip()
            return ref
    return None


def resolve_remote_digest(image_ref: str) -> Optional[str]:
    """Resolve the manifest digest of `image_ref` from its remote registry.

    Uses `docker manifest inspect --verbose`. The output is either a dict
    (single-arch image) or a list of dicts (multi-arch / OCI index). In
    both shapes, the manifest digest lives at `Descriptor.digest` of each
    entry.

    Returns the canonical "sha256:..." string, or None if the image
    doesn't exist or the call fails.
    """
    try:
        result = subprocess.run(
            ["docker", "manifest", "inspect", "--verbose", image_ref],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    entries = data if isinstance(data, list) else [data]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        descriptor = entry.get("Descriptor") or {}
        digest = descriptor.get("digest")
        if digest:
            return digest

    return None


# --- cache hit decision --------------------------------------------------


def prefetch_base_digests(
    task_ids: list[str],
    storage_client: storage.Client,
    *,
    parallelism: int = 8,
) -> dict[str, str]:
    """Resolve the digest of each unique FROM image used across the task set.

    Downloads each task's data first (idempotent via LOCAL_CACHE), parses
    FROM lines, then resolves unique refs once each.
    """
    print(f"Hydrating local cache for {len(task_ids)} task(s)...")
    refs: set[str] = set()
    for i, task_id in enumerate(task_ids, 1):
        if i % 100 == 0:
            print(f"  cached {i}/{len(task_ids)} ...", flush=True)
        try:
            task_dir = ensure_local_task_data(task_id, storage_client)
        except FileNotFoundError as e:
            print(f"  [WARN] {e}")
            continue
        ref = parse_from_line(task_dir / "Dockerfile")
        if ref:
            refs.add(ref)

    if not refs:
        return {}

    print(f"Resolving {len(refs)} unique base image digest(s)...")
    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(parallelism, len(refs))) as ex:
        future_to_ref = {ex.submit(resolve_remote_digest, ref): ref for ref in refs}
        for fut in future_to_ref:
            ref = future_to_ref[fut]
            digest = fut.result() or ""
            results[ref] = digest
            display = (digest[:23] + "...") if digest else "(unresolved)"
            print(f"  {ref} -> {display}")
    return results


def needs_rebuild(
    task_id: str,
    task_dir: Path,
    *,
    force: bool,
    check_base_only: bool,
    base_digest_cache: dict[str, str],
    images_map: dict[str, dict[str, str]],
) -> tuple[bool, str, str]:
    """Decide whether this task needs rebuilding.

    Returns (rebuild_needed, current_dockerfile_sha, current_base_digest).
    current_base_digest may be "" if resolution failed.
    """
    dockerfile_path = task_dir / "Dockerfile"
    cur_df_sha = dockerfile_sha_from_path(dockerfile_path)
    from_ref = parse_from_line(dockerfile_path)
    cur_base_digest = base_digest_cache.get(from_ref, "") if from_ref else ""

    if force:
        return True, cur_df_sha, cur_base_digest

    cache = images_map.get(task_id, {})
    if not cache:
        return True, cur_df_sha, cur_base_digest

    cached_df = cache.get("dockerfile_sha256", "")
    cached_base = cache.get("base_image_digest", "")

    if check_base_only:
        if cur_base_digest and cached_base and cur_base_digest != cached_base:
            return True, cur_df_sha, cur_base_digest
        return False, cur_df_sha, cur_base_digest

    if cur_df_sha != cached_df:
        return True, cur_df_sha, cur_base_digest
    if cur_base_digest and cached_base and cur_base_digest != cached_base:
        return True, cur_df_sha, cur_base_digest
    return False, cur_df_sha, cur_base_digest


async def record_built_image(
    task_id: str,
    task_image_digest: str,
    base_image_digest: str,
    dockerfile_sha_hex: str,
    images_map: dict[str, dict[str, str]],
) -> None:
    """Update task_images.json with the freshly-built digest. Serialized so
    concurrent builders don't race on the JSON file."""
    async with _images_map_lock:
        images_map[task_id] = {
            "task_image_digest": task_image_digest,
            "base_image_digest": base_image_digest,
            "dockerfile_sha256": dockerfile_sha_hex,
        }
        # Re-load fresh from disk in case the user manually edited, then
        # merge, then atomically write back. Keeps concurrent operators safe.
        on_disk = read_images_map()
        on_disk.update(images_map)
        write_images_map_atomic(on_disk)


# --- local builds --------------------------------------------------------


def build_local(
    task_id: str,
    storage_client: storage.Client,
    *,
    force: bool,
    check_base_only: bool,
    base_digest_cache: dict[str, str],
    images_map: dict[str, dict[str, str]],
) -> bool:
    try:
        task_dir = ensure_local_task_data(task_id, storage_client)
    except FileNotFoundError as e:
        print(f"[FAIL] {task_id}: {e}")
        return False

    rebuild, df_sha, base_digest = needs_rebuild(
        task_id, task_dir,
        force=force, check_base_only=check_base_only,
        base_digest_cache=base_digest_cache, images_map=images_map,
    )
    if not rebuild:
        cached = images_map.get(task_id, {}).get("task_image_digest", "cached")
        print(f"[SKIP] {task_id} ({cached[:23]}...)")
        return True

    img = image_name(task_id)
    build = subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-t", img, "."],
        cwd=task_dir, capture_output=True, text=True,
    )
    if build.returncode != 0:
        print(f"[FAIL] {task_id}: build error\n{build.stderr[-2000:]}")
        return False

    push = subprocess.run(["docker", "push", img], capture_output=True, text=True)
    if push.returncode != 0:
        print(f"[FAIL] {task_id}: push error\n{push.stderr[-2000:]}")
        return False

    inspect = subprocess.run(
        ["docker", "inspect", "--format={{index .RepoDigests 0}}", img],
        capture_output=True, text=True,
    )
    task_digest = "unknown"
    if inspect.returncode == 0 and "@" in inspect.stdout:
        task_digest = inspect.stdout.strip().split("@", 1)[1]

    # synchronous record (no asyncio.Lock needed for local sequential mode)
    images_map[task_id] = {
        "task_image_digest": task_digest,
        "base_image_digest": base_digest,
        "dockerfile_sha256": df_sha,
    }
    on_disk = read_images_map()
    on_disk.update(images_map)
    write_images_map_atomic(on_disk)
    print(f"[OK] {task_id} ({task_digest[:23]}...)")
    return True


def run_rescue_cache(
    task_ids: list[str],
    storage_client: storage.Client,
    *,
    base_digest_cache: dict[str, str],
    images_map: dict[str, dict[str, str]],
    parallelism: int = 16,
) -> None:
    """For each task missing from task_images.json, resolve its registry
    digest (if pushed) and add an entry. No Cloud Build calls.

    Use when an earlier bulk build pushed images successfully but failed to
    capture digests — saves re-running the full build pipeline.
    """
    pending = [t for t in task_ids if t not in images_map]
    if not pending:
        print("All tasks already in task_images.json — nothing to rescue.")
        return

    print(f"Rescuing digests for {len(pending)} task(s) missing from task_images.json...")

    def rescue_one(task_id: str) -> tuple[str, str, str, str]:
        ref = image_name(task_id)
        digest = resolve_remote_digest(ref) or ""
        if not digest:
            return task_id, "no-image", "", ""
        try:
            task_dir = ensure_local_task_data(task_id, storage_client)
            df_sha = dockerfile_sha_from_path(task_dir / "Dockerfile")
            from_ref = parse_from_line(task_dir / "Dockerfile") or ""
            base_digest = base_digest_cache.get(from_ref, "")
        except FileNotFoundError:
            df_sha = base_digest = ""
        return task_id, digest, base_digest, df_sha

    recovered = missing = 0
    total = len(pending)
    with ThreadPoolExecutor(max_workers=parallelism) as ex:
        future_to_task = {ex.submit(rescue_one, t): t for t in pending}
        for i, fut in enumerate(future_to_task, 1):
            task_id, result, base_digest, df_sha = fut.result()
            if result == "no-image":
                print(f"[MISS {i}/{total}] {task_id} (no image in registry)", flush=True)
                missing += 1
            else:
                images_map[task_id] = {
                    "task_image_digest": result,
                    "base_image_digest": base_digest,
                    "dockerfile_sha256": df_sha,
                }
                print(f"[RESCUED {i}/{total}] {task_id} ({result[:23]}...)", flush=True)
                recovered += 1

    # Single atomic write at the end.
    on_disk = read_images_map()
    on_disk.update(images_map)
    write_images_map_atomic(on_disk)
    print(f"\nRescue complete: {recovered} cached, {missing} still missing (will rebuild on next run)")


def run_local(
    task_ids: list[str],
    storage_client: storage.Client,
    *,
    force: bool,
    check_base_only: bool,
    base_digest_cache: dict[str, str],
    images_map: dict[str, dict[str, str]],
) -> None:
    succeeded = failed = 0
    for task_id in task_ids:
        ok = build_local(
            task_id, storage_client,
            force=force, check_base_only=check_base_only,
            base_digest_cache=base_digest_cache, images_map=images_map,
        )
        if ok:
            succeeded += 1
        else:
            failed += 1
    print(f"\nLocal builds complete: {succeeded} succeeded, {failed} failed")


# --- Cloud Build ---------------------------------------------------------


SYNTAX_DIRECTIVE = b"# syntax=docker/dockerfile:1\n"


def _ensure_syntax_directive(dockerfile_bytes: bytes) -> bytes:
    """Prepend `# syntax=docker/dockerfile:1` if not already present.

    BuildKit only enables modern Dockerfile features (heredocs, etc.) when
    a syntax directive is set. Many task Dockerfiles use heredoc `RUN
    cat > file << EOF` syntax without the directive, which fails on Cloud
    Build's older default frontend. Adding the directive transparently in
    the build context avoids modifying the repo.
    """
    # If a syntax directive already exists in the first few lines, leave alone.
    head = dockerfile_bytes[:512].lower()
    if b"# syntax=" in head:
        return dockerfile_bytes
    return SYNTAX_DIRECTIVE + dockerfile_bytes


def upload_context_sync(
    task_id: str,
    bucket_name: str,
    storage_client: storage.Client,
) -> str:
    """Build the per-task context tarball from LOCAL_CACHE (hydrated from
    the source bucket) and upload to the Cloud Build staging bucket."""
    task_dir = ensure_local_task_data(task_id, storage_client)
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
        for f in sorted(task_dir.iterdir()):
            if f.name in CONTEXT_EXCLUDES:
                continue
            if f.name == ".complete":
                continue
            if f.name == "Dockerfile":
                # Inject the syntax directive into the in-flight Dockerfile
                # without touching the cached file.
                patched = _ensure_syntax_directive(f.read_bytes())
                info = tarfile.TarInfo(name=f.name)
                info.size = len(patched)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(patched))
            else:
                tar.add(f, arcname=f.name)
    tar_buffer.seek(0)
    object_name = f"cloudbuild-contexts/eigent-seta/{task_id}.tar.gz"
    bucket = storage_client.bucket(bucket_name)
    bucket.blob(object_name).upload_from_file(tar_buffer)
    return object_name


def fetch_dockerhub_digest(task_id: str, *, retries: int = 5, delay: float = 2.0) -> str:
    """Resolve the just-pushed image's manifest digest via the OCI registry.

    Uses `docker manifest inspect` (same code path as base-digest resolution)
    rather than Docker Hub's flaky hub.docker.com web API. Retries a few
    times because the registry is sometimes briefly inconsistent immediately
    after a push.
    """
    ref = image_name(task_id)
    for attempt in range(retries):
        digest = resolve_remote_digest(ref)
        if digest:
            return digest
        if attempt < retries - 1:
            time.sleep(delay)
    return ""


async def build_remote(
    task_id: str,
    *,
    project_id: str,
    gcs_bucket: str,
    build_client: cloudbuild_v1.CloudBuildAsyncClient,
    storage_client: storage.Client,
    executor: ThreadPoolExecutor,
    rate_limiter: Optional[RateLimiter],
    force: bool,
    check_base_only: bool,
    base_digest_cache: dict[str, str],
    images_map: dict[str, dict[str, str]],
    on_submitted: Optional[Callable[[str], None]] = None,
) -> tuple[str, str, str]:
    loop = asyncio.get_event_loop()
    try:
        task_dir = await loop.run_in_executor(
            executor, ensure_local_task_data, task_id, storage_client,
        )
    except FileNotFoundError as e:
        return task_id, "fail", str(e)

    rebuild, df_sha, base_digest = needs_rebuild(
        task_id, task_dir,
        force=force, check_base_only=check_base_only,
        base_digest_cache=base_digest_cache, images_map=images_map,
    )
    if not rebuild:
        cached = images_map.get(task_id, {}).get("task_image_digest", "cached")
        return task_id, "skip", cached

    img = image_name(task_id)

    try:
        gcs_path = await loop.run_in_executor(
            executor, upload_context_sync, task_id, gcs_bucket, storage_client,
        )

        build = cloudbuild_v1.Build(
            source=cloudbuild_v1.Source(
                storage_source=cloudbuild_v1.StorageSource(
                    bucket=gcs_bucket,
                    object=gcs_path,
                )
            ),
            steps=[
                # Login to Docker Hub before buildx tries to push.
                cloudbuild_v1.BuildStep(
                    name="gcr.io/cloud-builders/docker",
                    entrypoint="bash",
                    args=[
                        "-c",
                        "echo $$DOCKERHUB_PASS | docker login -u $$DOCKERHUB_USER --password-stdin",
                    ],
                    secret_env=["DOCKERHUB_USER", "DOCKERHUB_PASS"],
                ),
                # Build + push as a proper OCI artifact set (image manifest +
                # manifest list, both addressable by digest in the registry).
                # `docker build` + `docker push` does NOT produce this format;
                # AR's pull-through cache requires it to proxy by digest.
                cloudbuild_v1.BuildStep(
                    name="gcr.io/cloud-builders/docker",
                    entrypoint="bash",
                    args=[
                        "-c",
                        # `docker buildx create --use` sets up a builder once
                        # per build step. `--provenance=false` keeps the
                        # manifest list to actual platform entries (no
                        # attestation entries that confuse some registries).
                        f"docker buildx create --use --name seta-builder >/dev/null 2>&1 || true && "
                        f"docker buildx build --platform linux/amd64 --provenance=false --push -t {img} .",
                    ],
                    env=["DOCKER_BUILDKIT=1"],
                ),
            ],
            available_secrets=cloudbuild_v1.Secrets(
                secret_manager=[
                    cloudbuild_v1.SecretManagerSecret(
                        version_name=f"projects/{project_id}/secrets/dockerhub-user/versions/latest",
                        env="DOCKERHUB_USER",
                    ),
                    cloudbuild_v1.SecretManagerSecret(
                        version_name=f"projects/{project_id}/secrets/dockerhub-pass/versions/latest",
                        env="DOCKERHUB_PASS",
                    ),
                ]
            ),
        )

        if rate_limiter:
            await rate_limiter.acquire()

        op = await build_client.create_build(project_id=project_id, build=build)
        if on_submitted is not None:
            on_submitted(task_id)
        result = await op.result()

        if result.status != cloudbuild_v1.Build.Status.SUCCESS:
            return task_id, "fail", f"status={result.status.name}"

        task_digest = await loop.run_in_executor(executor, fetch_dockerhub_digest, task_id)
        if not task_digest:
            return task_id, "fail", "digest fetch failed"

        await record_built_image(task_id, task_digest, base_digest, df_sha, images_map)
        return task_id, "ok", task_digest

    except Exception as e:
        return task_id, "fail", str(e)


async def run_remote(
    task_ids: list[str],
    *,
    project_id: str,
    gcs_bucket: str,
    max_concurrency: Optional[int],
    max_builds_per_minute: Optional[int],
    force: bool,
    check_base_only: bool,
    base_digest_cache: dict[str, str],
    images_map: dict[str, dict[str, str]],
    storage_client: storage.Client,
) -> None:
    sem = asyncio.Semaphore(max_concurrency) if max_concurrency else None
    rate_limiter = RateLimiter(max_builds_per_minute) if max_builds_per_minute else None
    build_client = cloudbuild_v1.CloudBuildAsyncClient()
    executor = ThreadPoolExecutor(max_workers=max_concurrency or 64)

    total = len(task_ids)
    submitted_count = 0

    def on_submitted(task_id: str) -> None:
        nonlocal submitted_count
        submitted_count += 1
        print(f"[SUBMITTED {submitted_count}/{total}] {task_id}", flush=True)

    async def submit(task_id: str):
        if sem:
            async with sem:
                return await build_remote(
                    task_id,
                    project_id=project_id, gcs_bucket=gcs_bucket,
                    build_client=build_client, storage_client=storage_client,
                    executor=executor, rate_limiter=rate_limiter,
                    force=force, check_base_only=check_base_only,
                    base_digest_cache=base_digest_cache, images_map=images_map,
                    on_submitted=on_submitted,
                )
        return await build_remote(
            task_id,
            project_id=project_id, gcs_bucket=gcs_bucket,
            build_client=build_client, storage_client=storage_client,
            executor=executor, rate_limiter=rate_limiter,
            force=force, check_base_only=check_base_only,
            base_digest_cache=base_digest_cache, images_map=images_map,
            on_submitted=on_submitted,
        )

    rate_info = f", rate_limit={int(max_builds_per_minute * 0.9)}/min" if max_builds_per_minute else ""
    print(f"Submitting {total} builds (max_concurrency={max_concurrency}{rate_info})...\n", flush=True)

    skipped = succeeded = failed = 0
    completed = 0
    futures = [asyncio.create_task(submit(t)) for t in task_ids]
    for coro in asyncio.as_completed(futures):
        task_id, status, msg = await coro
        completed += 1
        progress = f"({completed}/{total})"
        if status == "skip":
            print(f"[SKIP {progress}] {task_id} ({msg[:23]}...)", flush=True)
            skipped += 1
        elif status == "ok":
            print(f"[OK {progress}] {task_id} ({msg[:23]}...)", flush=True)
            succeeded += 1
        else:
            print(f"[FAIL {progress}] {task_id}: {msg}", flush=True)
            failed += 1

    print(f"\nRemote builds complete: {skipped} skipped, {succeeded} succeeded, {failed} failed")
    executor.shutdown(wait=False)


# --- entrypoint ----------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Build per-task SETA Dockerfiles")
    parser.add_argument("--local", action="store_true", help="Build locally instead of via Cloud Build")
    parser.add_argument("--filter", type=str, default=None,
                        help="Only build tasks whose dir name contains this substring")
    parser.add_argument("--max-concurrency", type=int, default=None,
                        help="Max concurrent remote builds (default: unlimited)")
    parser.add_argument("--max-builds-per-minute", type=int, default=None,
                        help="Rate-limit build submissions (applies 0.9 headroom)")
    parser.add_argument("--project", type=str, default="openreward",
                        help="GCP project ID for Cloud Build (default: openreward)")
    parser.add_argument("--bucket", type=str, default="openreward_cloudbuild",
                        help="GCS bucket for build contexts (default is the project's "
                             "auto-created Cloud Build staging bucket)")
    parser.add_argument("--dry-run", action="store_true", help="List tasks without building")
    parser.add_argument("--force", action="store_true", help="Rebuild even if cache hits")
    parser.add_argument("--check-base", action="store_true",
                        help="Only rebuild when the FROM-line image's remote digest has moved. "
                             "Run periodically to catch upstream base updates.")
    parser.add_argument("--rescue-cache", action="store_true",
                        help="For each task missing from task_images.json, try resolving "
                             "the existing image digest from the registry. Use when an "
                             "earlier bulk build succeeded but digest capture failed — "
                             "this recovers the digest map without re-running Cloud Build.")

    args = parser.parse_args()

    if not args.dry_run and (not SOURCE_BUCKET or not SOURCE_PREFIX):
        raise SystemExit(
            "SETA_SOURCE_BUCKET and SETA_SOURCE_PREFIX env vars must be set "
            "(they point at the OR bucket holding task data). See scripts/run.md "
            "for the concrete values."
        )

    task_ids = discover_tasks(args.filter)
    filter_msg = f" matching '{args.filter}'" if args.filter else ""
    print(f"Found {len(task_ids)} tasks{filter_msg}")

    images_map = read_images_map()

    if args.dry_run:
        for task_id in task_ids:
            entry = images_map.get(task_id)
            status = (
                f"(cached: {entry.get('task_image_digest', '')[:23]}...)"
                if entry else "(not built)"
            )
            print(f"  {image_name(task_id)} {status}")
        return

    if not task_ids:
        print("No tasks to build.")
        return

    LOCAL_CACHE.mkdir(parents=True, exist_ok=True)
    storage_client = storage.Client()

    # Hydrate local cache + resolve unique base image digests once.
    base_digest_cache = prefetch_base_digests(task_ids, storage_client)

    if args.rescue_cache:
        run_rescue_cache(
            task_ids, storage_client,
            base_digest_cache=base_digest_cache, images_map=images_map,
        )
        return

    if args.local:
        run_local(
            task_ids, storage_client,
            force=args.force, check_base_only=args.check_base,
            base_digest_cache=base_digest_cache, images_map=images_map,
        )
    else:
        asyncio.run(run_remote(
            task_ids,
            project_id=args.project, gcs_bucket=args.bucket,
            max_concurrency=args.max_concurrency,
            max_builds_per_minute=args.max_builds_per_minute,
            force=args.force, check_base_only=args.check_base,
            base_digest_cache=base_digest_cache, images_map=images_map,
            storage_client=storage_client,
        ))


if __name__ == "__main__":
    main()
