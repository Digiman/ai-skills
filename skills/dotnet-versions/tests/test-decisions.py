#!/usr/bin/env python3
"""Exercise the decision logic against synthetic inventories.

The real machine is usually already tidy, so the EOL / delete / BREAKS paths
would otherwise never run. These tests call the functions directly and check
the decisions, not the formatting.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dotnet-versions.py"
spec = importlib.util.spec_from_file_location("dotnet_versions", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def entry(channel, phase, eol=None, rtype="lts", latest_release=None,
          latest_sdk=None, latest_runtime=None, security=None):
    return {
        "channel-version": channel,
        "support-phase": phase,
        "eol-date": eol,
        "release-type": rtype,
        "latest-release": latest_release or f"{channel}.0",
        "latest-sdk": latest_sdk,
        "latest-runtime": latest_runtime,
        "security": security,
    }


def channels(*entries):
    return {e["channel-version"]: e for e in entries}


class TestVersionParsing(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(mod.vparse("8.0.425"), (8, 0, 425))

    def test_prerelease_suffix_dropped(self):
        self.assertEqual(mod.vparse("11.0.0-rc.1.26425.128"), (11, 0, 0))
        self.assertEqual(mod.vparse("7.0.100-preview.7.24328.19"), (7, 0, 100))

    def test_two_part_channel(self):
        self.assertEqual(mod.channel_of("3.1.426"), "3.1")

    def test_feature_band(self):
        self.assertEqual(mod.feature_band(mod.vparse("8.0.425")), (8, 0, 4))
        self.assertEqual(mod.feature_band(mod.vparse("10.0.100")), (10, 0, 1))


class TestPhaseLabels(unittest.TestCase):
    def test_eol_is_unsupported(self):
        st = mod.support_status("6.0.428", channels(entry("6.0", "eol", "2024-11-12")))
        self.assertEqual(st["label"], "UNSUPPORTED")

    def test_maintenance_is_security_only(self):
        st = mod.support_status("8.0.425", channels(entry("8.0", "maintenance", "2026-11-10")))
        self.assertEqual(st["label"], "security-only")

    def test_active_is_supported(self):
        st = mod.support_status("10.0.401", channels(entry("10.0", "active", "2028-11-14")))
        self.assertEqual(st["label"], "supported")

    def test_golive_rc_is_preview(self):
        """A go-live channel whose only release is an RC is not 'supported'."""
        ch = channels(entry("11.0", "go-live", rtype="sts", latest_release="11.0.0-rc.1"))
        self.assertEqual(mod.support_status("11.0.100", ch)["label"], "preview")

    def test_unknown_channel(self):
        self.assertEqual(mod.support_status("5.0.1", channels())["label"], "unknown")

    def test_security_false_does_not_change_label(self):
        st = mod.support_status("6.0.428", channels(entry("6.0", "eol", "2024-11-12", security=False)))
        self.assertIs(st["security"], False)


class TestBehindLatest(unittest.TestCase):
    def test_sdk_compared_against_sdk_not_runtime(self):
        """10.0.401 (SDK) vs 10.0.12 (runtime) must not read as 'behind'."""
        ch = channels(entry("10.0", "active", "2028-11-14",
                            latest_sdk="10.0.401", latest_runtime="10.0.12"))
        self.assertFalse(mod.support_status("10.0.401", ch, "sdk")["behind_latest"])
        self.assertFalse(mod.support_status("10.0.12", ch, "runtime")["behind_latest"])

    def test_sdk_behind_when_sdk_is_older(self):
        ch = channels(entry("10.0", "active", "2028-11-14",
                            latest_sdk="10.0.401", latest_runtime="10.0.12"))
        self.assertTrue(mod.support_status("10.0.301", ch, "sdk")["behind_latest"])

    def test_runtime_behind_when_runtime_is_older(self):
        ch = channels(entry("8.0", "maintenance", "2026-11-10",
                            latest_sdk="8.0.425", latest_runtime="8.0.31"))
        self.assertTrue(mod.support_status("8.0.11", ch, "runtime")["behind_latest"])


class TestClassify(unittest.TestCase):
    def test_keeps_newest_per_major(self):
        installed = [("8.0.100", "/r"), ("8.0.425", "/r"), ("9.0.318", "/r")]
        keep, drop = mod.classify(installed, "major")
        self.assertEqual([v for v, _ in keep], ["8.0.425", "9.0.318"])
        self.assertEqual([v for v, _ in drop], ["8.0.100"])

    def test_minor_band(self):
        installed = [("8.0.100", "/r"), ("8.0.425", "/r"), ("8.1.200", "/r")]
        keep, _ = mod.classify(installed, "minor")
        self.assertEqual([v for v, _ in keep], ["8.0.425", "8.1.200"])


class TestApplyEol(unittest.TestCase):
    def test_eol_channel_leaves_keep_entirely(self):
        keep = [("6.0.428", "/r"), ("8.0.425", "/r")]
        new_keep, new_drop = mod.apply_eol(keep, [], {"6.0"})
        self.assertEqual([v for v, _ in new_keep], ["8.0.425"])
        self.assertEqual([v for v, _ in new_drop], ["6.0.428"])

    def test_already_dropped_not_duplicated(self):
        keep = [("6.0.428", "/r")]
        _, new_drop = mod.apply_eol(keep, [("6.0.100", "/r")], {"6.0"})
        self.assertEqual([v for v, _ in new_drop], ["6.0.100", "6.0.428"])

    def test_no_eol_is_identity(self):
        keep, drop = [("8.0.425", "/r")], [("8.0.100", "/r")]
        self.assertEqual(mod.apply_eol(keep, drop, set()), (keep, drop))


class TestEolDetection(unittest.TestCase):
    def test_only_installed_eol_channels(self):
        ch = channels(entry("6.0", "eol", "2024-11-12"), entry("8.0", "active", "2028-11-14"),
                      entry("7.0", "eol", "2024-05-14"))
        installed = [("8.0.425", "/r"), ("6.0.428", "/r")]
        self.assertEqual(mod.eol_channels_of(ch, installed), {"6.0"})

    def test_maintenance_is_not_eol(self):
        ch = channels(entry("8.0", "maintenance", "2026-11-10"))
        self.assertEqual(mod.eol_channels_of(ch, [("8.0.425", "/r")]), set())


class TestPinSatisfaction(unittest.TestCase):
    KEPT = [mod.vparse(v) for v in ("8.0.425", "9.0.318", "10.0.401")]

    def test_latest_feature_within_channel_ok(self):
        self.assertTrue(mod.satisfies(mod.vparse("8.0.408"), "latestFeature", self.KEPT))

    def test_patch_policy_does_not_cross_feature_band(self):
        """8.0.308 pinned with rollForward=patch must not reach 8.0.425.

        Both 8.0.308 and 8.0.425 sit in the 8.0.4xx band, so 'patch' legitimately
        rolls between them. The 3xx -> 4xx step is a feature-band move, which
        'patch' is not allowed to make.
        """
        self.assertTrue(mod.satisfies(mod.vparse("8.0.408"), "patch", self.KEPT))
        self.assertFalse(mod.satisfies(mod.vparse("8.0.308"), "patch", self.KEPT))

    def test_feature_policy_can_cross_bands_within_minor(self):
        self.assertTrue(mod.satisfies(mod.vparse("8.0.308"), "latestFeature", self.KEPT))

    def test_major_rollforward_does_not_cross_major(self):
        """A major policy still stays inside its own major version."""
        self.assertFalse(mod.satisfies(mod.vparse("7.0.410"), "latestMajor", self.KEPT))

    def test_disable_requires_exact_pin(self):
        self.assertFalse(mod.satisfies(mod.vparse("8.0.408"), "disable", self.KEPT))
        self.assertTrue(mod.satisfies(mod.vparse("8.0.425"), "disable", self.KEPT))

    def test_pin_older_than_kept_feature_band(self):
        self.assertTrue(mod.satisfies(mod.vparse("10.0.100"), "latestFeature", self.KEPT))


class TestEndToEndDecisions(unittest.TestCase):
    """The scenario that motivated the change: an EOL channel installed alone."""

    def setUp(self):
        self.channels = channels(
            entry("8.0", "maintenance", "2026-11-10", latest_sdk="8.0.425",
                  latest_runtime="8.0.31"),
            entry("7.0", "eol", "2024-05-14", rtype="sts", latest_sdk="7.0.410",
                  latest_runtime="7.0.20"),
            entry("10.0", "active", "2028-11-14", latest_sdk="10.0.401",
                  latest_runtime="10.0.12"),
        )

    def test_eol_dropped_only_with_flag(self):
        sdks = [("7.0.410", "/r"), ("8.0.425", "/r"), ("10.0.401", "/r")]
        keep, drop = mod.classify(sdks, "major")
        eol = mod.eol_channels_of(self.channels, sdks)
        self.assertEqual(eol, {"7.0"})

        # Default: 7.0 is newest in its band, so it is kept like any other.
        self.assertIn("7.0.410", [v for v, _ in keep])

        # With the flag: the whole channel goes.
        keep2, drop2 = mod.apply_eol(keep, drop, eol)
        self.assertNotIn("7.0.410", [v for v, _ in keep2])
        self.assertIn("7.0.410", [v for v, _ in drop2])

    def test_dropping_eol_channel_breaks_its_pins(self):
        """Removing an EOL channel must surface the repos that pinned it."""
        sdks = [("7.0.410", "/r"), ("10.0.401", "/r")]
        keep, _ = mod.classify(sdks, "major")
        eol = mod.eol_channels_of(self.channels, sdks)
        keep, _ = mod.apply_eol(keep, [], eol)
        kept_versions = [v for v, _ in keep]
        self.assertFalse(mod.satisfies(mod.vparse("7.0.408"), "latestFeature",
                                       [mod.vparse(v) for v in kept_versions]))
        self.assertTrue(mod.satisfies(mod.vparse("10.0.100"), "latestFeature",
                                      [mod.vparse(v) for v in kept_versions]))


class TestEolCountdown(unittest.TestCase):
    def test_past_eol_is_negative(self):
        st = mod.channel_status(entry("7.0", "eol", "2020-01-01"))
        self.assertLess(st["eol_in_days"], 0)

    def test_missing_eol_date_is_none(self):
        self.assertIsNone(mod.channel_status(entry("11.0", "go-live"))["eol_in_days"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
