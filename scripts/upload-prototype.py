"""Publish a small, byte-verified playback test with the repository's token."""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / "prototype.json").read_text())
repository = "LocoManiLab/policy-rollouts"
assert manifest["repository"] == repository
token = os.environ["GH_TOKEN"]
base = f"https://api.github.com/repos/{repository}"


def request(method, url, data=None, content_type="application/json"):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"api.github.com", "uploads.github.com"}:
        raise RuntimeError("Unexpected GitHub request destination")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
        "Content-Type": content_type,
        "User-Agent": "LocoManiLab-rollout-publication",
    }
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=120) as response:
        safe_rate = {k:response.headers[k] for k in ["x-ratelimit-resource", "x-ratelimit-limit", "x-ratelimit-remaining", "retry-after"] if k in response.headers}
        result = json.load(response)
    print(json.dumps({"request": method, "host": parsed.hostname, "rate": safe_rate}), flush=True)
    return result


for entry in manifest["entries"]:
    data = (root / "prototype" / entry["assetName"]).read_bytes()
    assert len(data) == entry["bytes"]
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]

tag = manifest["tag"]
try:
    release = request("GET", base + "/releases/tags/" + urllib.parse.quote(tag))
except urllib.error.HTTPError as error:
    if error.code != 404:
        raise
    release = request("POST", base + "/releases", json.dumps({
        "tag_name": tag,
        "target_commitish": os.environ.get("GITHUB_SHA", "main"),
        "name": "Playback validation samples",
        "body": "Five original selected rollouts across five policies, including successes, failures, and long recordings. Used to validate in-page playback and seeking before publishing the complete 7,607-video selected library. Original MP4 bytes are preserved.",
        "prerelease": True,
        "make_latest": "false",
    }).encode())

existing = {asset["name"]: asset for asset in release["assets"]}
uploaded = []
for index, entry in enumerate(manifest["entries"]):
    name = entry["assetName"]
    asset = existing.get(name)
    if asset is None:
        if index:
            time.sleep(8)
        upload_url = release["upload_url"].split("{")[0] + "?" + urllib.parse.urlencode({"name": name})
        asset = request("POST", upload_url, (root / "prototype" / name).read_bytes(), "video/mp4")
    if asset["size"] != entry["bytes"] or asset.get("digest") != "sha256:" + entry["sha256"]:
        raise RuntimeError(f"Remote sample verification failed: {name}")
    if asset.get("content_type") != "video/mp4" or asset["state"] != "uploaded":
        raise RuntimeError(f"Unexpected remote media type or state: {name}")
    uploaded.append({"name": name, "size": asset["size"], "digest": asset["digest"], "url": asset["browser_download_url"]})
    print(json.dumps({"verified": name, "bytes": asset["size"]}), flush=True)
print(json.dumps({"release": release["html_url"], "samples": uploaded}), flush=True)
