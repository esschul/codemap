package com.example;

import org.springframework.stereotype.Service;

@Service
public class OrderService {
    private final OrderRepository orderRepository;

    public OrderService(OrderRepository orderRepository) {
        this.orderRepository = orderRepository;
    }

    public Object findOrder(Long id) {
        return orderRepository.findById(id);
    }

    public Object createOrder(Object body) {
        validate(body);
        return orderRepository.save(body);
    }

    // Private helper — should be followed from createOrder
    private void validate(Object body) {
        orderRepository.existsById(1L);
    }
}
