import unittest

from openevent.view.cli import _override_server
from openevent.view.config import ConfigError, parse_config


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = parse_config({})
        self.assertEqual(config.history.default_limit, 100)
        self.assertEqual(config.history.max_limit, 1000)
        self.assertEqual(config.history.fetch_batch_size, 1000)

    def test_history_constraints(self):
        for value in (True, 0, 1001):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_config({"history": {"max_limit": value}})
        with self.assertRaises(ConfigError):
            parse_config({"history": {"default_limit": 9, "max_limit": 8}})

    def test_timeout_and_worker_validation(self):
        with self.assertRaises(ConfigError):
            parse_config({"server": {"request_timeout_seconds": 0}})
        with self.assertRaises(ConfigError):
            parse_config({"openevent": {"channel_lookup_workers": 65}})

    def test_cli_overrides_keep_port_validation(self):
        config = parse_config({})
        self.assertEqual(_override_server(config, None, 9090).server.port, 9090)
        for port in (0, -1, 65536):
            with self.subTest(port=port), self.assertRaises(ConfigError):
                _override_server(config, None, port)
        with self.assertRaises(ConfigError):
            _override_server(config, "", None)


if __name__ == "__main__":
    unittest.main()
