# -*- coding: utf-8 -*-
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1; see LICENSE.

import json
import logging

from proxy.base_proxy import IpCache


class FakeCacheClient:
    def __init__(self, data):
        self.data = data

    def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [key for key in self.data if key.startswith(prefix)]

    def get(self, key):
        return self.data.get(key)


def _ip_json(ip):
    return json.dumps(
        {"ip": ip, "port": 8080, "user": "u", "password": "p", "expired_time_ts": 2000000000}
    )


def test_load_all_ip_skips_bad_entry_and_keeps_the_rest(caplog):
    ip_cache = IpCache.__new__(IpCache)
    ip_cache.cache_client = FakeCacheClient(
        {
            "kuaidaili_1.1.1.1": _ip_json("1.1.1.1"),
            "kuaidaili_2.2.2.2": "not-json",
            "kuaidaili_3.3.3.3": _ip_json("3.3.3.3"),
        }
    )

    with caplog.at_level(logging.ERROR):
        ip_list = ip_cache.load_all_ip(proxy_brand_name="kuaidaili")

    assert [ip.ip for ip in ip_list] == ["1.1.1.1", "3.3.3.3"]
    error_messages = [record.getMessage() for record in caplog.records]
    assert len(error_messages) == 1
    assert "kuaidaili_2.2.2.2" in error_messages[0]
