#!/usr/bin/env python3
"""Windows alarm for new Okdesk issues and comments on selected issues."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_OKDESK_URL = "https://sd.cpsupport.ru"
DEFAULT_POLL_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 15

if sys.platform == "win32":
    import winsound


class ClientError(RuntimeError):
    """An error that can safely be displayed to the client operator."""


class AuthenticationError(ClientError):
    """Okdesk rejected the configured account or its API key."""


class ResponseError(ClientError):
    def __init__(self, operation: str, status_code: int) -> None:
        self.operation = operation
        self.status_code = status_code
        super().__init__(f"Okdesk {operation} returned HTTP {status_code}.")


@dataclass(frozen=True)
class ClientConfig:
    okdesk_url: str
    login: str
    password: str
    tracked_issue_ids: tuple[int, ...]
    audio_file: Path
    poll_interval_seconds: int
    state_file: Path
    debug: bool
    request_timeout_seconds: int


@dataclass(frozen=True)
class MonitorState:
    latest_issue_id: int | None = None
    comment_ids: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RemoteSnapshot:
    latest_issue_id: int | None
    comment_ids: dict[int, tuple[int, ...]]


@dataclass(frozen=True)
class Evaluation:
    state: MonitorState
    alerts: tuple[str, ...]
    baselined_issue_ids: tuple[int, ...]


def log(message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", file=stream)


def require_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ClientError(f'"{key}" must be a non-empty string.')
    return value.strip()


def _positive_integer(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ClientError(f'"{key}" must be a positive integer.')
    return value


def _okdesk_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ClientError('"okdesk_url" must be an HTTPS origin without a path.')
    return value.rstrip("/")


def _tracked_issue_ids(raw: dict[str, Any]) -> tuple[int, ...]:
    value = raw.get("tracked_issue_ids", [])
    if not isinstance(value, list):
        raise ClientError('"tracked_issue_ids" must be a JSON array of positive integers.')

    result: list[int] = []
    seen: set[int] = set()
    for issue_id in value:
        if isinstance(issue_id, bool) or not isinstance(issue_id, int) or issue_id < 1:
            raise ClientError(
                '"tracked_issue_ids" must contain only positive integers.'
            )
        if issue_id not in seen:
            result.append(issue_id)
            seen.add(issue_id)
    return tuple(result)


def load_config(config_path: Path) -> ClientConfig:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ClientError(
            f"Configuration file not found: {config_path}. "
            "Copy alarm_client.example.json to alarm_client.json and fill it in."
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise ClientError(f"Cannot read configuration file: {error}") from error

    if not isinstance(raw, dict):
        raise ClientError("Configuration root must be a JSON object.")

    audio_file_value = require_string(raw, "audio_file")
    state_file_value = raw.get("state_file", "alarm_state.json")
    debug = raw.get("debug", False)

    if not isinstance(state_file_value, str) or not state_file_value.strip():
        raise ClientError('"state_file" must be a non-empty path.')
    if not isinstance(debug, bool):
        raise ClientError('"debug" must be true or false.')

    audio_file = Path(audio_file_value)
    state_file = Path(state_file_value)
    if not audio_file.is_absolute():
        audio_file = config_path.parent / audio_file
    if not state_file.is_absolute():
        state_file = config_path.parent / state_file
    if not audio_file.is_file():
        raise ClientError(f"Audio file not found: {audio_file}")

    okdesk_url = raw.get("okdesk_url", DEFAULT_OKDESK_URL)
    if not isinstance(okdesk_url, str) or not okdesk_url.strip():
        raise ClientError('"okdesk_url" must be a non-empty string.')

    return ClientConfig(
        okdesk_url=_okdesk_origin(okdesk_url.strip()),
        login=require_string(raw, "login"),
        password=require_string(raw, "password"),
        tracked_issue_ids=_tracked_issue_ids(raw),
        audio_file=audio_file,
        poll_interval_seconds=_positive_integer(
            raw, "poll_interval_seconds", DEFAULT_POLL_SECONDS
        ),
        state_file=state_file,
        debug=debug,
        request_timeout_seconds=_positive_integer(
            raw, "request_timeout_seconds", DEFAULT_TIMEOUT_SECONDS
        ),
    )


def _state_integer(value: Any, label: str, *, allow_none: bool = False) -> int | None:
    if allow_none and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClientError(f"State field {label} must be a non-negative integer.")
    return value


def read_state(state_file: Path) -> MonitorState:
    try:
        value = state_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return MonitorState()
    except OSError as error:
        raise ClientError(f"Cannot read state file {state_file}: {error}") from error

    if value.isdecimal():
        # Backward compatibility with the original one-line state file.
        return MonitorState(latest_issue_id=int(value))

    try:
        raw = json.loads(value)
    except json.JSONDecodeError as error:
        raise ClientError(
            f"State file {state_file} contains neither a legacy number nor valid JSON."
        ) from error
    if not isinstance(raw, dict):
        raise ClientError(f"State file {state_file} must contain a JSON object.")

    latest_issue_id = _state_integer(
        raw.get("latest_issue_id"), "latest_issue_id", allow_none=True
    )
    raw_comments = raw.get("comment_ids", {})
    if not isinstance(raw_comments, dict):
        raise ClientError("State field comment_ids must be a JSON object.")

    comment_ids: dict[int, int] = {}
    for issue_value, comment_value in raw_comments.items():
        try:
            issue_id = int(issue_value)
        except (TypeError, ValueError) as error:
            raise ClientError("State contains an invalid tracked issue number.") from error
        if issue_id < 1:
            raise ClientError("State contains an invalid tracked issue number.")
        parsed_comment = _state_integer(comment_value, f"comment_ids.{issue_id}")
        assert parsed_comment is not None
        comment_ids[issue_id] = parsed_comment
    return MonitorState(latest_issue_id=latest_issue_id, comment_ids=comment_ids)


def write_state(state_file: Path, state: MonitorState) -> None:
    payload = {
        "version": 1,
        "latest_issue_id": state.latest_issue_id,
        "comment_ids": {
            str(issue_id): comment_id
            for issue_id, comment_id in sorted(state.comment_ids.items())
        },
    }
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = state_file.with_name(f".{state_file.name}.{os.getpid()}.tmp")
        temporary_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary_file, state_file)
    except OSError as error:
        raise ClientError(f"Cannot save state file {state_file}: {error}") from error


def should_start_alarm(server_number: int, saved_number: int | None) -> bool:
    """A lower number is ignored because Okdesk requests can be deleted."""
    return saved_number is None or server_number > saved_number


def evaluate_snapshot(
    state: MonitorState,
    snapshot: RemoteSnapshot,
    tracked_issue_ids: tuple[int, ...],
) -> Evaluation:
    latest_issue_id = state.latest_issue_id
    comment_ids = dict(state.comment_ids)
    alerts: list[str] = []
    baselined: list[int] = []

    if snapshot.latest_issue_id is not None and should_start_alarm(
        snapshot.latest_issue_id, state.latest_issue_id
    ):
        latest_issue_id = snapshot.latest_issue_id
        alerts.append(f"New issue #{snapshot.latest_issue_id}")

    for issue_id in tracked_issue_ids:
        # A missing key means that this particular comment request failed. Do
        # not establish a false empty baseline or move its state backwards.
        if issue_id not in snapshot.comment_ids:
            continue
        remote_ids = snapshot.comment_ids[issue_id]
        current_id = max(remote_ids, default=0)
        saved_id = state.comment_ids.get(issue_id)
        if saved_id is None:
            comment_ids[issue_id] = current_id
            baselined.append(issue_id)
            continue
        if current_id > saved_id:
            new_count = sum(comment_id > saved_id for comment_id in remote_ids)
            comment_ids[issue_id] = current_id
            suffix = "comment" if new_count == 1 else "comments"
            alerts.append(f"{new_count} new {suffix} on issue #{issue_id}")

    return Evaluation(
        state=MonitorState(latest_issue_id=latest_issue_id, comment_ids=comment_ids),
        alerts=tuple(alerts),
        baselined_issue_ids=tuple(baselined),
    )


class OkdeskClient:
    """Small dependency-free client for the Okdesk REST API."""

    def __init__(self, config: ClientConfig) -> None:
        self.base_url = config.okdesk_url
        self.login = config.login
        self.password = config.password
        self.timeout = config.request_timeout_seconds
        self.api_token: str | None = None
        self._lock = RLock()

    def _decode_json(self, response: Any, operation: str) -> Any:
        try:
            return json.loads(response.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ClientError(f"Okdesk {operation} returned invalid JSON.") from error

    def authenticate(self) -> None:
        with self._lock:
            payload = json.dumps(
                {"login": self.login, "password": self.password}
            ).encode("utf-8")
            request = Request(
                f"{self.base_url}/api/v1/users/sign_in",
                data=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "okdesk-alarm/1.0",
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = self._decode_json(response, "authentication")
            except HTTPError as error:
                status_code = error.code
                error.close()
                if status_code in {401, 403, 422}:
                    raise AuthenticationError(
                        f"Okdesk rejected the configured login or password (HTTP {status_code})."
                    ) from error
                raise ResponseError("authentication", status_code) from error
            except URLError as error:
                raise ClientError("Cannot connect to Okdesk.") from error
            except TimeoutError as error:
                raise ClientError("Okdesk authentication timed out.") from error

            token = body.get("api_key") if isinstance(body, dict) else None
            if not isinstance(token, str) or not token:
                raise AuthenticationError("Okdesk did not return an API key.")
            self.api_token = token

    def ensure_authenticated(self) -> None:
        with self._lock:
            if self.api_token is None:
                self.authenticate()

    def _get_json(
        self,
        path: str,
        params: dict[str, Any],
        operation: str,
        *,
        allow_reauthentication: bool = True,
    ) -> Any:
        with self._lock:
            self.ensure_authenticated()
            safe_params = dict(params)
            safe_params["api_token"] = self.api_token
            request = Request(
                f"{self.base_url}{path}?{urlencode(safe_params)}",
                headers={"Accept": "application/json", "User-Agent": "okdesk-alarm/1.0"},
                method="GET",
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return self._decode_json(response, operation)
            except HTTPError as error:
                status_code = error.code
                error.close()
                if status_code == 401 and allow_reauthentication:
                    self.api_token = None
                    self.authenticate()
                    return self._get_json(
                        path,
                        params,
                        operation,
                        allow_reauthentication=False,
                    )
                if status_code == 401:
                    self.api_token = None
                    raise AuthenticationError("Okdesk rejected the API key.") from error
                raise ResponseError(operation, status_code) from error
            except URLError as error:
                # Do not include the exception text: the request URL contains api_token.
                raise ClientError(f"Okdesk {operation} request failed.") from error
            except TimeoutError as error:
                raise ClientError(f"Okdesk {operation} request timed out.") from error

    @staticmethod
    def _item_id(item: Any, operation: str) -> int:
        if not isinstance(item, dict):
            raise ClientError(f"Okdesk {operation} response has an unexpected format.")
        value = item.get("id")
        if isinstance(value, bool):
            raise ClientError(f"Okdesk {operation} response contains an invalid ID.")
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise ClientError(
                f"Okdesk {operation} response contains an invalid ID."
            ) from error
        if result < 1:
            raise ClientError(f"Okdesk {operation} response contains an invalid ID.")
        return result

    def latest_issue_id(self) -> int | None:
        body = self._get_json(
            "/api/v1/issues/list",
            {
                "page[number]": 1,
                "page[size]": 1,
                "sorting[field]": "created_at",
                "sorting[direction]": "reverse",
            },
            "issue list",
        )
        if not isinstance(body, list):
            raise ClientError("Okdesk issue list has an unexpected format.")
        if not body:
            return None
        return self._item_id(body[0], "issue list")

    def comment_ids(self, issue_id: int) -> tuple[int, ...]:
        body = self._get_json(
            f"/api/v1/issues/{issue_id}/comments", {}, f"comments for issue #{issue_id}"
        )
        if not isinstance(body, list):
            raise ClientError(
                f"Okdesk comments for issue #{issue_id} have an unexpected format."
            )
        return tuple(self._item_id(item, "comment list") for item in body)

    def snapshot(self, tracked_issue_ids: tuple[int, ...]) -> RemoteSnapshot:
        self.ensure_authenticated()
        return RemoteSnapshot(
            latest_issue_id=self.latest_issue_id(),
            comment_ids={
                issue_id: self.comment_ids(issue_id) for issue_id in tracked_issue_ids
            },
        )


def fetch_snapshot(
    client: OkdeskClient, tracked_issue_ids: tuple[int, ...]
) -> tuple[RemoteSnapshot, tuple[ClientError, ...]]:
    """Fetch independent monitor inputs without letting one unavailable issue block all."""
    client.ensure_authenticated()
    errors: list[ClientError] = []
    try:
        latest_issue_id = client.latest_issue_id()
    except ClientError as error:
        latest_issue_id = None
        errors.append(error)

    comment_ids: dict[int, tuple[int, ...]] = {}
    for issue_id in tracked_issue_ids:
        try:
            comment_ids[issue_id] = client.comment_ids(issue_id)
        except ClientError as error:
            errors.append(error)

    return RemoteSnapshot(latest_issue_id, comment_ids), tuple(errors)


class AlarmController:
    """Loop WAV with winsound or MP3 with the built-in Windows Media Player."""

    _WMP_LOOP_SCRIPT = (
        "$player = New-Object -ComObject WMPlayer.OCX; "
        "$media = $player.newMedia($env:TICKET_ALARM_AUDIO_FILE); "
        "$playlist = $player.newPlaylist('ticket-alarm', ''); "
        "$playlist.appendItem($media); "
        "$player.currentPlaylist = $playlist; "
        "$player.settings.setMode('loop', $true); "
        "$player.controls.play(); "
        "while ($true) { Start-Sleep -Seconds 1 }"
    )

    def __init__(self, audio_file: Path) -> None:
        self._audio_file = audio_file
        self._lock = Lock()
        self._wmp_process: subprocess.Popen[bytes] | None = None
        self._playing = False

    def start(self) -> bool:
        with self._lock:
            if self._playing:
                return False
            if sys.platform != "win32":
                raise ClientError("The dependency-free alarm player is supported only on Windows.")
            try:
                if self._audio_file.suffix.lower() == ".wav":
                    winsound.PlaySound(
                        str(self._audio_file),
                        winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP,
                    )
                else:
                    self._wmp_process = subprocess.Popen(
                        [
                            "powershell.exe",
                            "-NoLogo",
                            "-NoProfile",
                            "-NonInteractive",
                            "-Command",
                            self._WMP_LOOP_SCRIPT,
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env={
                            **os.environ,
                            "TICKET_ALARM_AUDIO_FILE": str(self._audio_file),
                        },
                    )
                    time.sleep(0.2)
                    if self._wmp_process.poll() is not None:
                        raise ClientError(
                            "Cannot start MP3 playback. Enable Windows Media Player or use a WAV file."
                        )
            except OSError as error:
                raise ClientError(f"Cannot start audio playback: {error}") from error
            self._playing = True
            return True

    def stop(self) -> bool:
        with self._lock:
            if not self._playing:
                return False
            if self._audio_file.suffix.lower() == ".wav":
                winsound.PlaySound(None, winsound.SND_PURGE)
            elif self._wmp_process is not None:
                self._wmp_process.terminate()
                try:
                    self._wmp_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._wmp_process.kill()
                    self._wmp_process.wait(timeout=3)
                self._wmp_process = None
            self._playing = False
            return True

    def close(self) -> None:
        self.stop()


class ClientState:
    """Thread-safe persistent state shared by polling and console commands."""

    def __init__(self, state_file: Path, initial_state: MonitorState) -> None:
        self._state_file = state_file
        self._state = initial_state
        self._lock = Lock()

    def get(self) -> MonitorState:
        with self._lock:
            return MonitorState(
                latest_issue_id=self._state.latest_issue_id,
                comment_ids=dict(self._state.comment_ids),
            )

    def save(self, state: MonitorState) -> None:
        write_state(self._state_file, state)
        with self._lock:
            self._state = state


def _last_status(client: OkdeskClient, config: ClientConfig, state: ClientState) -> None:
    snapshot = client.snapshot(config.tracked_issue_ids)
    saved = state.get()
    print(
        f"Okdesk latest issue: "
        f"#{snapshot.latest_issue_id if snapshot.latest_issue_id is not None else 'none'}; "
        f"saved client issue: "
        f"#{saved.latest_issue_id if saved.latest_issue_id is not None else 'none'}"
    )
    for issue_id in config.tracked_issue_ids:
        remote_id = max(snapshot.comment_ids[issue_id], default=0)
        saved_id = saved.comment_ids.get(issue_id)
        print(
            f"Issue #{issue_id} latest comment: "
            f"#{remote_id if remote_id else 'none'}; saved comment: "
            f"#{saved_id if saved_id else 'none'}"
        )


def acknowledgement_loop(
    alarm: AlarmController,
    client: OkdeskClient,
    config: ClientConfig,
    state: ClientState,
    shutdown: Event,
) -> None:
    while not shutdown.is_set():
        try:
            command = input().strip().lower()
        except EOFError:
            return
        if command == "awake":
            if alarm.stop():
                log("Alarm acknowledged.")
            else:
                log("No alarm is currently playing.")
        elif command == "test sound":
            if alarm.start():
                log('Sound test started. Type "awake" to stop it.')
            else:
                log("Alarm is already playing.")
        elif command == "last":
            try:
                _last_status(client, config, state)
            except ClientError as error:
                if config.debug:
                    log(f"Last-status request failed: {error}", error=True)
                else:
                    log("Last-status request failed.", error=True)
        elif command:
            print('Unknown command. Use "awake", "test sound", or "last".')


def run(config: ClientConfig) -> None:
    state = ClientState(config.state_file, read_state(config.state_file))
    client = OkdeskClient(config)
    alarm = AlarmController(config.audio_file)
    shutdown = Event()
    command_thread = Thread(
        target=acknowledgement_loop,
        args=(alarm, client, config, state, shutdown),
        name="alarm-acknowledgement",
        daemon=True,
    )
    command_thread.start()
    tracked = ", ".join(f"#{value}" for value in config.tracked_issue_ids) or "none"
    log(f'Client started. Tracked issues: {tracked}. Commands: "awake", "test sound", "last".')

    try:
        while True:
            try:
                current_state = state.get()
                snapshot, request_errors = fetch_snapshot(
                    client, config.tracked_issue_ids
                )
                evaluation = evaluate_snapshot(
                    current_state, snapshot, config.tracked_issue_ids
                )

                if evaluation.alerts:
                    started = alarm.start()
                    state.save(evaluation.state)
                    for message in evaluation.alerts:
                        log(f"{message}; alarm {'started' if started else 'is already playing'}.")
                elif evaluation.state != current_state:
                    state.save(evaluation.state)

                if config.debug:
                    for error in request_errors:
                        log(f"Request failed: {error}", error=True)
                    for issue_id in evaluation.baselined_issue_ids:
                        comment_id = evaluation.state.comment_ids[issue_id]
                        log(
                            f"Issue #{issue_id} comment baseline saved as "
                            f"#{comment_id if comment_id else 'none'}."
                        )
                    if not evaluation.alerts:
                        latest = snapshot.latest_issue_id
                        log(
                            f"No new events; latest issue is "
                            f"#{latest if latest is not None else 'none'}."
                        )
                elif request_errors:
                    log(
                        f"{len(request_errors)} Okdesk request(s) failed; will retry.",
                        error=True,
                    )
            except ClientError as error:
                if config.debug:
                    log(f"Request failed: {error}", error=True)
                else:
                    log("Request failed; will retry.", error=True)
            shutdown.wait(config.poll_interval_seconds)
    finally:
        shutdown.set()
        alarm.close()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Play an alarm for new Okdesk issues and tracked-issue comments."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("alarm_client.json"),
        help="path to JSON configuration file (default: alarm_client.json)",
    )
    return parser.parse_args()


def main() -> int:
    try:
        config = load_config(parse_arguments().config.resolve())
        run(config)
    except ClientError as error:
        log(f"Fatal error: {error}", error=True)
        return 1
    except KeyboardInterrupt:
        print()
        log("Client stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
