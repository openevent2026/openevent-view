import unittest

from openevent.view.config import ConfigError, parse_config


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = parse_config({})
        self.assertEqual(config.server.host, "127.0.0.1")
        self.assertEqual(config.server.port, 8080)
        self.assertEqual(config.server.request_timeout_seconds, 10.0)
        self.assertEqual(config.server.max_request_body_bytes, 65536)
        self.assertEqual(config.openevent.target, "127.0.0.1:9527")
        self.assertEqual(config.openevent.rpc_timeout_seconds, 10.0)
        self.assertEqual(config.openevent.channel_cache_size, 4096)
        self.assertEqual(config.openevent.channel_lookup_workers, 8)
        self.assertEqual(config.history.default_order, "desc")

    def test_rejects_large_max_limit(self):
        with self.assertRaises(ConfigError):
            parse_config({"history": {"max_limit": 1001}})

    def test_parses_rpc_timeout(self):
        config = parse_config({"openevent": {"rpc_timeout_seconds": 2.5}})
        self.assertEqual(config.openevent.rpc_timeout_seconds, 2.5)

    def test_rejects_non_positive_rpc_timeout(self):
        with self.assertRaises(ConfigError):
            parse_config({"openevent": {"rpc_timeout_seconds": 0}})

    def test_rejects_non_finite_rpc_timeout(self):
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_config({"openevent": {"rpc_timeout_seconds": value}})

    def test_parses_http_and_channel_limits(self):
        config = parse_config(
            {
                "server": {
                    "request_timeout_seconds": 2.5,
                    "max_request_body_bytes": 1024,
                },
                "openevent": {
                    "channel_cache_size": 32,
                    "channel_lookup_workers": 4,
                },
            }
        )
        self.assertEqual(config.server.request_timeout_seconds, 2.5)
        self.assertEqual(config.server.max_request_body_bytes, 1024)
        self.assertEqual(config.openevent.channel_cache_size, 32)
        self.assertEqual(config.openevent.channel_lookup_workers, 4)

    def test_rejects_too_many_channel_lookup_workers(self):
        with self.assertRaises(ConfigError):
            parse_config({"openevent": {"channel_lookup_workers": 65}})


if __name__ == "__main__":
    unittest.main()
