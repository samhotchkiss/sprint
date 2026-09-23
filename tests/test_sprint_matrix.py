#!/usr/bin/env python3

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
import types
import unittest


HERE = os.path.dirname(os.path.realpath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "bin", "sprint-matrix")
LOADER = importlib.machinery.SourceFileLoader("sprint_matrix", SCRIPT)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load sprint-matrix")
sprint_matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sprint_matrix)


class FakeMatrix:
    def __init__(self):
        self.created = []
        self.invited = []
        self.sent = []
        self.sync_results = []
        self.memberships = {}

    def whoami(self):
        return {"user_id": "@callbot:hotch.org"}

    def create_room(self, name, topic, invite_user):
        self.created.append((name, topic, invite_user))
        return "!sprint:hotch.org"

    def invite(self, room_id, user_id):
        self.invited.append((room_id, user_id))

    def membership(self, room_id, user_id):
        return self.memberships.get((room_id, user_id))

    def send(self, room_id, txn_id, text, notice=True):
        self.sent.append((room_id, txn_id, text, notice))
        return {"event_id": "$sent-%d" % len(self.sent)}

    def sync(self, room_id, since=None, timeout_ms=1000):
        if self.sync_results:
            return self.sync_results.pop(0)
        return {"next_batch": since or "s0", "rooms": {"join": {room_id: {
            "timeline": {"events": []},
        }}}}


class FakeBoard:
    def __init__(self, events=None):
        self.inbound = []
        self.event_rows = list(events or [])

    def settings(self):
        return {"name": "Matrix integration", "agent_name": "Nell"}

    def matrix_inbound(self, event_id, text, sender):
        self.inbound.append((event_id, text, sender))
        return {"duplicate": False, "event": {"seq": 41}}

    def events(self, after, limit=200):
        rows = [event for event in self.event_rows if event["seq"] > after][:limit]
        head = max([event["seq"] for event in self.event_rows] or [0])
        return {"events": rows, "head": head}


class SprintMatrixBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_env = dict(os.environ)
        os.environ.update({
            "MATRIX_ALLOWED_USER_ID": "@sam:hotch.org",
            "MATRIX_USER_ID": "@callbot:hotch.org",
            "MATRIX_ACCESS_TOKEN": "test-token",
        })
        self.addCleanup(self.restore_env)
        self.root = os.path.join(self.tmp.name, "project")
        os.makedirs(self.root)
        self.paths = sprint_matrix.paths_for(self.root)
        self.saved = []

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    def bridge(self, matrix=None, board=None, state=None):
        return sprint_matrix.SprintMatrixBridge(
            self.root,
            self.paths,
            matrix=matrix or FakeMatrix(),
            board=board or FakeBoard(),
            state=state or {},
            persist=lambda value: self.saved.append(dict(value)),
            log=lambda _record: None,
        )

    def test_first_start_creates_one_private_room_and_announces_it(self):
        matrix = FakeMatrix()
        bridge = self.bridge(matrix=matrix)
        room_id = bridge.ensure_room()
        self.assertEqual(room_id, "!sprint:hotch.org")
        self.assertEqual(len(matrix.created), 1)
        name, topic, invited = matrix.created[0]
        self.assertEqual(name, "Sprint — Matrix integration")
        self.assertIn("Nell", topic)
        self.assertEqual(invited, "@sam:hotch.org")
        self.assertEqual(len(matrix.sent), 1)
        self.assertEqual(matrix.sent[0][1], "sprint-%s-room-ready" % bridge.project_key)
        self.assertEqual(bridge.state["room_url"],
                         "https://matrix.to/#/%21sprint%3Ahotch.org")

    def test_existing_room_is_reused_and_sam_is_invited(self):
        matrix = FakeMatrix()
        state = {
            "room_id": "!existing:hotch.org",
            "room_name": "Sprint — Existing",
            "announced_room_id": "!existing:hotch.org",
        }
        bridge = self.bridge(matrix=matrix, state=state)
        self.assertEqual(bridge.ensure_room(), "!existing:hotch.org")
        self.assertEqual(matrix.created, [])
        self.assertEqual(matrix.invited,
                         [("!existing:hotch.org", "@sam:hotch.org")])
        self.assertEqual(matrix.sent, [])

    def test_existing_joined_user_is_not_reinvited(self):
        matrix = FakeMatrix()
        matrix.memberships[("!existing:hotch.org", "@sam:hotch.org")] = "join"
        bridge = self.bridge(matrix=matrix, state={
            "room_id": "!existing:hotch.org",
            "announced_room_id": "!existing:hotch.org",
        })
        self.assertEqual(bridge.ensure_room(), "!existing:hotch.org")
        self.assertEqual(matrix.invited, [])

    def test_matrix_text_becomes_one_board_sidebar_delivery(self):
        board = FakeBoard()
        bridge = self.bridge(board=board)
        accepted = bridge.ingest_matrix_event({
            "type": "m.room.message",
            "event_id": "$matrix-1",
            "sender": "@sam:hotch.org",
            "content": {"msgtype": "m.text", "body": "hello from my phone"},
        })
        self.assertTrue(accepted)
        self.assertEqual(board.inbound,
                         [("$matrix-1", "hello from my phone", "@sam:hotch.org")])

    def test_other_senders_and_voice_messages_do_not_enter_text_chat(self):
        board = FakeBoard()
        bridge = self.bridge(board=board)
        for event in (
            {"type": "m.room.message", "event_id": "$bot",
             "sender": "@callbot:hotch.org",
             "content": {"msgtype": "m.text", "body": "echo"}},
            {"type": "m.room.message", "event_id": "$voice",
             "sender": "@sam:hotch.org",
             "content": {"msgtype": "m.audio", "body": "voice.ogg"}},
        ):
            self.assertFalse(bridge.ingest_matrix_event(event))
        self.assertEqual(board.inbound, [])

    def test_initial_sync_starts_at_now_without_replaying_room_history(self):
        matrix = FakeMatrix()
        matrix.sync_results.append({
            "next_batch": "s1",
            "rooms": {"join": {"!sprint:hotch.org": {"timeline": {"events": [{
                "type": "m.room.message", "event_id": "$old",
                "sender": "@sam:hotch.org",
                "content": {"msgtype": "m.text", "body": "old history"},
            }]}}}},
        })
        board = FakeBoard()
        bridge = self.bridge(matrix=matrix, board=board)
        self.assertEqual(bridge.sync_matrix_once("!sprint:hotch.org"), 0)
        self.assertEqual(board.inbound, [])
        self.assertEqual(bridge.state["sync_token"], "s1")

    def test_board_and_session_chat_mirror_but_matrix_origin_does_not_echo(self):
        events = [
            {"seq": 10, "card_num": None, "actor": "user", "kind": "chat",
             "payload": {"text": "from the browser", "reply_to": "sidebar"}},
            {"seq": 11, "card_num": None, "actor": "user", "kind": "chat",
             "payload": {"text": "from Matrix", "reply_to": "sidebar",
                         "source_surface": "matrix", "source_event_id": "$m"}},
            {"seq": 12, "card_num": 7, "actor": "session", "kind": "note",
             "payload": {"text": "card-only status"}},
            {"seq": 13, "card_num": None, "actor": "session", "kind": "chat",
             "payload": {"text": "same agent reply"}},
        ]
        matrix = FakeMatrix()
        board = FakeBoard(events)
        bridge = self.bridge(matrix=matrix, board=board, state={"board_cursor": 9})
        mirrored = bridge.drain_board_once("!sprint:hotch.org")
        self.assertEqual(mirrored, 2)
        self.assertEqual([entry[2] for entry in matrix.sent], [
            "From the Sprint board:\nfrom the browser",
            "same agent reply",
        ])
        self.assertEqual([entry[1] for entry in matrix.sent], [
            bridge.board_txn_id(10), bridge.board_txn_id(13),
        ])
        self.assertEqual(bridge.state["board_cursor"], 13)

    def test_board_transaction_id_is_stable_for_retry(self):
        bridge = self.bridge()
        self.assertEqual(bridge.board_txn_id(42), bridge.board_txn_id(42))
        self.assertNotEqual(bridge.board_txn_id(42), bridge.board_txn_id(43))

    def test_switching_to_an_explicit_room_resets_both_cursors(self):
        matrix = FakeMatrix()
        state = {
            "room_id": "!old:hotch.org",
            "sync_token": "old-sync",
            "board_cursor": 99,
            "announced_room_id": "!old:hotch.org",
        }
        bridge = sprint_matrix.SprintMatrixBridge(
            self.root, self.paths, room_id="!new:hotch.org", matrix=matrix,
            board=FakeBoard(), state=state,
            persist=lambda value: self.saved.append(dict(value)),
            log=lambda _record: None,
        )
        self.assertEqual(bridge.ensure_room(), "!new:hotch.org")
        self.assertIsNone(bridge.state["sync_token"])
        self.assertIsNone(bridge.state["board_cursor"])
        self.assertEqual(bridge.state["announced_room_id"], "!new:hotch.org")

    def test_local_env_install_filters_secrets_and_is_mode_0600(self):
        env_path = os.path.join(self.tmp.name, "config", "matrix.env")
        sprint_matrix.install_env_text(
            "MATRIX_HOMESERVER=https://matrix.hotch.org\n"
            "MATRIX_ACCESS_TOKEN=secret-token\n"
            "MATRIX_USER_ID=@callbot:hotch.org\n"
            "MATRIX_ALLOWED_USER_ID=@sam:hotch.org\n"
            "MATRIX_PASSWORD=do-not-copy\n"
            "MATRIX_ROOM_ID=!old:hotch.org\n",
            env_path,
        )
        with open(env_path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('MATRIX_ACCESS_TOKEN="secret-token"', text)
        self.assertNotIn("MATRIX_PASSWORD", text)
        self.assertNotIn("MATRIX_ROOM_ID", text)
        self.assertEqual(os.stat(env_path).st_mode & 0o777, 0o600)

        for name in sprint_matrix.MATRIX_ENV_NAMES:
            os.environ.pop(name, None)
        self.assertTrue(sprint_matrix.load_env_file(env_path))
        self.assertEqual(os.environ["MATRIX_ACCESS_TOKEN"], "secret-token")
        self.assertEqual(os.environ["MATRIX_ALLOWED_USER_ID"], "@sam:hotch.org")

    def test_voice_environment_is_bound_to_the_sprint_room_and_workspace(self):
        environment = sprint_matrix.voice_environment(
            self.root,
            self.paths,
            "!sprint:hotch.org",
            {"base": "http://127.0.0.1:8399", "token": "board-token"},
        )
        self.assertEqual(environment["MATRIX_ROOM_ID"], "!sprint:hotch.org")
        self.assertEqual(environment["CODEX_SOURCE_CWD"], self.root)
        self.assertEqual(environment["MATRIX_VOICE_RUNTIME_DIR"],
                         self.paths["voice_runtime"])
        self.assertEqual(environment["MATRIX_VOICE_READY_FILE"],
                         self.paths["voice_ready"])
        self.assertEqual(environment["SPRINT_SERVER"], "http://127.0.0.1:8399")
        self.assertEqual(environment["SPRINT_TOKEN"], "board-token")

    def test_voice_status_is_isolated_from_text_bridge_state(self):
        sprint_matrix.write_json_atomic(self.paths["state"], {
            "pid": 123,
            "room_id": "!sprint:hotch.org",
        })
        sprint_matrix.write_json_atomic(self.paths["voice_state"], {
            "pid": 456,
            "room_id": "!sprint:hotch.org",
            "last_error": "listener stopped",
        })
        status = sprint_matrix.public_voice_status(self.root, self.paths)
        self.assertFalse(status["running"])
        self.assertFalse(status["ready"])
        self.assertIsNone(status["pid"])
        self.assertEqual(status["last_error"], "listener stopped")

    def test_cli_exposes_separate_voice_lifecycle(self):
        choices = sprint_matrix.parser()._subparsers._group_actions[0].choices
        self.assertIn("voice-start", choices)
        self.assertIn("voice-status", choices)
        self.assertIn("voice-stop", choices)

    def test_voice_supervisor_binds_runner_to_exact_session_and_room(self):
        subprocess.run(["git", "init", "-q", self.root], check=True)
        os.makedirs(self.paths["data_dir"], exist_ok=True)
        sprint_matrix.write_json_atomic(self.paths["state"], {
            "room_id": "!sprint:hotch.org",
        })
        sprint_matrix.write_json_atomic(self.paths["server"], {
            "port": 8399,
            "token": "board-token",
        })
        observed = os.path.join(self.tmp.name, "voice-observed.json")
        runner = os.path.join(self.tmp.name, "fake-voice-runner")
        with open(runner, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['VOICE_TEST_OUTPUT'], 'w', encoding='utf-8') as out:\n"
                "    json.dump({'args': sys.argv[1:], "
                "'room': os.environ['MATRIX_ROOM_ID'], "
                "'cwd': os.environ['CODEX_SOURCE_CWD'], "
                "'server': os.environ['SPRINT_SERVER']}, out)\n"
            )
        os.chmod(runner, 0o700)
        os.environ["VOICE_TEST_OUTPUT"] = observed

        result = sprint_matrix.cmd_voice_run(types.SimpleNamespace(
            project_root=self.root,
            data_dir=None,
            runner=runner,
            tmux_target="%77",
            codex_session_id="thread-123",
        ))
        self.assertEqual(result, 0)
        with open(observed, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["args"], ["%77", "thread-123"])
        self.assertEqual(payload["room"], "!sprint:hotch.org")
        self.assertEqual(payload["cwd"], os.path.realpath(self.root))
        self.assertEqual(payload["server"], "http://127.0.0.1:8399")
        voice_state = sprint_matrix.read_json(self.paths["voice_state"])
        self.assertIsNone(voice_state["pid"])
        self.assertEqual(voice_state["last_exit_code"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
