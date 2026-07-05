# payment-service

Settles payments and publishes settlement events for downstream consumers.
Failed events are parked on a Dead Letter Queue (DLQ) for replay.
