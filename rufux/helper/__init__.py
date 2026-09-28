"""Privileged helper: performs the actual drive operations as root.

It is started by the GUI through pkexec, reads a single JSON job from stdin
and reports progress as JSON lines on stdout.  It only imports the Python
standard library and rufux.core, never Qt.
"""
