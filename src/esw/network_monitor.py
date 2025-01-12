#!/usr/bin/env python3

import os
import time
import rospy
from mrover.msg import NetworkBandwidth
from typing import Optional

# Credit: https://stackoverflow.com/questions/15616378/python-network-bandwidth-monitor


def get_bytes(t: str, interface: str) -> int:
    with open("/sys/class/net/" + interface + "/statistics/" + t + "_bytes", "r") as f:
        data = f.read()
        return int(data)


def get_iface(default: str) -> Optional[str]:
    eth_addrs = [addr for addr in os.listdir("/sys/class/net/") if addr.startswith("e")]

    eth_iface = None
    if len(eth_addrs) == 1:
        eth_iface = eth_addrs[0]
    elif len(eth_addrs) > 1:
        eth_iface = default if default in eth_addrs else eth_addrs[0]

    return eth_iface


if __name__ == "__main__":
    rospy.init_node("network_monitor")
    iface = get_iface(rospy.get_param("default_network_iface"))

    if iface is not None:
        pub = rospy.Publisher("network_bandwidth", NetworkBandwidth, queue_size=1)
        while True:
            tx1 = get_bytes("tx", iface)
            rx1 = get_bytes("rx", iface)
            time.sleep(1)
            tx2 = get_bytes("tx", iface)
            rx2 = get_bytes("rx", iface)
            tx_speed = (tx2 - tx1) * 8.0 / 1000000.0  # Mbps
            rx_speed = (rx2 - rx1) * 8.0 / 1000000.0  # Mbps
            pub.publish(NetworkBandwidth(tx_speed, rx_speed))

    else:
        rospy.logerr(f"Node {rospy.get_name()} cannot locate valid network interface.")
