#!/usr/bin/env python3
"""Plan, resume, and verify original rollout MP4s in GitHub Releases.

`plan` is entirely offline and writes a PRIVATE manifest containing local paths.
`verify` only reads remote metadata. `sync` uploads assets and can recover only
its exactly labelled empty upload stubs. Uploaded/unknown collisions are kept;
it does not publish a local manifest or obtain new credentials.
Authentication is read from GH_TOKEN/GITHUB_TOKEN or an existing gh login.

Examples (do not publish either private manifest):
  python3 scripts/github-release-media.py plan --output /tmp/rollout-releases.json
  python3 scripts/github-release-media.py verify --manifest /tmp/rollout-releases.json
  python3 scripts/github-release-media.py sync --manifest /tmp/rollout-releases.json
For a prototype manifest with top-level entries, also pass --tag EXISTING_TAG.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "LocoManiLab/policy-rollouts"
ASSET_NAME = re.compile(r"[a-f0-9]{20}\.mp4")
API_VERSION = "2026-03-10"
OWNER_LABEL_PREFIX = "locomanilab-original:"


class TransferError(RuntimeError):
    def __init__(self, message: str, category: str = "validation"):
        if category not in {"validation", "transient", "auth", "collision"}:
            raise ValueError("Unknown failure category")
        super().__init__(message)
        self.category = category


class APIError(TransferError):
    def __init__(self, status: int, headers: dict | None = None, rate_limited: bool = False):
        # Do not include request headers, tokens, response bodies, or local paths.
        category = (
            "transient" if rate_limited or status == 429 or status == 408 or 500 <= status < 600
            else "auth" if status in (401, 403)
            else "collision" if status == 422
            else "validation"
        )
        super().__init__(f"GitHub API returned HTTP {status}", category=category)
        self.status = status
        self.headers = headers or {}
        self.rate_limited = rate_limited


def owner_label(entry: dict) -> str:
    clip_id, fingerprint = entry.get("id"), entry.get("sha256")
    if (
        not isinstance(clip_id, str)
        or not re.fullmatch(r"[a-f0-9]{20}", clip_id)
        or entry.get("assetName") != clip_id + ".mp4"
        or not isinstance(fingerprint, str)
        or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
    ):
        raise TransferError("A public clip ID, matching name, and SHA256 are required for upload ownership.")
    return OWNER_LABEL_PREFIX + clip_id + ":" + fingerprint


def owned_empty_starter(asset: dict, entry: dict) -> bool:
    return (
        isinstance(asset, dict)
        and asset.get("label") == owner_label(entry)
        and asset.get("name") == entry["assetName"]
        and asset.get("state") == "starter"
        and type(asset.get("size")) is int
        and asset["size"] == 0
        and asset.get("digest") is None
        and asset.get("content_type") == "video/mp4"
        and type(asset.get("id")) is int
        and asset["id"] > 0
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def write_failure_report(options, error: Exception) -> None:
    """Atomically publish only safe recovery fields, never exception details."""
    policy = getattr(options, "policy", None)
    if not isinstance(policy, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", policy):
        policy = None
    repository = getattr(options, "effective_repository", DEFAULT_REPO)
    # The optional worker status is scoped to this public migration. An
    # unexpected repository cannot place arbitrary CLI text into that status.
    if repository != DEFAULT_REPO:
        repository = None
    payload = {
        "version": 1,
        "repository": repository,
        "manifestSha256": getattr(options, "manifest_sha256", None),
        "policy": policy,
        "category": error.category if isinstance(error, TransferError) else "validation",
        "failedAt": datetime.now(timezone.utc).isoformat(),
    }
    target = options.failure_report
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".rollout-failure-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def existing_token(required: bool) -> str | None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token
    if shutil.which("gh"):
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    if required:
        raise TransferError("Existing API/gh authentication is required; no credentials were created.", category="auth")
    return None


def retry_delay(error: APIError, attempt: int) -> float:
    headers = {key.lower(): value for key, value in error.headers.items()}
    if headers.get("retry-after"):
        return max(0, float(headers["retry-after"]))
    if headers.get("x-ratelimit-remaining") == "0":
        return max(1, float(headers.get("x-ratelimit-reset", 0)) - time.time() + 1)
    if error.status in (403, 429):
        return 60 * (2**attempt)
    return min(60, 2**attempt)


def retryable(error: APIError) -> bool:
    return error.status in (429, 500, 502, 503, 504) or (error.status == 403 and error.rate_limited)


class GitHubAPI:
    def __init__(self, repository: str, token: str | None, write_interval: float = 8):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise TransferError("Expected repository in OWNER/REPO form.")
        self.repository = repository
        self.prefix = "https://api.github.com/repos/" + repository
        self.token = token
        self.write_interval = write_interval
        # Each new Actions job observes an initial write gap as well.
        self.last_write = time.monotonic()
        self.rate_state = {}

    def pause(self, delay: float) -> None:
        time.sleep(delay)

    def wait_write_gap(self) -> None:
        if self.last_write is not None:
            self.pause(max(0, self.write_interval - (time.monotonic() - self.last_write)))

    def request(self, method: str, url: str, body=None, content_type=None, size=None, redirects=0):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"api.github.com", "uploads.github.com"}:
            raise TransferError("Refusing to send credentials outside GitHub API/upload hosts.")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise TransferError("Unexpected authenticated endpoint.")
        if method not in ("GET", "HEAD"):
            self.wait_write_gap()
            self.last_write = time.monotonic()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "locomanilab-release-media",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if content_type:
            headers["Content-Type"] = content_type
        if size is not None:
            headers["Content-Length"] = str(size)
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=300)
        try:
            target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            rate = {key: response_headers[key] for key in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-used", "x-ratelimit-reset", "x-ratelimit-resource", "retry-after") if key in response_headers}
            observation = {"event": "api-rate", "host": parsed.hostname, "method": method, "status": response.status, **rate}
            self.rate_state[parsed.hostname + ":" + rate.get("x-ratelimit-resource", "unspecified")] = observation
            print(json.dumps(observation), flush=True)
            data = response.read()
            if response.status in (301, 302, 307, 308) and method == "GET":
                if redirects >= 5:
                    raise TransferError("Too many GitHub API redirects.")
                return self.request(method, urllib.parse.urljoin(url, response_headers["location"]), redirects=redirects + 1)
            if response.status >= 300:
                try:
                    message = json.loads(data).get("message", "").lower()
                except (ValueError, AttributeError):
                    message = ""
                limited = bool(response_headers.get("retry-after")) or response_headers.get("x-ratelimit-remaining") == "0" or "rate limit" in message
                raise APIError(response.status, response_headers, limited)
            try:
                return (json.loads(data) if data else None), response_headers
            except ValueError as error:
                raise TransferError("GitHub response could not be decoded; remote state must be reconciled.", category="transient") from error
        except (OSError, http.client.HTTPException) as error:
            raise TransferError("GitHub connection ended; remote state must be checked before retry.", category="transient") from error
        finally:
            connection.close()

    def get(self, url: str):
        for attempt in range(5):
            try:
                return self.request("GET", url)
            except APIError as error:
                if not retryable(error) or attempt == 4:
                    raise
                self.pause(retry_delay(error, attempt))
            except TransferError as error:
                if error.category != "transient":
                    raise
                if attempt == 4:
                    raise
                self.pause(min(60, 2**attempt))

    def release(self, tag: str, create: bool = False, title: str | None = None):
        url = self.prefix + "/releases/tags/" + urllib.parse.quote(tag, safe="")
        try:
            return self.get(url)[0]
        except APIError as error:
            if error.status != 404 or not create:
                raise
        payload = {"tag_name": tag, "name": title or tag, "draft": False, "prerelease": False}
        try:
            return self.request("POST", self.prefix + "/releases", json.dumps(payload).encode(), "application/json")[0]
        except TransferError:
            # A create may have committed even if its response was lost.
            return self.get(url)[0]

    def assets(self, release: dict) -> dict:
        url = release["assets_url"] + "?per_page=100"
        assets = {}
        while url:
            page, headers = self.get(url)
            for item in page:
                if item["name"] in assets:
                    raise TransferError("Duplicate remote asset names need manual resolution.")
                assets[item["name"]] = item
            links = re.findall(r'<([^>]+)>;\s*rel="([^"]+)"', headers.get("link", ""))
            url = next((target for target, relation in links if relation == "next"), None)
        return assets

    def upload(self, release: dict, entry: dict):
        # GitHub's documented upload hypermedia template exposes both query
        # parameters: .../assets{?name,label}. Refuse an unknown template rather
        # than upload without the ownership label needed for safe recovery.
        template = release.get("upload_url", "")
        if not isinstance(template, str):
            raise TransferError("GitHub upload template is missing.")
        expansion = re.search(r"\{\?([A-Za-z0-9_,]+)\}$", template)
        if not expansion or not {"name", "label"}.issubset(expansion.group(1).split(",")):
            raise TransferError("GitHub upload template does not expose name and label.")
        base = template[:expansion.start()]
        if "{" in base or "}" in base:
            raise TransferError("GitHub upload template contains unresolved path variables.")
        separator = "&" if "?" in base else "?"
        url = base + separator + urllib.parse.urlencode({"name": entry["assetName"], "label": owner_label(entry)})
        with Path(entry["localPath"]).open("rb") as stream:
            return self.request("POST", url, stream, entry.get("contentType", "video/mp4"), entry["bytes"])[0]

    def delete_starter(self, asset: dict, entry: dict):
        if not owned_empty_starter(asset, entry):
            raise TransferError("Refusing to delete an unowned or nonempty release asset.", category="collision")
        url = self.prefix + "/releases/assets/" + str(asset["id"])
        # Wait before the fresh GET, so the write gap does not leave a stale
        # starter observation sitting for eight seconds before deletion.
        self.wait_write_gap()
        try:
            current = self.get(url)[0]
        except APIError as error:
            if error.status == 404:
                return
            raise
        if not isinstance(current, dict) or current.get("id") != asset["id"] or not owned_empty_starter(current, entry):
            raise TransferError("The owned empty starter changed; reconcile without deleting it.", category="collision")
        self.request("DELETE", url)
        print(json.dumps({"event": "owned-empty-starter-deleted", "id": entry["id"], "assetId": asset["id"]}), flush=True)

def check_local(entry: dict) -> None:
    path = Path(entry["localPath"])
    if not path.is_file() or path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
        raise TransferError(f"Local original verification failed for {entry['assetName']}.")


def check_remote(asset: dict, entry: dict) -> None:
    if not isinstance(asset, dict):
        raise TransferError(f"Remote metadata missing for {entry['assetName']}.")
    if asset.get("state") != "uploaded" or asset.get("size") != entry["bytes"]:
        raise TransferError(f"Remote state/size mismatch for {entry['assetName']}.", category="collision")
    if asset.get("name") != entry["assetName"] or asset.get("content_type") != entry.get("contentType", "video/mp4"):
        raise TransferError(f"Remote name/media type mismatch for {entry['assetName']}.", category="collision")
    if asset.get("digest") != "sha256:" + entry["sha256"]:
        raise TransferError(f"Remote digest missing or mismatched for {entry['assetName']}; collision retained.", category="collision")


def sync_asset(api, release, entry, known):
    check_local(entry)
    name = entry["assetName"]
    for attempt in range(5):
        asset = known.get(name)
        if asset:
            if not owned_empty_starter(asset, entry):
                check_remote(asset, entry)
                return asset, "existing"
            delete_error = None
            try:
                api.delete_starter(asset, entry)
            except APIError as error:
                if error.status != 404 and not retryable(error):
                    raise
                delete_error = error
                if error.status == 429 or error.rate_limited:
                    api.pause(retry_delay(error, attempt))
            except TransferError:
                # DELETE may have succeeded despite a lost response. Always
                # read the release again before attempting another upload.
                pass
            known.clear()
            known.update(api.assets(release))
            remaining = known.get(name)
            if remaining:
                if remaining.get("state") == "uploaded":
                    check_remote(remaining, entry)
                    return remaining, "reconciled"
                if not owned_empty_starter(remaining, entry) or remaining["id"] != asset["id"]:
                    raise TransferError(f"Changed or unowned incomplete asset {name} retained.", category="collision")
                if attempt == 4:
                    raise TransferError(f"Owned empty starter {name} could not be removed; rerun to reconcile.", category="transient")
                api.pause(retry_delay(delete_error, attempt) if delete_error and not delete_error.rate_limited and delete_error.status != 429 else min(60, 2**attempt))
                continue
        # A retry must still upload the original bytes bound by the label.
        # Local validation failures are fatal, rather than ambiguous POSTs.
        check_local(entry)
        try:
            asset = api.upload(release, entry)
            check_remote(asset, entry)
            known[name] = asset
            return asset, "uploaded"
        except APIError as error:
            if error.status != 422 and not retryable(error):
                raise
            delay = retry_delay(error, attempt)
            upload_error = error
            if error.status == 429 or error.rate_limited:
                # Do not even send the reconciliation GET before the server's
                # requested backoff has elapsed.
                api.pause(delay)
                delay = 0
        except TransferError as error:
            if error.category != "transient":
                raise
            delay = min(60, 2**attempt)
            upload_error = None
        # Always reconcile an ambiguous upload before another POST. Never
        # overwrite an uploaded collision, even if GitHub returned 422/502.
        known.clear()
        known.update(api.assets(release))
        if name in known:
            if known[name].get("state") == "uploaded":
                check_remote(known[name], entry)
                return known[name], "reconciled"
            if not owned_empty_starter(known[name], entry):
                raise TransferError(f"Unowned or nonempty incomplete asset {name} retained.", category="collision")
        if upload_error is not None and upload_error.status == 422 and name not in known:
            raise upload_error
        if attempt == 4:
            raise TransferError(f"Upload did not complete for {name}; rerun to resume.", category="transient")
        api.pause(delay)
    raise AssertionError("unreachable")


def plan(root: Path, repository: str, tag_prefix: str) -> dict:
    catalog_path = root / "media/results/policy-rollouts-catalog.json"
    catalog = json.loads(catalog_path.read_text())
    exported = json.loads((root / ".site-media/export-manifest.json").read_text())
    if not exported.get("complete") or exported.get("catalogSha256") != sha256(catalog_path):
        raise TransferError("Completed original export is missing or stale.")
    videos = {entry["id"]: entry["video"] for entry in exported["entries"]}
    expected_ids = {clip["id"] for group in catalog["groups"] for clip in group["clips"]}
    if len(videos) != len(exported["entries"]) or set(videos) != expected_ids:
        raise TransferError("Original export IDs differ from the catalog.")
    policies = {}
    for group in catalog["groups"]:
        policy = group["policy"]
        release = policies.setdefault(policy, {"policy": policy, "tag": f"{tag_prefix}-{policy}-v1", "entries": []})
        for clip in group["clips"]:
            video = videos[clip["id"]]
            path = (root / ".site-media" / video["path"]).resolve()
            if not path.is_file() or path.stat().st_size != video["bytes"] or video["bytes"] != clip["bytes"]:
                raise TransferError("Exported original size mismatch.")
            release["entries"].append({"id": clip["id"], "assetName": clip["id"] + ".mp4", "localPath": str(path), "bytes": video["bytes"], "sha256": video["sha256"], "policy": policy, "setting": group["setting"], "outcome": clip["outcome"]})
    for release in policies.values():
        release["count"] = len(release["entries"])
        release["totalBytes"] = sum(entry["bytes"] for entry in release["entries"])
        if release["count"] > 1000:
            raise TransferError("Policy exceeds the 1000 assets per release limit.")
    return {"version": 1, "privateLocalManifest": True, "warning": "Never publish this local-path manifest.", "repository": repository, "count": sum(release["count"] for release in policies.values()), "totalBytes": exported["videoBytes"], "releases": sorted(policies.values(), key=lambda release: release["policy"])}


def manifest_releases(manifest: dict, tag: str | None, policy: str | None = None, base_path: Path | None = None):
    releases = manifest.get("releases")
    if releases is None:
        if not tag:
            raise TransferError("A prototype manifest needs --tag EXISTING_TAG.")
        releases = [{"tag": tag, "entries": manifest["entries"]}]
    elif tag:
        releases = [release for release in releases if release["tag"] == tag]
        if not releases:
            raise TransferError("The requested tag is absent from the manifest.")
    if policy:
        releases = [release for release in releases if release.get("policy") == policy]
        if not releases:
            raise TransferError("The requested policy is absent from the manifest.")
    for release in releases:
        entries = release["entries"]
        names = [entry["assetName"] for entry in entries]
        if len(names) > 1000 or len(set(names)) != len(names):
            raise TransferError("Release count or filename collision in local manifest.")
        for entry in entries:
            if not ASSET_NAME.fullmatch(entry["assetName"]) or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]):
                raise TransferError("Expected public clip filename and SHA256.")
            if not 0 < entry["bytes"] < 2 * 1024**3:
                raise TransferError("Each release asset must be under 2 GiB.")
            if "localPath" not in entry:
                relative = Path(entry.get("file", ""))
                if base_path is None or not relative.parts or relative.is_absolute() or ".." in relative.parts:
                    raise TransferError("Public source paths must stay inside the media repository.")
                resolved = (base_path / relative).resolve()
                if not resolved.is_relative_to(base_path.resolve()):
                    raise TransferError("Source path escapes the media repository.")
                entry["localPath"] = str(resolved)
    return releases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    planner = subcommands.add_parser("plan", help="Offline private staging manifest")
    planner.add_argument("--output", type=Path, required=True)
    planner.add_argument("--repo", default=DEFAULT_REPO)
    planner.add_argument("--tag-prefix", default="rollouts")
    for name in ("verify", "sync"):
        command = subcommands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--repo")
        command.add_argument("--tag")
        command.add_argument("--policy")
        command.add_argument("--report", type=Path)
        command.add_argument("--failure-report", type=Path)
        if name == "sync":
            command.add_argument("--create-releases", action="store_true")
            command.add_argument("--write-interval", type=float, default=8)
    options = parser.parse_args()
    if options.command == "plan":
        manifest = plan(ROOT, options.repo, options.tag_prefix)
        write_private(options.output, manifest)
        print(json.dumps({"privateManifest": str(options.output), "count": manifest["count"], "totalBytes": manifest["totalBytes"], "releases": [{key: release[key] for key in ("policy", "tag", "count", "totalBytes")} for release in manifest["releases"]]}))
        return 0
    try:
        return transfer(options)
    except Exception as error:
        if options.failure_report:
            try:
                write_failure_report(options, error)
            except OSError:
                print("Safe failure report could not be written.", file=sys.stderr)
        raise


def transfer(options) -> int:
    manifest_bytes = options.manifest.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    options.manifest_sha256 = manifest_sha256
    manifest = json.loads(manifest_bytes)
    options.effective_repository = options.repo or manifest.get("repository", DEFAULT_REPO)
    releases = manifest_releases(manifest, options.tag, options.policy, options.manifest.resolve().parent)
    mutating = options.command == "sync"
    interval = options.write_interval if mutating else 8
    if interval < 8:
        raise TransferError("Use at least 8 seconds between writes for the bulk transfer.")
    api = GitHubAPI(options.effective_repository, existing_token(mutating), interval)
    results = []
    started_at = datetime.now(timezone.utc).isoformat()

    def progress_report(complete):
        return {"repository": api.repository, "mode": options.command, "manifestSha256": manifest_sha256, "startedAt": started_at, "completedAt": datetime.now(timezone.utc).isoformat() if complete else None, "complete": complete, "verified": len(results), "rateLimits": api.rate_state, "entries": results}
    for item in releases:
        release = api.release(item["tag"], mutating and options.create_releases, item.get("title"))
        assets = api.assets(release)
        missing = sum(entry["assetName"] not in assets for entry in item["entries"])
        if len(assets) + missing > 1000:
            raise TransferError("Existing unrelated assets would exceed the release asset limit.")
        for index, entry in enumerate(item["entries"], 1):
            if mutating:
                asset, operation = sync_asset(api, release, entry, assets)
            else:
                asset = assets.get(entry["assetName"])
                if not asset:
                    raise TransferError(f"Missing remote asset {entry['assetName']}.", category="collision")
                check_remote(asset, entry)
                operation = "verified"
            # Reports are public-safe: no credentials or local source paths.
            result = {"tag": item["tag"], "id": entry["id"], "operation": operation, "name": asset["name"], "state": asset["state"], "contentType": asset["content_type"], "bytes": asset["size"], "digest": asset["digest"], "url": asset["browser_download_url"]}
            results.append(result)
            print(json.dumps(result), flush=True)
            if options.report and (len(results) % 25 == 0 or index == len(item["entries"])):
                write_private(options.report, progress_report(False))
    if options.report:
        write_private(options.report, progress_report(True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TransferError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        print("Local migration input or verification failed.", file=sys.stderr)
        raise SystemExit(1)
