#!/usr/bin/env python3
"""Official Flash MultiConnector layout; MemCache backend replaces the pool backend."""
import json
import sys


def connector(role, rank):
    producer = role == "P"
    kv_role = "kv_producer" if producer else "kv_consumer"
    store = {"backend": "memcache", "memcache_dp_init_barrier": True, "use_layerwise": False,
             "lookup_rpc_port": (37000 if producer else 37100) + rank}
    if not producer:
        store.update(consumer_is_to_load=False, consumer_is_to_put=False)
    return {"kv_connector": "MultiConnector", "kv_role": kv_role,
            "engine_id": "0" if producer else "1",
            "kv_connector_extra_config": {"connectors": [
                {"kv_connector": "MooncakeConnectorV2", "kv_role": kv_role,
                 "kv_port": "36680" if producer else "36580"},
                {"kv_connector": "AscendStoreConnector", "kv_role": kv_role,
                 "kv_connector_extra_config": store}]}}


if __name__ == "__main__":
    print(json.dumps(connector(sys.argv[1], int(sys.argv[2])), separators=(",", ":")))
