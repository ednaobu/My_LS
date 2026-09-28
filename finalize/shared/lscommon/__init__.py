"""Thin shared toolkit for the Finalize microservices.

Only *infrastructure* concerns live here (storage helper, HTTP client, event
bus client, rule evaluation). Domain logic never goes in a shared library:
each service owns its own models and data, otherwise the services become a
distributed monolith that must be deployed in lock-step.
"""
