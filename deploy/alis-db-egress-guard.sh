#!/usr/bin/env bash
set -euo pipefail

interface=${ALIS_DB_EGRESS_INTERFACE:-eth0}
research_ip=${ALIS_RESEARCH_PUBLIC_IP:-124.223.225.117}
control_ip=${ALIS_CONTROL_PUBLIC_IP:-47.119.146.111}
app_ip=${ALIS_APP_PUBLIC_IP:-124.221.86.123}
research_rate=${ALIS_RESEARCH_EGRESS_RATE:-128kbit}
control_rate=${ALIS_CONTROL_EGRESS_RATE:-128kbit}
app_rate=${ALIS_APP_EGRESS_RATE:-128kbit}

tc qdisc del dev "$interface" root 2>/dev/null || true
tc qdisc add dev "$interface" root handle 1: htb default 999
tc class replace dev "$interface" parent 1: classid 1:1 htb rate 1000mbit ceil 1000mbit
tc class replace dev "$interface" parent 1:1 classid 1:10 htb rate "$research_rate" ceil "$research_rate" burst 16k
tc class replace dev "$interface" parent 1:1 classid 1:20 htb rate "$control_rate" ceil "$control_rate" burst 16k
tc class replace dev "$interface" parent 1:1 classid 1:30 htb rate "$app_rate" ceil "$app_rate" burst 32k
tc class replace dev "$interface" parent 1:1 classid 1:999 htb rate 1000mbit ceil 1000mbit

tc qdisc replace dev "$interface" parent 1:10 handle 10: fq_codel
tc qdisc replace dev "$interface" parent 1:20 handle 20: fq_codel
tc qdisc replace dev "$interface" parent 1:30 handle 30: fq_codel
tc qdisc replace dev "$interface" parent 1:999 handle 999: fq_codel

tc filter replace dev "$interface" protocol ip parent 1: prio 10 u32 match ip dst "$research_ip/32" flowid 1:10
tc filter replace dev "$interface" protocol ip parent 1: prio 20 u32 match ip dst "$control_ip/32" flowid 1:20
tc filter replace dev "$interface" protocol ip parent 1: prio 30 u32 match ip dst "$app_ip/32" flowid 1:30
