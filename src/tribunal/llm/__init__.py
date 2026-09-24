"""tribunal LLM layer: one client, four providers, one output contract.

Deliberately free of imports. `config.py` needs `ProviderName` from `llm.base`, so anything
imported here lands in that cycle -- `config` -> `llm.base` -> `llm/__init__` -> `llm.client`
-> `config`. Output-model registration therefore lives in `client.py`, which can import
`contracts` directly because `contracts` depends on nothing in the project.
"""
