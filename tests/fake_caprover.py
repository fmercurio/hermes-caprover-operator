"""Shared opt-in loopback protocol fixture; no CapRover or images are run."""
from caprover_operator.lab import FakeCapRover, NEW_IMAGE, OLD_IMAGE

__all__ = ["FakeCapRover", "NEW_IMAGE", "OLD_IMAGE"]
