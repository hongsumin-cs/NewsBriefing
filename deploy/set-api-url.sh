#!/bin/bash
# EC2 퍼블릭 IP 를 조회해 S3 의 config.json 만 갱신. 재빌드 불필요.
#
#   bash deploy/set-api-url.sh                      인스턴스 이름으로 자동 조회
#   bash deploy/set-api-url.sh http://12.34.56.78:3000   직접 지정
#
# 인스턴스를 껐다 켜면 퍼블릭 IP 가 바뀐다. 그때 이것만 돌리면 된다.

set -euo pipefail

BUCKET="${BUCKET:-sean.hong2-s3}"
EC2_NAME="${EC2_NAME:-sean.hong-ec2}"
PORT="${PORT:-3000}"

API_URL="${1:-}"

if [ -z "$API_URL" ]; then
  echo "EC2 IP 조회 — 태그 Name=$EC2_NAME"
  IP=$(aws ec2 describe-instances \
        --filters "Name=tag:Name,Values=$EC2_NAME" \
                  "Name=instance-state-name,Values=running" \
        --query 'Reservations[].Instances[].PublicIpAddress' \
        --output text)

  if [ -z "$IP" ] || [ "$IP" = "None" ]; then
    echo "실행 중인 인스턴스를 찾지 못했습니다." >&2
    echo "  EC2_NAME=<태그> bash deploy/set-api-url.sh" >&2
    echo "  bash deploy/set-api-url.sh http://<IP>:$PORT" >&2
    exit 1
  fi
  API_URL="http://$IP:$PORT"
fi

echo "apiUrl = $API_URL"

TMP=$(mktemp)
printf '{ "apiUrl": "%s" }\n' "$API_URL" > "$TMP"

# max-age 를 짧게 — IP 가 바뀌었을 때 브라우저가 옛 값을 붙들지 않게
aws s3 cp "$TMP" "s3://$BUCKET/config.json" \
  --content-type "application/json" \
  --cache-control "max-age=30"
rm -f "$TMP"

echo "완료 — 브라우저 새로고침하면 새 주소로 붙습니다."
echo "http://$BUCKET.s3-website-us-east-1.amazonaws.com/"
