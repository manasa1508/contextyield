"""Energy labels must be conservative under missing and reset counters."""

from __future__ import annotations

from contextyield.energy import DeviceEnergyProbe


def test_unconfigured_is_unknown():
    probe = DeviceEnergyProbe(None, False)
    assert probe.finish(probe.start())["joules"] is None


def test_device_counter_unit_conversion_and_attribution():
    values = iter([1000, 3500])
    probe = DeviceEnergyProbe("nvml:0", True, lambda _: next(values))
    observed = probe.finish(probe.start())
    assert observed["joules"] == 2.5
    assert observed["basis"] == "device_counter_window"
    assert observed["attributable_to_request"] is True


def test_shared_device_is_not_request_attributable():
    values = iter([1000, 2000])
    probe = DeviceEnergyProbe("nvml:0", False, lambda _: next(values))
    assert probe.finish(probe.start())["attributable_to_request"] is False


def test_counter_reset_becomes_unknown():
    values = iter([2000, 1000])
    probe = DeviceEnergyProbe("nvml:0", True, lambda _: next(values))
    observed = probe.finish(probe.start())
    assert observed["basis"] == "unavailable"
    assert observed["joules"] is None
