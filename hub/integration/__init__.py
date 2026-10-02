"""Domain-neutral primitives for integrating external providers.

What a Provider adapter in any capability domain needs and no domain should
implement twice: bound accounts, an encrypted credential vault, a durable
receipt ledger for idempotent commands, and a cache of observed facts with a
change stream. Nothing here knows what a light or a lock is: the vocabulary is
``target``, ``scope``, ``observed``.

Built for the smart-home domain first and only; it is not a plugin framework.
A second domain reuses these modules as they are, or they grow then.
"""
