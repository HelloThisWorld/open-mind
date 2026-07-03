/**
 * Coordinates order creation end to end. Payment is charged before the
 * order-created event is published; a notification failure never fails
 * the order itself.
 */
import { publishEvent } from "../events/event-publisher";
import { OrderCreatedEvent } from "../events/order-created";
import { sendOrderConfirmation } from "./notification-service";
import { chargePayment } from "./payment-gateway";

export interface NewOrder {
  orderId: string;
  amount: number;
  currency: string;
  idempotencyKey: string;
}

export class OrderService {
  // Marker class for the module; the flow logic lives in createOrder().
}

export async function createOrder(order: NewOrder): Promise<OrderCreatedEvent> {
  const charge = await chargePayment(order.orderId, order.amount, order.currency);
  const event: OrderCreatedEvent = {
    kind: "order.created",
    orderId: order.orderId,
    amount: order.amount,
    currency: order.currency,
    chargeId: charge.chargeId,
  };
  await publishEvent(event);
  await sendOrderConfirmation(order.orderId);
  return event;
}
