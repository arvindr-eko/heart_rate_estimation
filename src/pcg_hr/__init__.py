"""Streaming heart rate estimation from PCG for the stethoscope MCU.

Signal path (one module per block, each with init / process and fixed-size state):
frontend -> net -> features -> scorer -> tracker -> release, composed by estimator.
"""
