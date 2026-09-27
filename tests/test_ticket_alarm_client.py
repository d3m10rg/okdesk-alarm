from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ticket_alarm_client import (
    ClientConfig,
    ClientError,
    MonitorState,
    OkdeskClient,
    RemoteSnapshot,
    evaluate_snapshot,
    load_config,
    read_state,
    write_state,
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def client_config() -> ClientConfig:
    return ClientConfig(
        okdesk_url="https://sd.cpsupport.ru",
        login="operator",
        password="secret",
        tracked_issue_ids=(42,),
        audio_file=Path("alarm.wav"),
        poll_interval_seconds=60,
        state_file=Path("alarm_state.json"),
        debug=False,
        request_timeout_seconds=15,
    )


class ConfigTests(unittest.TestCase):
    def test_loads_direct_okdesk_credentials_and_tracked_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "alarm.wav").write_bytes(b"RIFF")
            config_path = root / "alarm_client.json"
            config_path.write_text(
                json.dumps(
                    {
                        "login": "operator",
                        "password": "secret",
                        "tracked_issue_ids": [12, 13, 12],
                        "audio_file": "alarm.wav",
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.okdesk_url, "https://sd.cpsupport.ru")
            self.assertEqual(config.login, "operator")
            self.assertEqual(config.password, "secret")
            self.assertEqual(config.tracked_issue_ids, (12, 13))
            self.assertEqual(config.audio_file, root / "alarm.wav")

    def test_rejects_invalid_tracked_issue_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "alarm.wav").write_bytes(b"RIFF")
            config_path = root / "alarm_client.json"
            config_path.write_text(
                json.dumps(
                    {
                        "login": "operator",
                        "password": "secret",
                        "tracked_issue_ids": [0],
                        "audio_file": "alarm.wav",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ClientError):
                load_config(config_path)


class StateTests(unittest.TestCase):
    def test_reads_legacy_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "last_case_number.txt"
            state_path.write_text("16540\n", encoding="utf-8")

            self.assertEqual(read_state(state_path), MonitorState(latest_issue_id=16540))

    def test_round_trips_extended_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "alarm_state.json"
            expected = MonitorState(16542, {16540: 72, 16541: 8})

            write_state(state_path, expected)

            self.assertEqual(read_state(state_path), expected)


class EvaluationTests(unittest.TestCase):
    def test_first_comment_poll_sets_baseline_without_alert(self) -> None:
        result = evaluate_snapshot(
            MonitorState(latest_issue_id=20),
            RemoteSnapshot(latest_issue_id=20, comment_ids={10: (1, 2, 3)}),
            (10,),
        )

        self.assertEqual(result.alerts, ())
        self.assertEqual(result.baselined_issue_ids, (10,))
        self.assertEqual(result.state.comment_ids, {10: 3})

    def test_new_issue_and_comments_create_alerts(self) -> None:
        result = evaluate_snapshot(
            MonitorState(latest_issue_id=20, comment_ids={10: 2}),
            RemoteSnapshot(latest_issue_id=21, comment_ids={10: (1, 2, 4, 7)}),
            (10,),
        )

        self.assertEqual(
            result.alerts,
            ("New issue #21", "2 new comments on issue #10"),
        )
        self.assertEqual(result.state, MonitorState(21, {10: 7}))

    def test_deleted_items_do_not_move_state_backwards(self) -> None:
        result = evaluate_snapshot(
            MonitorState(latest_issue_id=20, comment_ids={10: 8}),
            RemoteSnapshot(latest_issue_id=19, comment_ids={10: (1, 2, 3)}),
            (10,),
        )

        self.assertEqual(result.alerts, ())
        self.assertEqual(result.state, MonitorState(20, {10: 8}))

    def test_failed_comment_request_does_not_create_empty_baseline(self) -> None:
        result = evaluate_snapshot(
            MonitorState(latest_issue_id=20),
            RemoteSnapshot(latest_issue_id=20, comment_ids={}),
            (10,),
        )

        self.assertEqual(result.alerts, ())
        self.assertEqual(result.baselined_issue_ids, ())
        self.assertEqual(result.state.comment_ids, {})


class OkdeskClientTests(unittest.TestCase):
    @patch("ticket_alarm_client.urlopen")
    def test_authenticates_and_requests_latest_issue_directly(self, mocked_urlopen: object) -> None:
        mocked_urlopen.side_effect = [  # type: ignore[attr-defined]
            FakeResponse({"api_key": "temporary-token"}),
            FakeResponse([{"id": 123}]),
        ]
        client = OkdeskClient(client_config())

        self.assertEqual(client.latest_issue_id(), 123)

        auth_request = mocked_urlopen.call_args_list[0].args[0]  # type: ignore[attr-defined]
        self.assertEqual(
            auth_request.full_url,
            "https://sd.cpsupport.ru/api/v1/users/sign_in",
        )
        self.assertEqual(
            json.loads(auth_request.data.decode("utf-8")),
            {"login": "operator", "password": "secret"},
        )
        list_request = mocked_urlopen.call_args_list[1].args[0]  # type: ignore[attr-defined]
        self.assertTrue(
            list_request.full_url.startswith(
                "https://sd.cpsupport.ru/api/v1/issues/list?"
            )
        )
        self.assertIn("api_token=temporary-token", list_request.full_url)

    @patch("ticket_alarm_client.urlopen")
    def test_requests_comments_for_selected_issue(self, mocked_urlopen: object) -> None:
        mocked_urlopen.side_effect = [  # type: ignore[attr-defined]
            FakeResponse({"api_key": "temporary-token"}),
            FakeResponse([{"id": 3}, {"id": 8}]),
        ]
        client = OkdeskClient(client_config())

        self.assertEqual(client.comment_ids(42), (3, 8))

        request = mocked_urlopen.call_args_list[1].args[0]  # type: ignore[attr-defined]
        self.assertEqual(
            request.full_url,
            "https://sd.cpsupport.ru/api/v1/issues/42/comments?api_token=temporary-token",
        )


if __name__ == "__main__":
    unittest.main()
