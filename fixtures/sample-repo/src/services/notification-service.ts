/**
 * Sends the confirmation message to the customer after an order is
 * created. A delivery failure is logged and retried; it never fails
 * the order itself.
 */
export class NotificationService {
  // Delivery transport is stubbed for the fixture.
}

export async function sendOrderConfirmation(orderId: string): Promise<boolean> {
  return orderId.length > 0;
}
