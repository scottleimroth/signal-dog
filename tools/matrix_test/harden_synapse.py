"""Hardens a freshly generated Synapse config for a throwaway, private test server. Run inside the Synapse image with
the config volume at /data:   python harden_synapse.py

Prints only the names of the settings it changed, never any value from the file (the file holds secrets)."""
import yaml

PATH = "/data/homeserver.yaml"
cfg = yaml.safe_load(open(PATH))
changed = []


def put(key, value):
    cfg[key] = value
    changed.append(key)


put("report_stats", False)
put("federation_domain_whitelist", [])                 # no federation at all
put("allow_public_rooms_over_federation", False)
put("trusted_key_servers", [])                         # never ask another server for keys
put("suppress_key_server_warning", True)
put("enable_registration", True)                       # the network is private; accounts are made by the test script
put("enable_registration_without_verification", True)
put("presence", {"enabled": False})
put("url_preview_enabled", False)
put("rc_message", {"per_second": 200, "burst_count": 400})
put("rc_registration", {"per_second": 100, "burst_count": 200})
put("rc_login", {"address": {"per_second": 100, "burst_count": 200}, "account": {"per_second": 100, "burst_count": 200},
                 "failed_attempts": {"per_second": 100, "burst_count": 200}})
put("rc_joins", {"local": {"per_second": 100, "burst_count": 200}, "remote": {"per_second": 1, "burst_count": 1}})
put("rc_invites", {"per_room": {"per_second": 100, "burst_count": 200}, "per_user": {"per_second": 100, "burst_count": 200}})
# one plain HTTP client listener on the private network, no federation resource
put("listeners", [{"port": 8008, "tls": False, "type": "http", "x_forwarded": False, "bind_addresses": ["0.0.0.0"],
                   "resources": [{"names": ["client"], "compress": False}]}])
yaml.safe_dump(cfg, open(PATH, "w"), default_flow_style=False)
print("changed settings:", ", ".join(changed))
