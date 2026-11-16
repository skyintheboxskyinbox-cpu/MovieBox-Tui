#!/usr/bin/env bash
# capacity-wait.sh — Oracle Always Free ARM capacity waiter (Mumbai, ap-mumbai-1)
#
# Why: Mumbai ARM (VM.Standard.A1.Flex) often answers "Out of capacity".
# The fix the user was shown in the reels is: retry no faster than every
# 60 seconds (faster -> 429 TooManyRequests) and try the OTHER Availability
# Domain (AD-1 full -> AD-2 free). This script does exactly that, hands-free,
# until an instance launches or you stop it with Ctrl-C.
#
# SAFETY: launches ONLY the Always Free shape VM.Standard.A1.Flex with
# 1 OCPU / 4 GB RAM (inside the free 2 OCPU / 12 GB pool). It never touches
# Billing, Upgrade, or any paid shape. Stop it any time with Ctrl-C.
#
# Needs (from the new Oracle account, after signup):
#   OCI_COMPARTMENT_ID  - compartment OCID (tenancy root works: ocid1.tenancy.oc1...)
#   OCI_SUBNET_ID       - subnet OCID of the VCN in ap-mumbai-1
#   OCI_IMAGE_ID        - Ubuntu 24.04 ARM image OCID (list with the command below)
#   OCI_SSH_KEY         - path to your SSH public key file (default ~/.ssh/id_rsa.pub)
#   OCI_AVAILABILITY_DOMAIN - optional: start with this AD, e.g. "xxxx:AP-MUMBAI-1-AD-1"
#
# Find the image OCID after signup:
#   oci compute image list --compartment-id "$OCI_COMPARTMENT_ID" \
#     --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
#     --shape VM.Standard.A1.Flex --region ap-mumbai-1
#
# Run:
#   OCI_COMPARTMENT_ID=ocid1... OCI_SUBNET_ID=ocid1... OCI_IMAGE_ID=ocid1... \
#     bash capacity-wait.sh
#   # or export the three values first, then: bash capacity-wait.sh
#
# Optional: MAX_ATTEMPTS=N (default 0 = keep trying until success / Ctrl-C),
#   OCPU=1 MEM_GB=4 DISPLAY_NAME=moviebox-oracle LOG_FILE=capacity-wait.log
#
# On success it prints the instance OCID and its public IP, then exits.
set -u

REGION="${OCI_REGION:-ap-mumbai-1}"
SHAPE="${OCI_SHAPE:-VM.Standard.A1.Flex}"
OCPU="${OCPU:-1}"
MEM_GB="${MEM_GB:-4}"
DISPLAY_NAME="${DISPLAY_NAME:-moviebox-oracle}"
SSH_KEY="${OCI_SSH_KEY:-$HOME/.ssh/id_rsa.pub}"
LOG_FILE="${LOG_FILE:-capacity-wait.log}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-0}"
POLL_SECONDS=60   # never faster: Oracle rate-limits quicker polling (429)

die() { echo "ERROR: $*" >&2; exit 1; }
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"; }

command -v oci >/dev/null 2>&1 || die "OCI CLI not found. Install it first: pip install oci-cli && oci setup config"
command -v jq >/dev/null 2>&1 || die "jq not found. Install it first: sudo apt install -y jq"
[ -n "${OCI_COMPARTMENT_ID:-}" ] || die "Set OCI_COMPARTMENT_ID (compartment or tenancy OCID)"
[ -n "${OCI_SUBNET_ID:-}" ] || die "Set OCI_SUBNET_ID (subnet OCID in $REGION)"
[ -n "${OCI_IMAGE_ID:-}" ] || die "Set OCI_IMAGE_ID (Ubuntu 24.04 ARM image OCID — see header)"
[ -f "$SSH_KEY" ] || die "SSH public key not found at $SSH_KEY (set OCI_SSH_KEY)"

# Availability Domains to rotate through: given one first, then the rest.
ADS="$(oci iam availability-domain list --compartment-id "$OCI_COMPARTMENT_ID" \
  --region "$REGION" --query 'data[*].name' --raw-output 2>/dev/null | tr '\t' '\n' | grep -v '^$')"
[ -n "$ADS" ] || die "Could not list availability domains for $REGION — check OCI config / compartment"
if [ -n "${OCI_AVAILABILITY_DOMAIN:-}" ]; then
  ADS="$(printf '%s\n%s\n' "$OCI_AVAILABILITY_DOMAIN" "$ADS" | awk '!seen[$0]++')"
fi
mapfile -t AD_LIST <<< "$ADS"
log "Capacity waiter started: shape=$SHAPE ${OCPU}OCPU/${MEM_GB}GB region=$REGION ADs: ${AD_LIST[*]} (poll every ${POLL_SECONDS}s, max attempts: ${MAX_ATTEMPTS:-unlimited})"

attempt=0
while :; do
  attempt=$((attempt + 1))
  if [ "$MAX_ATTEMPTS" -gt 0 ] && [ "$attempt" -gt "$MAX_ATTEMPTS" ]; then
    log "Gave up after $MAX_ATTEMPTS attempts — no capacity found. Re-run later."
    exit 2
  fi
  ad="${AD_LIST[$(( (attempt - 1) % ${#AD_LIST[@]} ))]}"
  log "Attempt $attempt: launching in $ad ..."
  out="$(oci compute instance launch \
    --compartment-id "$OCI_COMPARTMENT_ID" \
    --availability-domain "$ad" \
    --shape "$SHAPE" \
    --shape-config "{\"ocpus\": $OCPU, \"memoryInGBs\": $MEM_GB}" \
    --image-id "$OCI_IMAGE_ID" \
    --subnet-id "$OCI_SUBNET_ID" \
    --assign-public-ip true \
    --display-name "$DISPLAY_NAME" \
    --ssh-authorized-keys-file "$SSH_KEY" \
    --region "$REGION" 2>&1)"
  rc=$?
  if [ $rc -eq 0 ]; then
    inst_id="$(printf '%s' "$out" | jq -r '.data.id // empty')"
    log "SUCCESS: instance launched in $ad — $inst_id"
    log "Waiting for RUNNING state..."
    oci compute instance get --instance-id "$inst_id" --region "$REGION" \
      --wait-for-state RUNNING --wait-interval-seconds 10 >/dev/null 2>&1 || true
    pub_ip="$(oci compute instance list-vnics --instance-id "$inst_id" --region "$REGION" \
      --query 'data[0]."public-ip"' --raw-output 2>/dev/null)"
    log "Instance RUNNING. Public IP: ${pub_ip:-<not assigned yet — re-check in a minute>}"
    echo "INSTANCE_ID=$inst_id"
    echo "PUBLIC_IP=${pub_ip:-}"
    echo "Next: ssh ubuntu@${pub_ip:-<ip>}  then follow ORACLE-SETUP.md (Install section)."
    exit 0
  fi
  # Capacity errors are retryable; anything else (bad subnet/image/auth) is not.
  if printf '%s' "$out" | grep -qi "out of capacity\|OutOfCapacity\|capacity.*exceed\|InternalError.*capacity\|500.*capacity"; then
    log "Attempt $attempt ($ad): Out of capacity — retrying in ${POLL_SECONDS}s (next AD: ${AD_LIST[$(( attempt % ${#AD_LIST[@]} ))]})."
  else
    log "Attempt $attempt ($ad): NON-CAPACITY error — stopping so a wrong setting is not retried blindly:"
    printf '%s\n' "$out" | tee -a "$LOG_FILE" | head -20
    exit 3
  fi
  sleep "$POLL_SECONDS"
done
