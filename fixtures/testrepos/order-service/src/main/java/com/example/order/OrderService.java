package com.example.order;

import com.example.order.model.Order;

// Order intake rules. An order without a SKU (Stock Keeping Unit) is rejected
// before it ever reaches the repository.
@Service
public class OrderService {

    private final OrderRepository orderRepository;

    public OrderService(OrderRepository orderRepository) {
        this.orderRepository = orderRepository;
    }

    public Order placeOrder(Order order) {
        if (order.getSku() == null || order.getSku().isEmpty()) {
            throw new IllegalArgumentException("order requires a sku");
        }
        order.setStatus("PLACED");
        return orderRepository.save(order);
    }

    public Order getOrder(String id) {
        return orderRepository.findById(id);
    }
}
