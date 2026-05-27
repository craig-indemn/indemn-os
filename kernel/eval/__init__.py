"""Kernel-side eval framework: check_engine + (P4) code_executor + online_dispatch.

This package runs POST-CLAIM (outside save_tracked transactions). The watch
evaluator at kernel/watch/evaluator.py stays unchanged — entity-local +
microseconds + no I/O — per D-L architecture lock.
"""
