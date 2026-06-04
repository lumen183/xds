set -e

addr=$(awk '/__tracepoint_nvme_setup_cmd/ {print $1}' /proc/kallsyms)

[ -z "$addr" ] && { echo "no nvme_setup_cmd tp"; exit 1; }

rmmod p2p_dev &>/dev/null || true
rmmod stub &>/dev/null || true

insmod ./p2p_dev.ko tp_nvme_setup_cmd_addr=0x${addr}
chmod 666 /dev/p2p_device
echo done