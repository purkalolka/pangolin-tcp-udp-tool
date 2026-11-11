import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pangolin_tool as pt

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class FileEditTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.pang = os.path.join(self.d, "pangolin")
        os.makedirs(os.path.join(self.pang, "config", "traefik"))
        shutil.copy(os.path.join(FIX, "docker-compose.yml"),
                    os.path.join(self.pang, "docker-compose.yml"))
        shutil.copy(os.path.join(FIX, "traefik_config.yml"),
                    os.path.join(self.pang, "config", "traefik", "traefik_config.yml"))
        # config.yml с флагом, как на реальном сервере 1.26
        with open(os.path.join(self.pang, "config", "config.yml"), "w") as f:
            f.write("flags:\n    allow_raw_resources: true\n")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_add_tcp_then_udp_idempotent(self):
        self.assertTrue(pt.add_gerbil_port(self.pang, 29378, "tcp"))
        self.assertFalse(pt.add_gerbil_port(self.pang, 29378, "tcp"))
        self.assertTrue(pt.add_gerbil_port(self.pang, 29378, "udp"))
        text = open(os.path.join(self.pang, "docker-compose.yml")).read()
        self.assertIn("- 29378:29378", text)
        self.assertIn("- 29378:29378/udp", text)
        self.assertEqual(len([l for l in text.splitlines() if "29378" in l]), 2)

    def test_add_traefik_entrypoints(self):
        self.assertTrue(pt.add_traefik_entrypoint(self.pang, 29378, "tcp"))
        self.assertFalse(pt.add_traefik_entrypoint(self.pang, 29378, "tcp"))
        self.assertTrue(pt.add_traefik_entrypoint(self.pang, 29378, "udp"))
        text = open(os.path.join(self.pang, "config", "traefik",
                                 "traefik_config.yml")).read()
        self.assertIn("  tcp-29378:\n    address: \":29378/tcp\"", text)
        self.assertIn("  udp-29378:\n    address: \":29378/udp\"", text)
        self.assertEqual(text.count("entryPoints:"), 1, "не плодить второй блок")

    def test_remove_roundtrip(self):
        pt.add_gerbil_port(self.pang, 25565, "tcp")
        pt.add_traefik_entrypoint(self.pang, 25565, "tcp")
        self.assertTrue(pt.remove_gerbil_port(self.pang, 25565, "tcp"))
        self.assertTrue(pt.remove_traefik_entrypoint(self.pang, 25565, "tcp"))
        self.assertFalse(pt.remove_gerbil_port(self.pang, 25565, "tcp"))
        self.assertNotIn((25565, "tcp"), pt.os_ports(self.pang))

    def test_os_ports_after_add(self):
        pt.add_gerbil_port(self.pang, 25565, "tcp")
        pt.add_traefik_entrypoint(self.pang, 25565, "tcp")
        self.assertIn((25565, "tcp"), pt.os_ports(self.pang))

    def test_ensure_raw_flag_present_is_noop(self):
        self.assertFalse(pt.ensure_raw_flag(self.pang))

    def test_ensure_raw_flag_adds(self):
        with open(os.path.join(self.pang, "config", "config.yml"), "w") as f:
            f.write("flags:\n    disable_signup_without_invite: true\n")
        self.assertTrue(pt.ensure_raw_flag(self.pang))
        text = open(os.path.join(self.pang, "config", "config.yml")).read()
        self.assertIn("allow_raw_resources: true", text)


class ValidationTests(unittest.TestCase):
    def test_reserved_ports(self):
        for p in (22, 80, 443, 51820, 3003):
            with self.assertRaises(pt.ToolError):
                pt.validate_rule("x", "tcp", p, "127.0.0.1", 3389)

    def test_bad_name_and_proto(self):
        with self.assertRaises(pt.ToolError):
            pt.validate_rule("Bad Name!", "tcp", 29378, "127.0.0.1", 3389)
        with self.assertRaises(pt.ToolError):
            pt.validate_rule("ok", "sctp", 29378, "127.0.0.1", 3389)

    def test_ok(self):
        pt.validate_rule("rdp-pc2", "udp", 29379, "127.0.0.1", 3389)


class ApiPayloadTests(unittest.TestCase):
    def test_create_resource_uses_mode_first(self):
        api = pt.PangolinApi("http://127.0.0.1:3003/v1", "k", "emae")
        calls = []

        def fake_req(method, path, body=None):
            calls.append((method, path, body))
            return {"resourceId": 42}

        with mock.patch.object(api, "_req", side_effect=fake_req):
            r = api.create_resource("rdp-pc1", "tcp", 29378)
        self.assertEqual(r, {"resourceId": 42})
        self.assertEqual(calls[0][2],
                         {"name": "rdp-pc1", "mode": "tcp", "proxyPort": 29378})

    def test_create_resource_falls_back_to_legacy(self):
        api = pt.PangolinApi("http://127.0.0.1:3003/v1", "k", "emae")
        calls = []

        def fake_req(method, path, body=None):
            calls.append(body)
            if len(calls) == 1:
                raise pt.ToolError("Pangolin API PUT: HTTP 400: bad mode")
            return {"resourceId": 43}

        with mock.patch.object(api, "_req", side_effect=fake_req):
            api.create_resource("rdp-pc1", "udp", 29378)
        self.assertEqual(calls[1],
                         {"name": "rdp-pc1", "http": False,
                          "protocol": "udp", "proxyPort": 29378})

    def test_create_site_minimal_body(self):
        api = pt.PangolinApi("http://127.0.0.1:3003/v1", "k", "emae")
        defaults = {"exitNodeId": 1, "subnet": "100.89.128.4/30",
                    "newtId": "abc", "newtSecret": "s3cr3t",
                    "endpoint": "tun.emae.su"}
        calls = []

        def fake_req(method, path, body=None):
            calls.append((method, path, body))
            if "pick-site-defaults" in path:
                return defaults
            return {"siteId": 7, "name": "node-1"}

        with mock.patch.object(api, "_req", side_effect=fake_req):
            site, d = api.create_site("node-1")
        self.assertEqual(site["siteId"], 7)
        # лишние pubKey/address дают 400 — тело только минимальное
        self.assertEqual(calls[1][2],
                         {"name": "node-1", "type": "newt", "exitNodeId": 1,
                          "subnet": "100.89.128.4/30", "newtId": "abc",
                          "secret": "s3cr3t"})


if __name__ == "__main__":
    unittest.main()
