#!/usr/bin/env python3
"""
SkillKorp Suite - Common Device Driver Interface

Defines the contract shared by the K20 keyboard and M20 mouse drivers so that
the CLI, GUI, tray and profile manager can operate on either device
polymorphically, plus the plumbing both drivers need:

- device discovery and (re)opening through skillkorp.devices.hidraw, with an
  automatic rescan when the hidraw node changed (replug, renumbering);
- the persisted "current state" JSON file, read-modify-written under an
  inter-process lock so the GUI, tray and daemon never lose each other's
  updates, and merged over defaults so older/partial files keep working.

The two hardware protocols are entirely different (different chipsets, HID
usage pages and report layouts): frame encoding stays in each driver.
"""

import copy
import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from skillkorp import config_store
from . import hidraw
from .hidraw import DeviceNotFoundError, HidrawDevice, HidrawNode


class SkillkorpDeviceDriver(ABC):
    """Abstract base class for a SkillKorp hardware device driver."""

    #: "keyboard" or "mouse" - set by concrete subclasses.
    device_type: str = ""
    #: Human readable name used in error messages.
    display_name: str = ""

    dev_path: Optional[str]
    config: Dict[str, Any]

    def __init__(self, dev_path: Optional[str] = None):
        self._node: Optional[HidrawNode] = None
        self._config_mtime: Optional[float] = None
        self.config = self._load_config()
        self.dev_path = dev_path
        if dev_path:
            self._node = hidraw.node_for_path(dev_path)
        else:
            self.dev_path = self.find_device()

    # ------------------------------------------------------------------
    # To implement
    # ------------------------------------------------------------------
    @classmethod
    @abstractmethod
    def find_nodes(cls) -> List[HidrawNode]:
        """Return the matching vendor-interface nodes, preferred first."""

    @abstractmethod
    def config_file(self) -> str:
        """Path of the persisted current-state JSON file."""

    @abstractmethod
    def default_config(self) -> Dict[str, Any]:
        """Factory defaults for the persisted state."""

    def sanitize_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Repair values a user (or an older version) may have stored badly."""
        return cfg

    @abstractmethod
    def get_battery(self) -> Dict[str, Any]:
        """Return a dict with 'percentage', 'charging', 'connected', 'wireless', 'status_str'."""

    @abstractmethod
    def apply_profile(self, profile: Dict[str, Any]) -> bool:
        """Apply a complete configuration profile dict to the hardware."""

    @abstractmethod
    def wireless_product_ids(self) -> tuple:
        """USB product IDs that correspond to the 2.4GHz receiver."""

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    @classmethod
    def find_device(cls) -> Optional[str]:
        nodes = cls.find_nodes()
        return nodes[0].devnode if nodes else None

    def _refresh_node(self) -> Optional[HidrawNode]:
        nodes = self.find_nodes()
        self._node = nodes[0] if nodes else None
        self.dev_path = self._node.devnode if self._node else None
        return self._node

    def is_connected(self) -> bool:
        return self._refresh_node() is not None

    def is_wireless(self) -> bool:
        node = self._node or self._refresh_node()
        return bool(node and node.product_id in self.wireless_product_ids())

    def open_device(self, exclusive: bool = True) -> HidrawDevice:
        """Open (and by default lock) the device, rescanning once if the cached node is stale."""
        node = self._node or self._refresh_node()
        if node is None:
            raise DeviceNotFoundError(f"{self.display_name} non détecté(e).")
        try:
            return HidrawDevice(node, exact_feature_sizes=self.exact_feature_sizes(), exclusive=exclusive).open()
        except DeviceNotFoundError:
            node = self._refresh_node()
            if node is None:
                raise
            return HidrawDevice(node, exact_feature_sizes=self.exact_feature_sizes(), exclusive=exclusive).open()

    def exact_feature_sizes(self) -> bool:
        return os.environ.get("SKILLKORP_EXACT_REPORT_SIZE", "1") != "0"

    # ------------------------------------------------------------------
    # Persisted state
    # ------------------------------------------------------------------
    def _read_config_from_disk(self) -> Dict[str, Any]:
        path = self.config_file()
        data = config_store.load_json(path, dict)
        cfg = self.default_config()
        config_store.deep_merge(cfg, data)
        self._config_mtime = config_store.mtime_or_none(path)
        return self.sanitize_config(cfg)

    def _load_config(self) -> Dict[str, Any]:
        cfg = self._read_config_from_disk()
        if self._config_mtime is None:  # first run: materialize defaults
            try:
                self._write_config(cfg)
            except OSError:
                pass
        return cfg

    def _write_config(self, cfg: Dict[str, Any]):
        path = self.config_file()
        config_store.save_json_atomic(path, cfg)
        self._config_mtime = config_store.mtime_or_none(path)

    def reload_config_if_changed(self) -> bool:
        """Reload self.config if another process modified the file."""
        mtime = config_store.mtime_or_none(self.config_file())
        if mtime is None or mtime == self._config_mtime:
            return False
        self.config = self._read_config_from_disk()
        return True

    def save_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Persist the current (or given) configuration."""
        if config is not None:
            self.config = config
        with config_store.locked(self.config_file()):
            self._write_config(self.config)

    def update_config(self, mutator: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        """Atomic read-modify-write of the persisted state across processes."""
        with config_store.locked(self.config_file()):
            cfg = self._read_config_from_disk()
            mutator(cfg)
            cfg = self.sanitize_config(cfg)
            self._write_config(cfg)
        self.config = cfg
        return cfg

    def commit(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge *patch* into the persisted state."""
        patch = copy.deepcopy(patch)
        return self.update_config(lambda cfg: config_store.deep_merge(cfg, patch))

    # Backwards-compatible alias used by older call sites.
    def _save_config(self):
        self.save_config()
