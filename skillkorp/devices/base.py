#!/usr/bin/env python3
"""
SkillKorp Suite - Common Device Driver Interface

Defines the abstract contract shared by the K20 keyboard and M20 mouse
drivers so that the CLI, GUI, tray, and profile manager can operate on
either device polymorphically. The two hardware protocols are entirely
different (different chipsets, HID Usage Pages, and report layouts) -
this base class only unifies the application-level surface, never the
low-level frame encoding, which stays in each concrete driver unchanged.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class SkillkorpDeviceDriver(ABC):
    """Abstract base class for a SkillKorp hardware device driver."""

    #: "keyboard" or "mouse" - set by concrete subclasses.
    device_type: str = ""

    dev_path: Optional[str]
    config: Dict[str, Any]

    @staticmethod
    @abstractmethod
    def find_device() -> Optional[str]:
        """Locate and return the hidraw device node path for this device, or None."""
        raise NotImplementedError

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the device is currently present and accessible."""
        raise NotImplementedError

    @abstractmethod
    def is_wireless(self) -> bool:
        """Return True if the device is connected via its 2.4GHz wireless dongle."""
        raise NotImplementedError

    @abstractmethod
    def get_battery(self) -> Dict[str, Any]:
        """Query battery status. Returns a dict with at least 'percentage',
        'charging', 'connected', 'wireless', and 'status_str' keys."""
        raise NotImplementedError

    @abstractmethod
    def apply_profile(self, profile: Dict[str, Any]) -> bool:
        """Apply a complete configuration profile dict to the hardware."""
        raise NotImplementedError

    @abstractmethod
    def reload_config_if_changed(self) -> bool:
        """Reload self.config from disk if it was modified externally (by another process)."""
        raise NotImplementedError

    @abstractmethod
    def save_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Persist the current (or given) configuration to disk."""
        raise NotImplementedError
