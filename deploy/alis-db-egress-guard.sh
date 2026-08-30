#!/usr/bin/env bash
set -euo pipefail

interface=${ALIS_DB_EGRESS_INTERFACE:-eth0}
compute_ip=${ALIS_COMPUTE_PUBLIC_IP:-124.223.225.117}
app_ip=${ALIS_APP_PUBLIC_IP:-124.221.86.123}
compute_rate=${ALIS_COMPUTE_EGRESS_RATE:-96kbit}
app_rate=${ALIS_APP_EGRESS_RATE:-96kbit}
default_rate=${ALIS_DEFAULT_EGRESS_RATE:-128kbit}

tc qdisc del dev "$interface" root 2>/dev/null || true
tc qdisc add dev "$interface" root handle 1: htb default 999
tc class replace dev "$interface" parent 1: classid 1:1 htb rate 1000mbit ceil 1000mbit
tc class replace dev "$interface" parent 1:1 classid 1:10 htb rate "$compute_rate" ceil "$compute_rate" burst 16k
tc class replace dev "$interface" parent 1:1 classid 1:30 htb rate "$app_rate" ceil "$app_rate" burst 32k
tc class replace dev "$interface" parent 1:1 classid 1:999 htb rate "$default_rate" ceil "$default_rate"

tc qdisc replace dev "$interface" parent 1:10 handle 10: fq_codel
tc qdisc replace dev "$interface" parent 1:30 handle 30: fq_codel
tc qdisc replace dev "$interface" parent 1:999 handle 999: fq_codel

tc filter replace dev "$interface" protocol ip parent 1: prio 10 u32 match ip dst "$compute_ip/32" flowid 1:10
tc filter replace dev "$interface" protocol ip parent 1: prio 30 u32 match ip dst "$app_ip/32" flowid 1:30
