#!/bin/bash
# React 를 빌드해 S3 에 올린다. CloudShell 에서 실행.
#
#   bash deploy/frontend.sh
#
# 저장소를 clone/pull 한 상태여야 하고, Node 18+ 가 필요하다.

set -euo pipefail

BUCKET="${BUCKET:-sean.hong2-s3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$ROOT/client"

echo "── 빌드 ──"
npm install --silent
npm run build

echo
echo "── S3 업로드 — s3://$BUCKET ──"
# 해시가 박힌 에셋은 오래 캐시, index.html 은 캐시하지 않는다.
aws s3 sync dist/ "s3://$BUCKET/" \
  --exclude "index.html" --exclude "config.json" \
  --cache-control "max-age=31536000,immutable" \
  --delete

aws s3 cp dist/index.html "s3://$BUCKET/index.html" \
  --content-type "text/html; charset=utf-8" \
  --cache-control "no-cache"

echo
echo "── API 주소 ──"
bash "$ROOT/deploy/set-api-url.sh" "$@"
