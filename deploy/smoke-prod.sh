#!/usr/bin/env bash
# Production smoke test for a freshly-deployed ClaimsForge.
# Usage: ./smoke-prod.sh http://YOUR_VULTR_IP
set -euo pipefail

BASE="${1:-http://localhost}"
PASS=0
FAIL=0

check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  ✅ $name"
    PASS=$((PASS+1))
  else
    echo "  ❌ $name"
    FAIL=$((FAIL+1))
  fi
}

echo "==> Smoke testing $BASE"

echo
echo "[1] Static + health"
check "GET /                       (200)" curl -fsS "$BASE/"
check "GET /api/claimsforge/health (gemini enabled)" \
  bash -c "curl -fsS $BASE/api/claimsforge/health | grep -qE '\"enabled\"[[:space:]]*:[[:space:]]*true'"
check "GET /api/demo-scenarios     (3 scenarios)" \
  bash -c "curl -fsS $BASE/api/demo-scenarios | grep -qE '\"id\"[[:space:]]*:[[:space:]]*\"mug-crack\"'"

echo
echo "[2] Static assets"
check "GET /api/demo-images/mug_crack.jpg (200)" \
  curl -fsS -o /dev/null "$BASE/api/demo-images/mug_crack.jpg"

echo
echo "[3] End-to-end POST /api/claim"
RESP=$(curl -fsS -X POST "$BASE/api/claim" \
  -H "Content-Type: application/json" \
  -d '{"message":"我的杯子破了，订单号 ORD-TEST-001","session_id":"smoke","image_id":"demo:mug_crack.jpg","estimated_value_cents":2400}' \
  --max-time 30)
if echo "$RESP" | grep -q '"full_refund"\|"partial_refund"\|"replacement"\|"escalated": true'; then
  echo "  ✅ /api/claim returned a valid result"
  PASS=$((PASS+1))
  AMT=$(echo "$RESP" | python3 -c "import sys, json; d=json.load(sys.stdin); o=d.get('final_offer'); print(f\"final_offer: {o['offer_type']} ¥{o['amount_cents']/100:.2f}\" if o else f\"escalated: {d.get('escalated')}\")")
  echo "     $AMT"
else
  echo "  ❌ /api/claim returned unexpected:"
  echo "$RESP" | head -c 300
  FAIL=$((FAIL+1))
fi

echo
echo "============================================================"
echo "  PASS: $PASS   FAIL: $FAIL"
echo "============================================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
