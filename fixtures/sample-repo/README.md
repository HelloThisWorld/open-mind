# sample-repo

A tiny order-processing service used as Open Mind's bundled analysis fixture.
It is deliberately small but structurally real: an HTTP entry point, a routing
layer, domain services, and an event pipeline that parks exhausted deliveries
in a dead letter queue (DLQ).

This code is analyzed, never executed. It has no dependencies and no build.

## How an order flows through the system

1. `src/server.ts` boots the HTTP app and mounts the order routes.
2. `src/routes/orders.ts` accepts `POST /orders` requests.
3. `src/services/order-service.ts` validates and creates the order.
4. `src/services/payment-gateway.ts` charges the payment.
5. `src/events/event-publisher.ts` publishes the order-created event.
6. `src/services/notification-service.ts` sends the confirmation.

Failed event deliveries are retried with exponential backoff and parked in the
dead letter queue (DLQ) when all attempts are exhausted.

## Terminology

| Term | Definition |
| ---- | ---------- |
| SLA | The service level agreement for order intake: 99.9% of orders acknowledged within 2 seconds. |
| Backpressure | The mechanism that slows order intake when the event pipeline falls behind. |

See [docs/GLOSSARY.md](docs/GLOSSARY.md) for the full glossary.
