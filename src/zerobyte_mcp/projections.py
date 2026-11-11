"""Positive output projections; new upstream fields are not exposed automatically."""
import math

from .client import SafeError

BACKUP = "id shortId name volumeId repositoryId enabled cronExpression compressionMode maxRetries retryDelay lastBackupAt lastBackupStatus nextBackupAt createdAt updatedAt".split()
REPOSITORY = "id shortId name type compressionMode status lastChecked autoCheckEnabled createdAt updatedAt".split()
VOLUME = "id shortId name type sourceKind status autoRemount lastHealthCheck createdAt updatedAt".split()
TASK = "id kind status resourceType resourceId cancellationRequested createdAt startedAt updatedAt finishedAt".split()
HISTORY = "id kind outcome status createdAt startedAt finishedAt".split()
STATS = "total_size total_uncompressed_size compression_ratio compression_progress compression_space_saving snapshots_count".split()
PROGRESS = "seconds_elapsed seconds_remaining percent_done total_files files_done total_bytes bytes_done files_restored bytes_restored".split()


def pick(obj, keys):
    if not isinstance(obj, dict):
        raise SafeError("Unexpected upstream response shape.")
    # Nested values in scalar fields indicate drift. Omit, never serialize wholesale.
    return {k: obj[k] for k in keys if k in obj and (obj[k] is None or type(obj[k]) in {str, bool, int, float})}


def pick_numbers(obj, keys):
    """Do not expose text if a numeric upstream field changes shape."""
    values = pick(obj, keys)
    return {k: v for k, v in values.items()
            if v is None or type(v) is int or (type(v) is float and math.isfinite(v))}


def record(obj, kind):
    if kind == "backup":
        out = pick(obj, BACKUP)
        out["hasError"] = bool(obj.get("lastBackupError"))
        for name, fields in (("volume", VOLUME), ("repository", REPOSITORY)):
            if isinstance(obj.get(name), dict):
                out[name] = pick(obj[name], fields)
    elif kind == "repository":
        out = pick(obj, REPOSITORY)
        out["hasError"] = bool(obj.get("lastError"))
    elif kind == "volume":
        out = pick(obj, VOLUME)
        out["hasError"] = bool(obj.get("lastError"))
    elif kind == "task":
        out = pick(obj, TASK)
        out["hasError"] = bool(obj.get("error"))
        progress = obj.get("progress")
        if isinstance(progress, dict) and isinstance(progress.get("progress"), dict):
            out["progress"] = pick_numbers(progress["progress"], PROGRESS)
    elif kind == "history":
        out = pick(obj, HISTORY)
        out["hasMessage"] = bool(obj.get("message"))
        if isinstance(obj.get("target"), dict):
            out["target"] = pick(obj["target"], ["kind", "label", "scheduleShortId", "repositoryShortId", "snapshotId"])
    else:
        raise SafeError("Unknown response projection.")
    return out


def records(obj, kind):
    if not isinstance(obj, list) or len(obj) > 1000:
        raise SafeError("Expected a collection with at most 1000 items.")
    return [record(row, kind) for row in obj]
