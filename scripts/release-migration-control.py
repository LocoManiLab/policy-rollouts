#!/usr/bin/env python3
"""Validate a public migration plan and assemble a complete verification proof.

No network requests, authentication, uploads, or Git writes occur here. The
workflow obtains authenticated remote reports with github-release-media.py.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess


REPOSITORY = "LocoManiLab/policy-rollouts"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_plan(path):
    plan_bytes = path.read_bytes()
    plan = json.loads(plan_bytes)
    require(plan.get("version") == 1 and plan.get("repository") == REPOSITORY, "Wrong plan version/repository")
    require(plan.get("privateLocalManifest") is False, "The migration plan must be public-safe")
    catalog_bytes = (path.parent / "catalog.json").read_bytes()
    require(digest(catalog_bytes) == plan.get("catalogSha256"), "Catalog bytes changed")
    catalog = json.loads(catalog_bytes)
    catalog_rows = [clip for group in catalog["groups"] for clip in group["clips"]]
    expected = {clip["id"]: (group["policy"], clip["bytes"]) for group in catalog["groups"] for clip in group["clips"]}
    require(len(catalog_rows) == len(expected), "Catalog contains duplicate clip IDs")
    require(len(expected) == catalog["stats"]["clips"] == plan.get("count") == 7607, "Expected all 7,607 unique clips")
    require(len(plan["releases"]) == catalog["stats"]["policies"] == 12, "Expected all 12 policies")
    policies = set()
    tags = set()
    entries = {}
    catalog_policies = {policy["id"] for policy in catalog["policies"]}
    for release in plan["releases"]:
        policy, tag = release["policy"], release["tag"]
        require(policy in catalog_policies, "Unknown policy in migration plan")
        require(policy not in policies and tag not in tags, "Repeated policy/tag")
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag) and tag not in {"latest", "main", "master", "nightly"}, "A fixed version tag is required")
        require(0 < len(release["entries"]) <= 1000, "Release asset count exceeds limit")
        policies.add(policy)
        tags.add(tag)
        for entry in release["entries"]:
            clip_id = entry["id"]
            require(clip_id not in entries and re.fullmatch(r"[a-f0-9]{20}", clip_id), "Invalid/repeated clip ID")
            require(expected.get(clip_id) == (policy, entry["bytes"]), "Clip policy/size differs from catalog")
            require(entry.get("policy") == policy, "Wrong entry policy")
            require(entry.get("assetName") == clip_id + ".mp4", "Wrong public asset name")
            require(entry.get("file") == f"originals/{policy}/{clip_id}.mp4", "Unexpected source file")
            require("localPath" not in entry and re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]), "Private path or invalid video digest")
            poster = entry["poster"]
            require(poster.get("path") == f"rollout-posters/{clip_id}.jpg" and poster.get("bytes", 0) > 0 and re.fullmatch(r"[a-f0-9]{64}", poster.get("sha256", "")), "Invalid poster metadata")
            entries[clip_id] = entry
    require(set(entries) == set(expected), "Plan does not cover the complete catalog")
    require(sum(entry["bytes"] for entry in entries.values()) == plan.get("totalBytes") == 4737404922, "Wrong original byte total")
    return plan, entries, digest(plan_bytes)


def verify_prototype(prototype, entries):
    require(prototype.get("repository") == REPOSITORY and prototype.get("playback") is True and prototype.get("seek") is True, "Prototype playback and seek must have passed")
    verified_at = prototype.get("verifiedAt")
    require(isinstance(verified_at, str), "Missing prototype timestamp")
    datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
    samples = prototype.get("samples", [])
    require(len(samples) == 5 and len({sample["id"] for sample in samples}) == 5, "Expected exactly five original prototype clips")
    policies = set()
    successful_long_seeks = 0
    for sample in samples:
        entry = entries.get(sample["id"])
        require(entry is not None, "Prototype clip not in this catalog")
        require(sample.get("sha256") == entry["sha256"] and sample.get("bytes") == entry["bytes"], "Prototype original bytes/digest changed")
        require(sample.get("duration") == entry["duration"], "Prototype duration differs from original clip")
        require(sample.get("playback") is True and sample.get("error") is None, "Prototype playback did not pass")
        if sample.get("seek") is True:
            require(isinstance(sample.get("seekSeconds"), (int, float)) and 0 <= sample["seekSeconds"] <= entry["duration"], "Prototype seek lies outside its original duration")
        policies.add(entry["policy"])
        if sample.get("duration", 0) >= 60 and sample.get("seek") is True and sample.get("seekSeconds", 0) >= 60:
            successful_long_seeks += 1
    require(len(policies) >= 3 and successful_long_seeks >= 2, "Cross-policy playback and two long seeks are required")


def verify_request(request, plan, plan_hash, prototype):
    for key in ("repository", "catalogSha256", "count", "totalBytes"):
        require(request.get(key) == plan[key], "Migration request does not match plan: " + key)
    require(request.get("version") == 1 and request.get("planSha256") == plan_hash, "Migration request plan hash differs")
    require(request.get("prototypeVerifiedAt") == prototype["verifiedAt"], "Prototype confirmation changed")


def verify_git_tree(entries, directory):
    result = subprocess.run(["git", "-C", str(directory), "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", "originals"], check=True, capture_output=True)
    actual = {path.decode() for path in result.stdout.split(b"\0") if path}
    require(actual == {entry["file"] for entry in entries.values()}, "Committed originals do not cover the exact plan")


def verify_report(report, expected, tags):
    require(report.get("repository") == REPOSITORY and report.get("complete") is True, "Remote report is incomplete")
    rows = report.get("entries", [])
    require(report.get("verified") == len(rows) == len(expected), "Remote verification count differs")
    seen = set()
    for row in rows:
        clip_id = row["id"]
        require(clip_id in expected and clip_id not in seen, "Unknown/repeated remote clip")
        entry = expected[clip_id]
        require(row.get("tag") == tags[entry["policy"]], "Remote report has the wrong policy tag")
        require(row.get("bytes") == entry["bytes"] and row.get("digest") == "sha256:" + entry["sha256"], "Remote SHA256/size mismatch")
        require(row.get("operation") in {"uploaded", "existing", "reconciled", "verified"}, "Unverified operation")
        require(row.get("name") == entry["assetName"] and row.get("state") == "uploaded" and row.get("contentType") == "video/mp4", "Remote filename/state/MIME is not verified")
        expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tags[entry['policy']]}/{clip_id}.mp4"
        require(row.get("url") == expected_url, "Unexpected public download URL")
        require("localPath" not in row, "A public report cannot expose source paths")
        seen.add(clip_id)
    return seen


def failure_status(failure, plan, plan_hash, run_id, scope="policy"):
    """Expose only a classified failure, never source paths or exception text."""
    require(failure.get("version") == 1 and failure.get("repository") == REPOSITORY, "Wrong failure record repository")
    require(failure.get("manifestSha256") == plan_hash, "Failure record plan fingerprint differs")
    require(scope in {"policy", "final-verify"}, "Invalid failure scope")
    if scope == "policy":
        require(failure.get("policy") in {release["policy"] for release in plan["releases"]}, "Wrong failure policy")
    else:
        require(failure.get("policy") is None, "Final verify failure must cover the complete plan")
    require(failure.get("category") in {"transient", "auth", "collision", "validation"}, "Unclassified failure")
    require(isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]*", run_id), "Invalid Actions run ID")
    datetime.fromisoformat(failure["failedAt"].replace("Z", "+00:00"))
    return {"version": 1, "repository": REPOSITORY, "catalogSha256": plan["catalogSha256"],
            "planSha256": plan_hash, "count": plan["count"], "runId": int(run_id),
            "phase": "failed", "category": failure["category"], "policy": failure["policy"], "scope": scope,
            "failedAt": failure["failedAt"]}


def build_proof(plan, entries, prototype, policy_reports, remote_report, plan_hash):
    verify_prototype(prototype, entries)
    tags = {release["policy"]: release["tag"] for release in plan["releases"]}
    require(set(policy_reports) == set(tags), "All 12 policy reports are required")
    for policy, report in policy_reports.items():
        require(report.get("mode") == "sync" and report.get("manifestSha256") == plan_hash, "Policy report mode or plan fingerprint differs")
        datetime.fromisoformat(report["completedAt"].replace("Z", "+00:00"))
        selected = {key: entry for key, entry in entries.items() if entry["policy"] == policy}
        verify_report(report, selected, tags)
    # A new authenticated final GET over every release is required, rather than
    # trusting only the reports produced during the long upload sequence.
    require(remote_report.get("mode") == "verify" and remote_report.get("manifestSha256") == plan_hash, "A final verify command over this exact plan is required")
    require(all(row.get("operation") == "verified" for row in remote_report.get("entries", [])), "Final report must contain fresh GET verifications")
    read_started = datetime.fromisoformat(remote_report["startedAt"].replace("Z", "+00:00"))
    read_finished = datetime.fromisoformat(remote_report["completedAt"].replace("Z", "+00:00"))
    upload_finished = max(datetime.fromisoformat(report["completedAt"].replace("Z", "+00:00")) for report in policy_reports.values())
    require(read_started >= upload_finished and read_finished >= read_started, "Final reads must occur after all policy uploads")
    verify_report(remote_report, entries, tags)
    return {
        "version": 1, "provider": "github-releases", "repository": REPOSITORY,
        "complete": True, "catalogSha256": plan["catalogSha256"],
        "verification": {key: prototype[key] for key in ("playback", "seek", "verifiedAt")},
        "count": len(entries), "totalBytes": plan["totalBytes"],
        "remoteVerifiedAt": datetime.now(timezone.utc).isoformat(),
        "policies": [{"id": release["policy"], "tag": release["tag"], "versioned": True} for release in plan["releases"]],
        "entries": [{"id": clip_id, "policy": entry["policy"], "video": {"name": entry["assetName"], "bytes": entry["bytes"], "sha256": entry["sha256"], "remoteVerified": True}, "poster": entry["poster"]} for clip_id, entry in sorted(entries.items())],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "finalize", "failure-status"))
    parser.add_argument("--plan", type=Path, default=Path("release-plan.json"))
    parser.add_argument("--prototype", type=Path, default=Path("prototype-verification.json"))
    parser.add_argument("--request", type=Path)
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--event", choices=("push", "workflow_dispatch"), default="workflow_dispatch")
    parser.add_argument("--prototype-input", default="false")
    parser.add_argument("--git-tree", action="store_true")
    parser.add_argument("--reports", type=Path)
    parser.add_argument("--remote-report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--failure-report", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--failure-scope", choices=("policy", "final-verify"), default="policy")
    options = parser.parse_args()
    require(options.repository == REPOSITORY, "Workflow is running in an unexpected repository")
    plan, entries, plan_hash = load_plan(options.plan)
    prototype = json.loads(options.prototype.read_text())
    verify_prototype(prototype, entries)
    if options.command == "failure-status":
        require(options.failure_report is not None and options.output is not None, "Failure status needs a safe failure report and output")
        status = failure_status(json.loads(options.failure_report.read_text()), plan, plan_hash, options.run_id, options.failure_scope)
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(status, separators=(",", ":")) + "\n")
        print(json.dumps({"failureStatus": True, "category": status["category"], "runId": status["runId"]}))
        return
    if options.command == "preflight":
        if options.event == "push":
            require(options.request is not None, "Push trigger requires a migration request")
            verify_request(json.loads(options.request.read_text()), plan, plan_hash, prototype)
        else:
            require(options.prototype_input == "true", "Manual dispatch must confirm prototype validation")
        if options.git_tree:
            verify_git_tree(entries, options.plan.resolve().parent)
        print(json.dumps({"ready": True, "repository": REPOSITORY, "count": len(entries), "planSha256": plan_hash}))
        return
    require(options.reports is not None and options.remote_report is not None and options.output is not None, "Finalization needs policy reports, a fresh remote report, and output")
    reports = {release["policy"]: json.loads((options.reports / (release["policy"] + ".json")).read_text()) for release in plan["releases"]}
    proof = build_proof(plan, entries, prototype, reports, json.loads(options.remote_report.read_text()), plan_hash)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(proof, separators=(",", ":")) + "\n")
    print(json.dumps({"complete": True, "verified": len(entries), "publicProof": str(options.output)}))


if __name__ == "__main__":
    main()
