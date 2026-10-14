# LocoManiLab original selected policy rollouts

This repository stages 7,607 original MP4s for 12 versioned GitHub Releases.
Playback and long seeking were verified using the five original prototype recordings.

Push source files in one policy per batch, then copy `migration-request.template.json` to `migration-request.json` and commit it as the final trigger. The request binds the exact catalog and plan bytes. Earlier source pushes do not trigger migration. Manual dispatch additionally requires `prototype_verified=true`.

The 12 upload jobs run sequentially, pace mutating API calls at eight seconds, and resume only after original and remote SHA256/size verification. Existing mismatches are never replaced or deleted. Each job has a three-hour timeout; a failed run can be rerun to resume.

Only after all original files pass a fresh remote verification does the workflow commit `hosting/policy-rollouts-releases.json` as the public completion proof. Fixed version tags follow the uploader's no-overwrite rule; this is not a claim of server-enforced release immutability.
