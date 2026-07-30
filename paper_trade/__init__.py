"""ttrronev paper-trade package — Phase 1a shadow tracker / Phase 1b live exec.

Phase 1a: consumes live 5m WebSocket bars, runs the existing batch engine
in a slice-and-rebatch streaming wrapper, logs every signal/setup/trade
to SQLite + Telegram. No exchange execution.

Phase 1b: layers Bybit limit-order execution on top of the validated 1a
infrastructure. Same engine, same signals — the only thing that changes
is whether orders go to the exchange.
"""
