#!/usr/bin/env python3
"""Probe HE20 and bulb with a fresh tinytuya connection (bypasses runtime session)."""
import os, sys, json, time

# Load keys from hermes .env
env_path = os.path.expandvars(r"%LOCALAPPDATA%\hermes\.env")
keys = {}
with open(env_path, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            keys[k.strip()] = v.strip().strip('"').strip("'")

import tinytuya

def probe(name, dev_id, ip, key, proto=3.5):
    print(f"--- {name} @ {ip} ---")
    d = tinytuya.Device(dev_id=dev_id, address=ip, local_key=key)
    d.set_version(proto)
    d.set_socketTimeout(4)
    t0 = time.time()
    try:
        status = d.status()
        print(f"  status() in {time.time()-t0:.1f}s: {json.dumps(status)[:300]}")
    except Exception as e:
        print(f"  status() FAILED in {time.time()-t0:.1f}s: {type(e).__name__}: {e}")
    finally:
        try:
            d.close()
        except Exception:
            pass

he20_key = keys.get("SMART_ROOM_TUYA_HE20_KEY", "")
bulb_key = keys.get("SMART_ROOM_TUYA_BULB_KEY", "")
print(f"keys loaded: he20={'yes' if he20_key else 'NO'}, bulb={'yes' if bulb_key else 'NO'}")

probe("HE20", "bfa17ce4b21e11893eaase", "192.168.1.182", he20_key)
probe("BULB", "bf670860f9a64316fcfdxa", "192.168.1.104", bulb_key)
