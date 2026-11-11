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


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.pang = os.path.join(self.d, "pangolin")
        os.makedirs(os.path.join(self.pang, "config", "traefik"))
        shutil.copy(os.path.join(FIX, "docker-compose.yml"),
                    os.path.join(self.pang, "docker-compose.yml"))
        shutil.copy(os.path.join(FIX, "traefik_config.yml"),
                    os.path.join(self.pang, "config", "traefik", "traefik_config.yml"))
        with open(os.path.join(self.pang, "config", "config.yml"), "w") as f:
            f.write("flags:\n    allow_raw_resources: true\n")

    def tearDown(self):
        shutil.rmtree(self.d)

    def _cfg_with_rule(self):
        state = os.path.join(self.d, "rules.json")
        rule = {"name": "rdp-pc1", "protocol": "tcp", "public_port": 29378,
                "site_id": 1, "site_name": "wsl", "target_ip": "127.0.0.1",
                "target_port": 3389, "resource_id": 3}
        with open(state, "w") as f:
            json.dump([rule], f)
        # порт уже прописан в фикстурах, как после add
        pt.add_gerbil_port(self.pang, 29378, "tcp")
        pt.add_traefik_entrypoint(self.pang, 29378, "tcp")
        return {"api_base": "http://127.0.0.1:3003/v1", "api_key": "k",
                "org_id": "emae", "state_file": state,
                "pangolin_dir": self.pang, "ui_token": "t"}

    def test_delete_no_restart_skips_restart(self):
        # порт правила заведён в фикстурах (как после range) — удаление
        # не трогает файлы и не рестартит вообще
        cfg = self._cfg_with_rule()
        api = mock.Mock()
        with mock.patch.object(pt, "PangolinApi", return_value=api), \
             mock.patch.object(pt, "restart_stack") as rs, \
             mock.patch.object(pt, "verify_listener") as vl:
            res = pt.do_delete(cfg, name="rdp-pc1", no_restart=True)
        rs.assert_not_called()
        vl.assert_not_called()
        self.assertFalse(res["pending_restart"])
        self.assertEqual(pt.load_state(cfg["state_file"]), [])
        self.assertTrue(pt.port_provisioned(self.pang, 29378, "tcp"))

    def test_delete_no_restart_unprovisioned_pending(self):
        cfg = self._cfg_with_rule()
        state = json.load(open(cfg["state_file"]))
        state[0]["public_port"] = 29379
        with open(cfg["state_file"], "w") as f:
            json.dump(state, f)
        api = mock.Mock()
        with mock.patch.object(pt, "PangolinApi", return_value=api), \
             mock.patch.object(pt, "restart_stack") as rs:
            res = pt.do_delete(cfg, name="rdp-pc1", no_restart=True)
        rs.assert_not_called()
        self.assertTrue(res["pending_restart"])

    def test_apply_restarts_once_and_verifies(self):
        cfg = self._cfg_with_rule()
        with mock.patch.object(pt, "restart_stack") as rs, \
             mock.patch.object(pt, "verify_listener") as vl:
            res = pt.do_apply(cfg)
        rs.assert_called_once()
        vl.assert_called_once_with(29378, "tcp")
        self.assertTrue(res["all_ok"])

    def test_host_port_in_use(self):
        import socket as _s
        srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(pt.host_port_in_use(port, "tcp"))
            self.assertFalse(pt.host_port_in_use(port, "udp"))
        finally:
            srv.close()
        self.assertFalse(pt.host_port_in_use(port, "tcp"))

    def test_range_refuses_busy_port(self):
        import socket as _s
        srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        cfg_path = os.path.join(self.d, "config.json")
        with open(cfg_path, "w") as f:
            json.dump({"org_id": "emae", "api_key": "k"}, f)
        cfg = {"api_base": "http://127.0.0.1:3003/v1", "api_key": "k",
               "org_id": "emae", "state_file": os.path.join(self.d, "r.json"),
               "pangolin_dir": self.pang, "ui_token": "t",
               "config_path": cfg_path}
        try:
            with mock.patch.object(pt, "restart_stack") as rs:
                with self.assertRaises(pt.ToolError):
                    pt.do_range(cfg, port, port)
                rs.assert_not_called()
            self.assertFalse(pt.port_provisioned(self.pang, port, "tcp"))
        finally:
            srv.close()

    def test_port_provisioned_needs_both_sides(self):
        self.assertFalse(pt.port_provisioned(self.pang, 29400, "tcp"))
        pt.add_gerbil_port(self.pang, 29400, "tcp")
        # только Gerbil, без entrypoint — ещё не готов
        self.assertFalse(pt.port_provisioned(self.pang, 29400, "tcp"))
        pt.add_traefik_entrypoint(self.pang, 29400, "tcp")
        self.assertTrue(pt.port_provisioned(self.pang, 29400, "tcp"))
        self.assertFalse(pt.port_provisioned(self.pang, 29400, "udp"))
        self.assertFalse(pt.port_provisioned(self.pang, 29401, "tcp"))

    def test_range_writes_ports_and_config_once(self):
        cfg_path = os.path.join(self.d, "config.json")
        with open(cfg_path, "w") as f:
            json.dump({"org_id": "emae", "api_key": "k"}, f)
        cfg = {"api_base": "http://127.0.0.1:3003/v1", "api_key": "k",
               "org_id": "emae", "state_file": os.path.join(self.d, "r.json"),
               "pangolin_dir": self.pang, "ui_token": "t",
               "config_path": cfg_path}
        with mock.patch.object(pt, "restart_stack") as rs, \
             mock.patch.object(pt, "verify_listener"):
            res = pt.do_range(cfg, 29500, 29501)
        rs.assert_called_once()
        self.assertEqual(res["range"], [29500, 29501])
        self.assertTrue(pt.port_provisioned(self.pang, 29500, "tcp"))
        self.assertTrue(pt.port_provisioned(self.pang, 29501, "udp"))
        with open(cfg_path) as f:
            self.assertEqual(json.load(f)["port_ranges"], [[29500, 29501]])

    def test_add_in_range_skips_restart(self):
        cfg = self._cfg_with_rule()
        cfg["state_file"] = os.path.join(self.d, "empty.json")
        pt.add_gerbil_port(self.pang, 29400, "tcp")
        pt.add_traefik_entrypoint(self.pang, 29400, "tcp")
        api = mock.Mock()
        api.list_sites.return_value = [{"siteId": 1, "name": "wsl"}]
        api.create_resource.return_value = {"resourceId": 9}
        api.create_target.return_value = {}
        with mock.patch.object(pt, "PangolinApi", return_value=api), \
             mock.patch.object(pt, "restart_stack") as rs, \
             mock.patch.object(pt, "verify_listener") as vl:
            res = pt.do_add(cfg, "range-echo", "tcp", 29400, "wsl",
                            "127.0.0.1", 29401)
        rs.assert_not_called()
        vl.assert_called_once_with(29400, "tcp")
        self.assertFalse(res["pending_restart"])


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

    def test_site_create_returns_connect_command(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        cfg = {"api_base": "http://127.0.0.1:3003/v1", "api_key": "k",
               "org_id": "emae", "state_file": os.path.join(tmp, "r.json"),
               "pangolin_dir": tmp, "ui_token": "t"}
        api = mock.Mock()
        api.create_site.return_value = (
            {"siteId": 9, "name": "node-1", "subnet": "100.89.128.4/30"},
            {"endpoint": "tun.emae.su", "newtId": "abc",
             "newtSecret": "s3cr3t", "subnet": "100.89.128.4/30"})
        with mock.patch.object(pt, "PangolinApi", return_value=api):
            res = pt.do_site_create(cfg, "node-1")
        cmd = res["connect_command"]
        self.assertIn("docker run -d --name newt --network host", cmd)
        self.assertIn("--restart unless-stopped", cmd)
        self.assertIn("-e PANGOLIN_ENDPOINT=https://tun.emae.su", cmd)
        self.assertIn("-e NEWT_ID=abc", cmd)
        self.assertIn("-e NEWT_SECRET=s3cr3t", cmd)
        self.assertTrue(cmd.endswith("fosrl/newt:latest"))
        # .env остаётся как запасной вариант
        self.assertTrue(os.path.exists(res["credentials_file"]))


if __name__ == "__main__":
    unittest.main()
