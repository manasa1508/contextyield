"""Optional co-located NVIDIA device-window observation, never facility energy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

_NVML_LOCK = Lock()


@dataclass(frozen=True)
class EnergyWindow:
    start_mj: int | None
    error: str | None = None


class DeviceEnergyProbe:
    def __init__(self, meter: str | None, exclusive_device: bool,
                 reader: Callable[[int], int] | None = None) -> None:
        self.meter = meter
        self.exclusive_device = exclusive_device
        self.reader = reader or _nvml_total_energy_mj

    def start(self) -> EnergyWindow:
        if self.meter is None:
            return EnergyWindow(None, "not_configured")
        try:
            index = int(self.meter.split(":", 1)[1])
            return EnergyWindow(self.reader(index))
        except Exception as exc:  # noqa: BLE001 - optional telemetry cannot fail a request
            return EnergyWindow(None, type(exc).__name__)

    def finish(self, window: EnergyWindow) -> dict:
        if window.start_mj is None:
            return {"basis": "unavailable", "joules": None, "scope": "none",
                    "attributable_to_request": False, "reason": window.error}
        try:
            index = int(self.meter.split(":", 1)[1]) if self.meter else 0
            end_mj = self.reader(index)
            if end_mj < window.start_mj:
                raise ValueError("counter_reset")
            return {"basis": "device_counter_window", "joules": (end_mj - window.start_mj) / 1000,
                    "scope": "gpu_device_window", "attributable_to_request": self.exclusive_device,
                    "reason": "gross_device_energy_includes_idle; excludes_CPU_and_facility"}
        except Exception as exc:  # noqa: BLE001 - unsupported/reset meter means unknown
            return {"basis": "unavailable", "joules": None, "scope": "none",
                    "attributable_to_request": False, "reason": type(exc).__name__}


def _nvml_total_energy_mj(index: int) -> int:
    import pynvml  # type: ignore[import-not-found]

    with _NVML_LOCK:
        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            return int(pynvml.nvmlDeviceGetTotalEnergyConsumption(handle))
        finally:
            pynvml.nvmlShutdown()
