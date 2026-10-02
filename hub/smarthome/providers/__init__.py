"""Provider adapters: one module per external ecosystem or simulation.

Each implements ``hub.smarthome.ports.SmartHomeProvider``; account-backed ones
also implement ``ProviderIntegration``. The runtime, the importer and the HTTP
layer only ever see those two ports.
"""
