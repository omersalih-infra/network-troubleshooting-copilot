"""
config.example.py - template for collector.py settings.

Copy this file to config.py and fill in your own values:

    cp config.example.py config.py

config.py is listed in .gitignore and must never be committed.

The addresses below use the RFC 5737 documentation range (203.0.113.0/24)
as placeholders; they are not real. Replace them with the management
addresses of your own routers.
"""

# Management-plane login used by collector.py (read-only show commands only).
USERNAME = "CHANGE_ME"
PASSWORD = "CHANGE_ME"

# One entry per router. device_type is a Netmiko platform string.
# NOTE: "cisco_ios_telnet" sends credentials in clear text. It is acceptable
# only on an isolated lab management network; use "cisco_ios" (SSH) wherever
# the device supports it.
DEVICES = [
    {"name": "R-A1", "host": "203.0.113.2", "device_type": "cisco_ios"},
    {"name": "R-A2", "host": "203.0.113.3", "device_type": "cisco_ios_telnet"},
    {"name": "R-B1", "host": "203.0.113.4", "device_type": "cisco_ios"},
    {"name": "R-B2", "host": "203.0.113.5", "device_type": "cisco_ios"},
]

# Loopback0 address of each router, used as the ping target (and source) for
# the pairwise reachability matrix. These are the lab's own loopbacks from the
# report's addressing plan (Section 4.2); they are lab-internal, private
# addresses and must match the Loopback0 addresses configured on your routers.
LOOPBACKS = {
    "R-A1": "10.0.0.1",
    "R-A2": "10.0.0.2",
    "R-B1": "10.0.0.3",
    "R-B2": "10.0.0.4",
}
