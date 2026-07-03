# Glossary

Terms used across the sample order-processing service. Each entry is the
authoritative in-repo definition of the term.

- **OrderService**: Coordinates order creation end to end: it validates the payload, charges payment, publishes the order-created event, and triggers the confirmation notification.
- **PaymentGateway**: The payment charging component. It authorizes and captures the order amount and never stores raw card numbers; only an opaque charge id is kept with the order.
- **EventPublisher**: Delivers domain events to subscribers with at-least-once semantics; failed deliveries are retried on the backoff schedule and parked in the dead letter queue when attempts are exhausted.
- **NotificationService**: Sends the confirmation message to the customer after an order is created. A delivery failure is logged and retried; it never fails the order itself.
- **OrderCreatedEvent**: The domain event emitted exactly once per successfully created order. It carries the order id, amount, currency, and charge id.
- **RetryPolicy**: The exponential backoff schedule for failed event deliveries: attempts wait 1s, 4s, 16s, 64s, then 256s before the event is parked in the dead letter queue (DLQ).
- **IdempotencyKey**: A client-supplied unique key sent with order creation requests; the stored first response is replayed for repeated submissions so duplicate orders are never created.
