#!/usr/bin/env bash
set -euo pipefail

interface=${ALIS_DB_EGRESS_INTERFACE:-eth0}
compute_ip=${ALIS_COMPUTE_PUBLIC_IP:-124.223.225.117}
app_ip=${ALIS_APP_PUBLIC_IP:-124.221.86.123}
reset_day=${ALIS_EGRESS_RESET_DAY:-11}
soft_limit_gb=${ALIS_EGRESS_SOFT_LIMIT_GB:-120}
hard_limit_gb=${ALIS_EGRESS_HARD_LIMIT_GB:-150}
state_dir=${ALIS_EGRESS_STATE_DIR:-/var/lib/alis-egress-guard}
state_file="$state_dir/state"
lock_file="$state_dir/lock"

normal_parent_rate=${ALIS_EGRESS_NORMAL_PARENT_RATE:-448kbit}
normal_compute_rate=${ALIS_COMPUTE_EGRESS_RATE:-192kbit}
normal_app_rate=${ALIS_APP_EGRESS_RATE:-192kbit}
normal_default_rate=${ALIS_DEFAULT_EGRESS_RATE:-64kbit}
soft_parent_rate=${ALIS_EGRESS_SOFT_PARENT_RATE:-128kbit}
soft_compute_rate=${ALIS_EGRESS_SOFT_COMPUTE_RATE:-48kbit}
soft_app_rate=${ALIS_EGRESS_SOFT_APP_RATE:-48kbit}
soft_default_rate=${ALIS_EGRESS_SOFT_DEFAULT_RATE:-32kbit}
hard_rate=${ALIS_EGRESS_HARD_RATE:-1kbit}
hard_parent_rate=${ALIS_EGRESS_HARD_PARENT_RATE:-128kbit}
management_rate=${ALIS_EGRESS_MANAGEMENT_RATE:-64kbit}

mkdir -p "$state_dir"
chmod 700 "$state_dir"
exec 9>"$lock_file"
flock -x 9

period_key() {
    local today day
    today=$(date +%Y-%m-%d)
    day=$(date +%d)
    if ((10#$day >= reset_day)); then
        printf '%s-%02d\n' "$(date +%Y-%m)" "$reset_day"
    else
        printf '%s-%02d\n' "$(date -d 'last month' +%Y-%m)" "$reset_day"
    fi
}

load_state() {
    STATE_EXISTS=0
    PERIOD_KEY=""
    LAST_ACCOUNTED_BYTES=0
    PERIOD_TX_BYTES=0
    MODE="normal"
    if [[ -f "$state_file" ]]; then
        STATE_EXISTS=1
        # shellcheck disable=SC1090
        source "$state_file"
        # One-time compatibility with the original whole-interface counter.
        LAST_ACCOUNTED_BYTES=${LAST_ACCOUNTED_BYTES:-0}
    fi
}

save_state() {
    local temporary="$state_file.tmp"
    umask 077
    printf 'PERIOD_KEY=%q\nLAST_ACCOUNTED_BYTES=%q\nPERIOD_TX_BYTES=%q\nMODE=%q\nUPDATED_AT=%q\n' \
        "$PERIOD_KEY" "$LAST_ACCOUNTED_BYTES" "$PERIOD_TX_BYTES" "$MODE" "$(date --iso-8601=seconds)" >"$temporary"
    mv "$temporary" "$state_file"
}

class_sent_bytes() {
    local classid=$1
    tc -s class show dev "$interface" classid "$classid" 2>/dev/null \
        | awk '/ Sent [0-9]+ bytes / {print $2; found=1; exit} END {if (!found) print 0}'
}

account_bytes() {
    local current_period current_tx delta compute_bytes app_bytes
    current_period=$(period_key)
    # Count only the two database cross-node classes. SSH, monitoring,
    # backups and other host traffic must never consume the DB budget.
    compute_bytes=$(class_sent_bytes 1:10)
    app_bytes=$(class_sent_bytes 1:30)
    current_tx=$((compute_bytes + app_bytes))
    if [[ "$PERIOD_KEY" != "$current_period" ]]; then
        PERIOD_KEY="$current_period"
        if ((STATE_EXISTS == 0)); then
            PERIOD_TX_BYTES=$((${ALIS_EGRESS_INITIAL_USED_GB:-0} * 1000 * 1000 * 1000))
        else
            PERIOD_TX_BYTES=0
        fi
        LAST_ACCOUNTED_BYTES="$current_tx"
        return
    fi
    if ((current_tx >= LAST_ACCOUNTED_BYTES)); then
        delta=$((current_tx - LAST_ACCOUNTED_BYTES))
    else
        # qdisc/class counters reset after reboot or a mode transition.
        delta=$current_tx
    fi
    PERIOD_TX_BYTES=$((PERIOD_TX_BYTES + delta))
    LAST_ACCOUNTED_BYTES=$current_tx
}

select_mode() {
    local soft_bytes hard_bytes
    soft_bytes=$((soft_limit_gb * 1000 * 1000 * 1000))
    hard_bytes=$((hard_limit_gb * 1000 * 1000 * 1000))
    if [[ "${ALIS_EGRESS_FORCE_FREEZE:-0}" == "1" ]] || ((PERIOD_TX_BYTES >= hard_bytes)); then
        MODE="hard"
    elif ((PERIOD_TX_BYTES >= soft_bytes)); then
        MODE="soft"
    else
        MODE="normal"
    fi
}

apply_tc() {
    local parent_rate compute_rate app_rate default_rate compute_ceil app_ceil
    case "$MODE" in
        hard)
            parent_rate="$hard_parent_rate"
            compute_rate="$hard_rate"
            app_rate="$hard_rate"
            default_rate="$management_rate"
            compute_ceil="$hard_rate"
            app_ceil="$hard_rate"
            ;;
        soft)
            parent_rate="$soft_parent_rate"
            compute_rate="$soft_compute_rate"
            app_rate="$soft_app_rate"
            default_rate="$soft_default_rate"
            compute_ceil="$soft_parent_rate"
            app_ceil="$soft_parent_rate"
            ;;
        *)
            parent_rate="$normal_parent_rate"
            compute_rate="$normal_compute_rate"
            app_rate="$normal_app_rate"
            default_rate="$normal_default_rate"
            compute_ceil="$normal_parent_rate"
            app_ceil="$normal_parent_rate"
            ;;
    esac

    tc qdisc del dev "$interface" root 2>/dev/null || true
    tc qdisc add dev "$interface" root handle 1: htb default 999
    tc class replace dev "$interface" parent 1: classid 1:1 htb rate "$parent_rate" ceil "$parent_rate"
    tc class replace dev "$interface" parent 1:1 classid 1:10 htb rate "$compute_rate" ceil "$compute_ceil" burst 16k
    tc class replace dev "$interface" parent 1:1 classid 1:30 htb rate "$app_rate" ceil "$app_ceil" burst 32k
    tc class replace dev "$interface" parent 1:1 classid 1:999 htb rate "$default_rate" ceil "$parent_rate" burst 16k
    tc qdisc replace dev "$interface" parent 1:10 handle 10: fq_codel
    tc qdisc replace dev "$interface" parent 1:30 handle 30: fq_codel
    tc qdisc replace dev "$interface" parent 1:999 handle 999: fq_codel
    tc filter replace dev "$interface" protocol ip parent 1: prio 10 u32 match ip dst "$compute_ip/32" flowid 1:10
    tc filter replace dev "$interface" protocol ip parent 1: prio 30 u32 match ip dst "$app_ip/32" flowid 1:30
}

print_status() {
    printf 'period=%s mode=%s used_bytes=%s used_gb=%s soft_limit_gb=%s hard_limit_gb=%s reset_day=%s\n' \
        "$PERIOD_KEY" "$MODE" "$PERIOD_TX_BYTES" \
        "$(awk -v bytes="$PERIOD_TX_BYTES" 'BEGIN {printf "%.3f", bytes/1000000000}')" \
        "$soft_limit_gb" "$hard_limit_gb" "$reset_day"
}

load_state
account_bytes
previous_mode="$MODE"
select_mode
case "${1:-apply}" in
    apply)
        apply_tc
        save_state
        ;;
    account)
        if [[ "$MODE" != "$previous_mode" ]] || ! tc qdisc show dev "$interface" | grep -q 'htb 1:'; then
            apply_tc
            LAST_ACCOUNTED_BYTES=0
        fi
        save_state
        ;;
    status)
        save_state
        print_status
        ;;
    *)
        echo "usage: $0 [apply|account|status]" >&2
        exit 2
        ;;
esac
