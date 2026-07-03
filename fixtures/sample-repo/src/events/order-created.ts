/**
 * The domain event emitted exactly once per successfully created order.
 */
export interface OrderCreatedEvent {
  kind: "order.created";
  orderId: string;
  amount: number;
  currency: string;
  chargeId: string;
}
