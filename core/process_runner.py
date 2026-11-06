import os
import sys
import time
import logging
import threading
import subprocess
from typing import List, Optional, Dict, Tuple, Callable
import psutil

logger = logging.getLogger("director.process_runner")

_ACTIVE: Dict[int, str] = {}          # pid -> executable, for every CLI the runner currently has alive
_ACTIVE_LOCK = threading.Lock()


class OperationStopped(Exception):
    """The operator stopped this work: nothing further may be launched or advanced on this thread."""


_STOP = threading.local()


def set_stop_guard(guard: Optional[Callable[[], Optional[str]]]) -> None:
    """A guard returns a reason once the work on this thread has been stopped, otherwise None."""
    _STOP.guard = guard


def check_stop() -> None:
    guard = getattr(_STOP, "guard", None)
    reason = guard() if guard else None
    if reason:
        raise OperationStopped(f"OPERATION_STOPPED: {reason}")


def active_processes() -> Dict[int, str]:
    with _ACTIVE_LOCK:
        return dict(_ACTIVE)

class ProcessKillFailedError(Exception):
    """Raised when taskkill fails to terminate the process tree and process.poll() remains None."""
    pass

class ProcessResult:
    def __init__(
        self,
        exit_code: Optional[int],
        stdout: str,
        stderr: str,
        timed_out: bool,
        duration_sec: float,
        pid: int,
        timeout_reason: Optional[str] = None
    ):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.duration_sec = duration_sec
        self.pid = pid
        self.timeout_reason = timeout_reason

REAL_CLI_BASENAMES = {
    "codex", "codex.cmd", "codex.exe", "codex.ps1",
    "claude", "claude.cmd", "claude.exe", "claude.ps1",
    "agy", "agy.cmd", "agy.exe",
}


class RealCliCallDuringUnitTest(RuntimeError):
    """Raised when a real AI CLI would be launched while DIRECTOR_TESTING=1 without the integration opt-in."""


class ProcessTreeRunner:
    """
    Orchestrator-owned process manager.
    - Spawns subprocesses with shell=False
    - Tracks process tree and monitors timeouts (hard timeout + no-output timeout)
    - Records heartbeats every 30s
    - Terminates entire process tree using taskkill /PID <pid> /T /F
    - Verifies process.poll() confirms termination
    """
    blocked_real_cli_calls: List[List[str]] = []   # real-CLI launches refused under DIRECTOR_TESTING=1

    @classmethod
    def kill_active(cls) -> List[int]:
        """작업 정지: kill every CLI process the runner has alive (builder/reviewer/head). Returns the pids killed."""
        killed = []
        for pid in list(active_processes()):
            try:
                if cls.kill_process_tree(pid):
                    killed.append(pid)
            except Exception as exc:
                logger.warning(f"kill_active: pid {pid}: {exc}")
            with _ACTIVE_LOCK:
                _ACTIVE.pop(pid, None)
        return killed

    @staticmethod
    def kill_process_tree(pid: int) -> bool:
        """
        Kills process and all its children via Windows taskkill /PID <pid> /T /F,
        with psutil fallback.
        """
        if not psutil.pid_exists(pid):
            return True

        # 1. Primary: Windows taskkill
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                timeout=5
            )
        except Exception as e:
            logger.warning(f"taskkill failed on PID {pid}: {e}")

        # 2. Safety cleanup with psutil
        try:
            if psutil.pid_exists(pid):
                p = psutil.Process(pid)
                for child in p.children(recursive=True):
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        time.sleep(0.1)
        return not psutil.pid_exists(pid)

    @classmethod
    def run(
        cls,
        argv: List[str],
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: float = 300.0,
        no_output_timeout: Optional[float] = None,
        heartbeat_interval: float = 30.0,
        on_heartbeat: Optional[Callable[[int, float], None]] = None,
        stdin_data: Optional[str] = None
    ) -> ProcessResult:
        """Every AI CLI call is written to the work ledger (core.usage) around the real execution; ledger problems
        never affect the call itself."""
        row_id = None
        try:
            from core import usage
            if usage.provider_of(argv[0] if argv else "") and not cls._guarded(argv):
                row_id = usage.call_started(argv, stdin_data)
        except Exception:
            row_id = None
        try:
            result = cls._run_process(argv, cwd=cwd, env=env, timeout=timeout, no_output_timeout=no_output_timeout,
                                      heartbeat_interval=heartbeat_interval, on_heartbeat=on_heartbeat,
                                      stdin_data=stdin_data)
        except BaseException as exc:
            try:
                from core import usage
                usage.call_failed(row_id, exc)
            except Exception:
                pass
            raise
        try:
            from core import usage
            usage.call_finished(row_id, argv, stdin_data, result)
        except Exception:
            pass
        return result

    @staticmethod
    def _guarded(argv: List[str]) -> bool:
        """A real AI CLI that the unit-test guard is about to refuse: nothing ran, nothing to record."""
        return os.environ.get("DIRECTOR_TESTING") == "1" and os.environ.get("DIRECTOR_ALLOW_REAL_CLI") != "1"

    @classmethod
    def _run_process(
        cls,
        argv: List[str],
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: float = 300.0,
        no_output_timeout: Optional[float] = None,
        heartbeat_interval: float = 30.0,
        on_heartbeat: Optional[Callable[[int, float], None]] = None,
        stdin_data: Optional[str] = None
    ) -> ProcessResult:
        """
        Executes a CLI command via Popen (shell=False).
        Orchestrator tracks the PID and ensures hard timeout and hang protection.
        """
        check_stop()   # covers every retry and failover path after a stop killed the current CLI
        # Unit-test guard: under DIRECTOR_TESTING=1 a real Codex/Claude/Antigravity CLI is never launched unless the
        # test explicitly opted in (integration_cli marker sets DIRECTOR_ALLOW_REAL_CLI=1). This prevents mock tests
        # from consuming real quota or hanging for the full CLI timeout.
        if os.environ.get("DIRECTOR_TESTING") == "1" and os.environ.get("DIRECTOR_ALLOW_REAL_CLI") != "1":
            base = os.path.basename(str(argv[0])).lower() if argv else ""
            if base in REAL_CLI_BASENAMES:
                cls.blocked_real_cli_calls.append(list(argv))
                raise RealCliCallDuringUnitTest(
                    f"REAL_CLI_CALL_DURING_UNIT_TEST: refusing to launch {base} while DIRECTOR_TESTING=1 "
                    f"(use @pytest.mark.integration_cli for real-CLI tests)"
                )

        start_time = time.time()
        last_output_time = [start_time]
        last_heartbeat_time = [start_time]
        stdout_chunks: List[str] = []
        stderr_chunks: List[str] = []

        logger.info(f"[ProcessTreeRunner] Starting: {argv} (cwd={cwd}, timeout={timeout}s)")

        # Start process with shell=False
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            shell=False,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        pid = process.pid
        with _ACTIVE_LOCK:
            _ACTIVE[pid] = str(argv[0])

        if stdin_data is not None:
            try:
                process.stdin.write(stdin_data)
                process.stdin.flush()
                process.stdin.close()
            except Exception as e:
                logger.warning(f"[ProcessTreeRunner] Failed to write to stdin: {e}")

        def _reader(pipe, chunk_list):
            try:
                for line in iter(pipe.readline, ""):
                    chunk_list.append(line)
                    last_output_time[0] = time.time()
                pipe.close()
            except Exception:
                pass

        out_thread = threading.Thread(target=_reader, args=(process.stdout, stdout_chunks), daemon=True)
        err_thread = threading.Thread(target=_reader, args=(process.stderr, stderr_chunks), daemon=True)
        out_thread.start()
        err_thread.start()

        timed_out = False
        timeout_reason = None

        while True:
            # Check if process naturally finished
            ret = process.poll()
            if ret is not None:
                break

            now = time.time()
            elapsed = now - start_time

            # 1. Heartbeat check (every 30s)
            if (now - last_heartbeat_time[0]) >= heartbeat_interval:
                last_heartbeat_time[0] = now
                logger.info(f"[Heartbeat] PID {pid} running for {elapsed:.1f}s")
                if on_heartbeat:
                    try:
                        on_heartbeat(pid, elapsed)
                    except Exception:
                        pass

            # 2. Hard timeout check
            if elapsed >= timeout:
                timed_out = True
                timeout_reason = f"HARD_TIMEOUT: Exceeded {timeout}s"
                logger.error(f"[ProcessTreeRunner] PID {pid} exceeded hard timeout ({timeout}s). Terminating process tree...")
                break

            # 3. No-output timeout check
            if no_output_timeout and (now - last_output_time[0]) >= no_output_timeout:
                timed_out = True
                timeout_reason = f"NO_OUTPUT_TIMEOUT: No output for {no_output_timeout}s"
                logger.error(f"[ProcessTreeRunner] PID {pid} produced no output for {no_output_timeout}s. Terminating process tree...")
                break

            time.sleep(0.05)

        duration_sec = round(time.time() - start_time, 3)
        with _ACTIVE_LOCK:
            _ACTIVE.pop(pid, None)

        if timed_out:
            # Terminate entire process tree using taskkill /PID <pid> /T /F
            cls.kill_process_tree(pid)

            # Confirm process.poll() is not None
            poll_deadline = time.time() + 2.0
            while time.time() < poll_deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.05)

            if process.poll() is None:
                # Force kill if still somehow running
                try:
                    process.kill()
                except Exception:
                    pass
                if process.poll() is None:
                    raise ProcessKillFailedError(f"PROCESS_KILL_FAILED: Failed to terminate PID {pid}")

            out_thread.join(timeout=0.5)
            err_thread.join(timeout=0.5)

            return ProcessResult(
                exit_code=process.poll(),
                stdout="".join(stdout_chunks),
                stderr="".join(stderr_chunks),
                timed_out=True,
                duration_sec=duration_sec,
                pid=pid,
                timeout_reason=timeout_reason
            )

        out_thread.join(timeout=1.0)
        err_thread.join(timeout=1.0)

        return ProcessResult(
            exit_code=process.poll(),
            stdout="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
            timed_out=False,
            duration_sec=duration_sec,
            pid=pid,
            timeout_reason=None
        )
