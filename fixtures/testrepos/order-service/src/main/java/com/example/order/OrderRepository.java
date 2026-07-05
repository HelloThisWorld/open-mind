package com.example.order;

import com.example.order.model.Order;

// Storage boundary for orders; kept as an interface so tests can swap the store.
public interface OrderRepository {

    Order save(Order order);

    Order findById(String id);
}
