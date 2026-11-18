#!/usr/bin/env bash
# Download a GGUF model from a Docker Hub `ai/<model>` OCI artifact (no Docker daemon needed).
# Usage: scripts/pull_model.sh ai/qwen3 4b-instruct-2507-q4_K_M models/qwen3-4b-instruct-2507-q4_k_m.gguf
set -euo pipefail
repo="$1"; tag="$2"; out="$3"
token=$(curl -fsSL "https://auth.docker.io/token?service=registry.docker.io&scope=repository:${repo}:pull" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
manifest=$(curl -fsSL -H "Authorization: Bearer $token" \
  -H "Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json" \
  "https://registry-1.docker.io/v2/${repo}/manifests/${tag}")
digest=$(echo "$manifest" | python3 -c '
import json, sys
m = json.load(sys.stdin)
layers = [l for l in m["layers"] if "gguf" in l["mediaType"]
          or l.get("annotations", {}).get("org.cncf.model.filepath", "").endswith(".gguf")]
print(max(layers, key=lambda l: l["size"])["digest"])')
mkdir -p "$(dirname "$out")"
echo "Downloading ${repo}:${tag} (${digest}) -> ${out}"
curl -fL --retry 4 -H "Authorization: Bearer $token" -o "$out" \
  "https://registry-1.docker.io/v2/${repo}/blobs/${digest}"
echo "$digest" | sed 's/sha256://' | awk -v f="$out" '{print $1"  "f}' | sha256sum -c -
