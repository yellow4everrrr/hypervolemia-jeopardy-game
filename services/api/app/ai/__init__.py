"""The AI coach layer.

Pure by design: the evidence bundle, the contract and the validator have no network
dependency, so the tests that prove a fabricated statistic cannot reach a trader run in
milliseconds with no API key. The only module that talks to a model is
``app.infrastructure.ai.anthropic_client``.
"""
