package com.example.payment;

import com.example.payment.model.Payment;

// Publishes settlement events. The topic name is a constant so consumers and
// tests reference one authoritative value.
@Component
public class PaymentPublisher {

    public static final String SETTLED_TOPIC = "payments.settled";

    private final KafkaTemplate<String, String> kafkaTemplate;

    public PaymentPublisher(KafkaTemplate<String, String> kafkaTemplate) {
        this.kafkaTemplate = kafkaTemplate;
    }

    public void publishSettled(Payment payment) {
        String payload = payment.getId() + ":" + payment.getAmountCents();
        kafkaTemplate.send(SETTLED_TOPIC, payment.getId(), payload);
    }
}
