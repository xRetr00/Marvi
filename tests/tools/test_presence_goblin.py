"""Tests for tools/presence/goblin.py's check_stuck heuristic and the
notify_stuck shoulder-tap job it schedules.

check_stuck fixtures are AW window-event streams, newest first, shaped like
AWClient.get_events()'s output: {"timestamp": iso, "duration": seconds,
"data": {"app": ..., "title": ...}}.

notify_stuck tests never touch a real AW instance or cron scheduler: they
monkeypatch cron.jobs.create_job/list_jobs (the only cron surface goblin.py
calls) and tools.presence.common.get_presence_config, the same way the rest
of the presence test suite mocks out AW/config.
"""

import tools.presence.goblin as goblin
from tools.presence.goblin import (
    DEBOUNCE_SECONDS,
    INVESTIGATION_TOOLSETS,
    SHOULDER_TAP_JOB_NAME,
    STUCK_MIN_DURATION_SECONDS,
    check_stuck,
    notify_stuck,
)


def _event(app: str, title: str, duration: float, ts: str = "2026-07-09T10:00:00Z") -> dict:
    return {"timestamp": ts, "duration": duration, "data": {"app": app, "title": title}}


class TestCheckStuckNoFalsePositives:
    def test_normal_long_focused_work_session_is_not_stuck(self):
        """A long, uninterrupted, error-free coding session must NOT trigger --
        duration alone is not enough; one of the two corroborating signals is
        required (design spec: ">45 min AND (error keywords OR rapid search
        switching)")."""
        title = "aw_client.py - hermes-agent - Visual Studio Code"
        events = [_event("Code.exe", title, 600) for _ in range(6)]  # 60 minutes total
        assert check_stuck(events) is None

    def test_short_session_with_error_keyword_is_not_stuck(self):
        """Error-looking title but under the 45-minute threshold: not stuck yet."""
        events = [_event("Code.exe", "main.py - TypeError: undefined - Visual Studio Code", 300)]
        assert check_stuck(events) is None

    def test_normal_app_switching_without_search_is_not_stuck(self):
        """Switching between a few ordinary apps (not search/SO tabs) plus a
        long current session must not be flagged."""
        events = [_event("Code.exe", "main.py - proj - Visual Studio Code", 2800)]
        events += [
            _event("Slack.exe", "general - Slack", 60),
            _event("Outlook.exe", "Inbox - Outlook", 120),
        ]
        assert check_stuck(events) is None

    def test_empty_events(self):
        assert check_stuck([]) is None

    def test_missing_title_is_ignored(self):
        assert check_stuck([_event("Code.exe", "", 3000)]) is None

    def test_malformed_event_does_not_raise(self):
        assert check_stuck([{"garbage": True}]) is None


class TestCheckStuckDetectsRealSignals:
    def test_error_keyword_in_current_title_triggers(self):
        events = [
            _event(
                "Code.exe",
                "main.py - TypeError: cannot read property - Visual Studio Code",
                STUCK_MIN_DURATION_SECONDS + 60,
            )
        ]
        finding = check_stuck(events)
        assert finding is not None
        assert finding["stuck"] is True
        assert finding["signal"] == "error_keyword"
        assert finding["duration_seconds"] >= STUCK_MIN_DURATION_SECONDS

    def test_error_keyword_seen_in_lookback_triggers(self):
        # Current window itself is clean, but the events leading up to it
        # (still within the same settled window) show an error-looking title.
        title = "main.py - proj - Visual Studio Code"
        events = [_event("Code.exe", title, STUCK_MIN_DURATION_SECONDS + 120)]
        finding = check_stuck(events)
        # Same-title stretch has no error keyword and no search switching --
        # not stuck (duration alone is insufficient, matches the no-false-
        # positive contract above).
        assert finding is None

    def test_rapid_search_tab_switching_triggers(self):
        title = "main.py - proj - Visual Studio Code"
        events = [_event("Code.exe", title, STUCK_MIN_DURATION_SECONDS + 60)]
        events += [
            _event("chrome.exe", "python list comprehension trick - Stack Overflow - Google Chrome", 20),
            _event("chrome.exe", "python list comprehension - Google Search", 15),
            _event("chrome.exe", "another list comprehension answer - Stack Overflow - Google Chrome", 25),
        ]
        finding = check_stuck(events)
        assert finding is not None
        assert finding["signal"] == "rapid_search_switching"

    def test_two_search_switches_not_enough(self):
        """Below the rapid-switch threshold (>=3): must not trigger."""
        title = "main.py - proj - Visual Studio Code"
        events = [_event("Code.exe", title, STUCK_MIN_DURATION_SECONDS + 60)]
        events += [
            _event("chrome.exe", "fix - Stack Overflow - Google Chrome", 20),
            _event("chrome.exe", "fix - Google Search", 15),
        ]
        assert check_stuck(events) is None


def _finding(**overrides) -> dict:
    base = {
        "stuck": True,
        "app": "Code.exe",
        "title": "aw_client.py - TypeError: cannot read property - hermes-agent - Visual Studio Code",
        "duration_seconds": 3000,
        "signal": "error_keyword",
    }
    base.update(overrides)
    return base


class _RecordingCreateJob:
    """Stand-in for cron.jobs.create_job that records the kwargs it was
    called with, so tests can assert on the prompt/toolsets/name without a
    real cron scheduler."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "fake-job-id", **kwargs}


class _GoblinNotifyHarness:
    """Patches every external seam notify_stuck touches (cron.jobs,
    tools.presence.common.get_presence_config, the delivery-target lookup,
    and the debounce state) so tests exercise only goblin.py's own logic."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.create_job = _RecordingCreateJob()
        self.marked_notified = False

        import cron.jobs as cron_jobs

        monkeypatch.setattr(cron_jobs, "create_job", self.create_job)
        self.set_pending_jobs([])
        self.set_investigation(True)
        self.set_delivery_target("telegram")
        self.set_should_notify(True)
        monkeypatch.setattr(goblin, "_mark_notified", self._mark_notified)

    def _mark_notified(self):
        self.marked_notified = True

    def set_pending_jobs(self, jobs):
        import cron.jobs as cron_jobs

        self.monkeypatch.setattr(cron_jobs, "list_jobs", lambda *a, **kw: list(jobs))

    def set_investigation(self, enabled: bool):
        import tools.presence.common as presence_common

        def fake_get_presence_config():
            return {"goblin": {"shoulder_taps": True, "investigation": enabled}}

        self.monkeypatch.setattr(presence_common, "get_presence_config", fake_get_presence_config)

    def set_delivery_target(self, target):
        self.monkeypatch.setattr(goblin, "_pick_delivery_target", lambda: target)

    def set_should_notify(self, value: bool):
        self.monkeypatch.setattr(goblin, "should_notify_now", lambda: value)


class TestNotifyStuckInvestigation:
    """Default (presence.goblin.investigation=true): the shoulder-tap job
    must be told to investigate before it messages the user."""

    def test_creates_job_with_finding_context_and_silent_instruction(self, monkeypatch):
        harness = _GoblinNotifyHarness(monkeypatch)
        finding = _finding()

        assert notify_stuck(finding) is True
        assert len(harness.create_job.calls) == 1
        call = harness.create_job.calls[0]
        prompt = call["prompt"]

        # Finding context: window title, app, and duration must all be present
        # so the agent doesn't have to re-derive what "stuck" means.
        assert finding["title"] in prompt
        assert finding["app"] in prompt
        assert "50 minute" in prompt  # 3000s == 50 minutes

        # Directed to investigate with its own tools, not just message.
        assert "INVESTIGATE" in prompt
        assert "read" in prompt.lower()
        assert "search the web" in prompt.lower()

        # Respects the existing [SILENT] delivery convention when
        # investigation turns up nothing useful.
        assert "[SILENT]" in prompt

        # Short + concrete deliverable shape: diagnosis + one suggestion + offer.
        assert "ONE concrete suggestion" in prompt
        assert "go deeper" in prompt

    def test_includes_parsed_vscode_workspace_and_file(self, monkeypatch):
        harness = _GoblinNotifyHarness(monkeypatch)
        finding = _finding(
            title="aw_client.py - TypeError: cannot read property - hermes-agent - Visual Studio Code",
            app="Code.exe",
        )

        notify_stuck(finding)
        prompt = harness.create_job.calls[0]["prompt"]

        assert "hermes-agent" in prompt  # parsed workspace
        assert "aw_client.py" in prompt  # parsed file

    def test_requests_file_and_web_toolsets(self, monkeypatch):
        harness = _GoblinNotifyHarness(monkeypatch)
        notify_stuck(_finding())

        call = harness.create_job.calls[0]
        assert call["enabled_toolsets"] == INVESTIGATION_TOOLSETS
        assert call["enabled_toolsets"] == ["file", "web"]
        assert "cronjob" not in call["enabled_toolsets"]

    def test_job_is_one_shot_and_named_for_dedup(self, monkeypatch):
        harness = _GoblinNotifyHarness(monkeypatch)
        notify_stuck(_finding())

        call = harness.create_job.calls[0]
        assert call["name"] == SHOULDER_TAP_JOB_NAME
        assert call["repeat"] == 1
        assert call["schedule"] == "1m"
        assert call["deliver"] == "telegram"

    def test_marks_notified_on_success(self, monkeypatch):
        harness = _GoblinNotifyHarness(monkeypatch)
        assert harness.marked_notified is False
        notify_stuck(_finding())
        assert harness.marked_notified is True


class TestNotifyStuckStaticFallback:
    """presence.goblin.investigation=false: exactly the old static-offer
    prompt, no toolsets."""

    def test_uses_unchanged_static_offer_prompt(self, monkeypatch):
        harness = _GoblinNotifyHarness(monkeypatch)
        harness.set_investigation(False)
        finding = _finding()

        assert notify_stuck(finding) is True
        call = harness.create_job.calls[0]
        prompt = call["prompt"]

        assert prompt == goblin._build_static_prompt(finding)
        assert "INVESTIGATE" not in prompt
        assert "[SILENT]" in prompt
        assert call["enabled_toolsets"] is None

    def test_static_fallback_still_one_shot_same_name(self, monkeypatch):
        harness = _GoblinNotifyHarness(monkeypatch)
        harness.set_investigation(False)
        notify_stuck(_finding())

        call = harness.create_job.calls[0]
        assert call["name"] == SHOULDER_TAP_JOB_NAME
        assert call["repeat"] == 1


class TestNotifyStuckDedupAndDebounce:
    def test_skips_when_a_shoulder_tap_job_is_still_pending(self, monkeypatch):
        harness = _GoblinNotifyHarness(monkeypatch)
        harness.set_pending_jobs([
            {"name": SHOULDER_TAP_JOB_NAME, "enabled": True, "id": "prior-job"},
        ])

        assert notify_stuck(_finding()) is False
        assert harness.create_job.calls == []
        assert harness.marked_notified is False

    def test_does_not_skip_on_a_pending_job_with_a_different_name(self, monkeypatch):
        harness = _GoblinNotifyHarness(monkeypatch)
        harness.set_pending_jobs([
            {"name": "some-other-cron-job", "enabled": True, "id": "unrelated"},
        ])

        assert notify_stuck(_finding()) is True
        assert len(harness.create_job.calls) == 1

    def test_disabled_prior_job_does_not_block_a_new_one(self, monkeypatch):
        """A completed one-shot job is popped from storage by cron/jobs.py,
        but guard against any record that lingers disabled too."""
        harness = _GoblinNotifyHarness(monkeypatch)
        harness.set_pending_jobs([
            {"name": SHOULDER_TAP_JOB_NAME, "enabled": False, "id": "old-job"},
        ])

        assert notify_stuck(_finding()) is True

    def test_debounce_gate_is_untouched(self, monkeypatch):
        """should_notify_now() still gates job creation, and the 2h constant
        is unchanged by the investigation feature."""
        assert DEBOUNCE_SECONDS == 2 * 60 * 60

        harness = _GoblinNotifyHarness(monkeypatch)
        harness.set_should_notify(False)

        assert notify_stuck(_finding()) is False
        assert harness.create_job.calls == []
        assert harness.marked_notified is False

    def test_no_delivery_target_skips_without_creating_job(self, monkeypatch):
        harness = _GoblinNotifyHarness(monkeypatch)
        harness.set_delivery_target(None)

        assert notify_stuck(_finding()) is False
        assert harness.create_job.calls == []
