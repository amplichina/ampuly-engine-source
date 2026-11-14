"""Headless embeddings of the core runtime.

These modules must not import the console service factory, websocket handler,
or UI state stores. An embedding may attach a plugin host after initialize.
"""
